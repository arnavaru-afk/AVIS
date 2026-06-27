"""Schema contract loading and validation for ingestion connectors."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class FieldContract:
    """Single field definition inside a source contract."""

    name: str
    type_name: str
    nullable: bool


@dataclass(slots=True)
class ContractViolation:
    """Rejected row and the reason it failed contract validation."""

    row: dict[str, Any]
    reason: str


@dataclass(slots=True)
class SchemaContract:
    """Connector contract loaded from `data/contracts`."""

    name: str
    fields: tuple[FieldContract, ...]

    @property
    def field_names(self) -> tuple[str, ...]:
        return tuple(field.name for field in self.fields)


def load_contract(path: Path) -> SchemaContract:
    """Load a JSON contract file from disk."""

    payload = json.loads(path.read_text(encoding="utf-8"))
    fields = tuple(
        FieldContract(
            name=field["name"],
            type_name=field["type"],
            nullable=bool(field["nullable"]),
        )
        for field in payload["fields"]
    )
    return SchemaContract(name=payload["name"], fields=fields)


def validate_rows(
    rows: list[dict[str, Any]],
    contract: SchemaContract,
) -> tuple[list[dict[str, Any]], list[ContractViolation]]:
    """Validate rows against the exact contract shape."""

    valid_rows: list[dict[str, Any]] = []
    rejected: list[ContractViolation] = []
    expected_fields = set(contract.field_names)

    for row in rows:
        actual_fields = set(row.keys())
        if actual_fields != expected_fields:
            missing = sorted(expected_fields - actual_fields)
            extra = sorted(actual_fields - expected_fields)
            rejected.append(
                ContractViolation(
                    row=row,
                    reason=(
                        f"schema_drift missing={missing or '[]'} extra={extra or '[]'}"
                    ),
                )
            )
            continue

        row_rejected = False
        for field in contract.fields:
            value = row[field.name]
            if value is None:
                if field.nullable:
                    continue
                rejected.append(
                    ContractViolation(
                        row=row,
                        reason=f"null_not_allowed field={field.name}",
                    )
                )
                row_rejected = True
                break

            if not _matches_type(value, field.type_name):
                rejected.append(
                    ContractViolation(
                        row=row,
                        reason=(
                            f"type_mismatch field={field.name} "
                            f"expected={field.type_name}"
                        ),
                    )
                )
                row_rejected = True
                break

        if not row_rejected:
            valid_rows.append(row)

    return valid_rows, rejected


def _matches_type(value: Any, type_name: str) -> bool:
    if type_name == "string":
        return isinstance(value, str)
    if type_name == "decimal":
        return isinstance(value, Decimal)
    if type_name == "date":
        return isinstance(value, date) and not isinstance(value, datetime)
    return False
