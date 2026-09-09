"""Base connector primitives for the AVIS ingestion layer."""

from __future__ import annotations

import abc
import base64
import hashlib
import json
import time as time_module
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from avis.db.models import DqIncident, DqResult, DqRule, OpsJobEvent, OpsPipelineRun
from etl.ingest.contracts import ContractViolation, load_contract, validate_rows


@dataclass(slots=True)
class RawWriteResult:
    """Outcome of a raw-zone write."""

    source_system: str
    trade_date: date
    checksum: str
    row_count: int
    manifest_path: Path
    created: bool
    updated: bool
    pipeline_run_id: int | None = None
    confirmed_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Bounded exponential backoff for transient connector failures."""

    max_attempts: int = 3
    initial_delay_seconds: float = 5.0
    max_delay_seconds: float = 300.0


class BaseConnector(abc.ABC):
    """Base class that standardizes connector execution and raw-zone writes."""

    source_system: str
    pipeline_name: str
    job_name: str

    def __init__(
        self,
        *,
        session: Session,
        raw_zone_root: Path,
        contract_path: Path,
        retry_policy: RetryPolicy | None = None,
        sleep_fn: Callable[[float], None] | None = None,
    ) -> None:
        self._session = session
        self._raw_zone_root = raw_zone_root
        self._contract_path = contract_path
        self._retry_policy = retry_policy or RetryPolicy()
        self._sleep_fn = sleep_fn or time_module.sleep
        self.last_pipeline_run_id: int | None = None

    @abc.abstractmethod
    def fetch(self, trade_date: date) -> bytes:
        """Fetch raw payload bytes for the requested trade date."""

    @abc.abstractmethod
    def validate(
        self,
        payload: bytes,
        trade_date: date,
    ) -> list[dict[str, Any]]:
        """Parse and connector-validate rows before contract enforcement."""

    @abc.abstractmethod
    def write_raw(
        self,
        rows: list[dict[str, Any]],
        payload: bytes,
        trade_date: date,
        pipeline_run_id: int,
    ) -> RawWriteResult:
        """Persist validated rows into the raw zone."""

    def run(self, trade_date: date) -> RawWriteResult:
        """Execute fetch, contract enforcement, and raw write for a connector."""

        pipeline_run = self._create_pipeline_run(trade_date)
        self.last_pipeline_run_id = pipeline_run.pipeline_run_id
        self._emit_job_event(
            pipeline_run.pipeline_run_id,
            event_type="START",
            message=f"Connector {self.source_system} started",
        )
        attempt = 1
        while True:
            try:
                payload = self.fetch(trade_date)
                rows = self.validate(payload, trade_date)
                contract = load_contract(self._contract_path)
                valid_rows, rejected = validate_rows(rows, contract)
                self._quarantine_contract_violations(
                    rejected,
                    pipeline_run.pipeline_run_id,
                    target_table=f"raw_zone:{self.source_system}",
                )
                result = self.write_raw(
                    valid_rows,
                    payload,
                    trade_date,
                    pipeline_run.pipeline_run_id,
                )
                result = RawWriteResult(
                    source_system=result.source_system,
                    trade_date=result.trade_date,
                    checksum=result.checksum,
                    row_count=result.row_count,
                    manifest_path=result.manifest_path,
                    created=result.created,
                    updated=result.updated,
                    pipeline_run_id=pipeline_run.pipeline_run_id,
                    confirmed_at=self._now(),
                )
                self._emit_job_event(
                    pipeline_run.pipeline_run_id,
                    event_type="END",
                    message=f"raw_write_confirmed path={result.manifest_path}",
                    metrics_payload={
                        "attempt": attempt,
                        "row_count": result.row_count,
                        "checksum": result.checksum,
                        "created": result.created,
                        "updated": result.updated,
                    },
                )
                self._finish_pipeline_run(pipeline_run, "SUCCESS")
                return result
            except Exception as exc:
                retryable = self._is_retryable_exception(exc)
                can_retry = retryable and attempt < self._retry_policy.max_attempts
                if can_retry:
                    delay_seconds = self._retry_delay_seconds(attempt)
                    self._emit_job_event(
                        pipeline_run.pipeline_run_id,
                        event_type="RETRY",
                        message=f"retrying_after_failure: {exc}",
                        metrics_payload={
                            "attempt": attempt,
                            "max_attempts": self._retry_policy.max_attempts,
                            "delay_seconds": delay_seconds,
                        },
                    )
                    self._sleep_fn(delay_seconds)
                    attempt += 1
                    continue

                self._emit_job_event(
                    pipeline_run.pipeline_run_id,
                    event_type="FAILED",
                    message=f"run_failed: {exc}",
                    metrics_payload={
                        "attempt": attempt,
                        "max_attempts": self._retry_policy.max_attempts,
                        "retryable": retryable,
                    },
                )
                if retryable:
                    self._record_retry_exhaustion_incident(
                        pipeline_run_id=pipeline_run.pipeline_run_id,
                        trade_date=trade_date,
                        attempt=attempt,
                        failure_reason=str(exc),
                    )
                self._finish_pipeline_run(pipeline_run, "FAILED")
                raise

    def _write_raw_payload(
        self,
        *,
        rows: list[dict[str, Any]],
        payload: bytes,
        trade_date: date,
        source_system: str,
    ) -> RawWriteResult:
        """Write a versioned raw-zone record keyed by source and trade date."""

        checksum = self.compute_checksum(payload)
        base_path = self._raw_zone_root / source_system / trade_date.isoformat()
        payload_path = base_path / f"{checksum}.json"
        manifest_path = base_path / "manifest.json"
        base_path.mkdir(parents=True, exist_ok=True)

        created = not payload_path.exists()
        current_manifest: dict[str, Any] | None = None
        if manifest_path.exists():
            current_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if current_manifest.get("current_checksum") == checksum:
                return RawWriteResult(
                    source_system=source_system,
                    trade_date=trade_date,
                    checksum=checksum,
                    row_count=len(rows),
                    manifest_path=manifest_path,
                    created=False,
                    updated=False,
                )

        payload_path.write_text(
            json.dumps(
                {
                    "source_system": source_system,
                    "trade_date": trade_date.isoformat(),
                    "checksum": checksum,
                    "payload_b64": base64.b64encode(payload).decode("ascii"),
                    "rows": [self._jsonify_row(row) for row in rows],
                    "written_at": self._now().isoformat(),
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        manifest_path.write_text(
            json.dumps(
                {
                    "source_system": source_system,
                    "trade_date": trade_date.isoformat(),
                    "current_checksum": checksum,
                    "current_payload_file": payload_path.name,
                    "previous_checksum": None
                    if current_manifest is None
                    else current_manifest.get("current_checksum"),
                    "row_count": len(rows),
                    "updated_at": self._now().isoformat(),
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        return RawWriteResult(
            source_system=source_system,
            trade_date=trade_date,
            checksum=checksum,
            row_count=len(rows),
            manifest_path=manifest_path,
            created=created,
            updated=True,
            confirmed_at=self._now(),
        )

    def compute_checksum(self, payload: bytes) -> str:
        """Hash a payload and return its SHA256 checksum."""

        return hashlib.sha256(payload).hexdigest()

    def _create_pipeline_run(self, trade_date: date) -> OpsPipelineRun:
        pipeline_run = OpsPipelineRun(
            run_uuid=uuid.uuid4(),
            pipeline_name=self.pipeline_name,
            run_mode="SCHEDULED",
            triggered_by="SYSTEM",
            status="RUNNING",
            started_at=self._now(),
            ended_at=None,
            run_context={
                "source_system": self.source_system,
                "trade_date": trade_date.isoformat(),
            },
        )
        self._assign_pk_if_sqlite(pipeline_run, "pipeline_run_id", OpsPipelineRun)
        self._session.add(pipeline_run)
        self._session.flush()
        return pipeline_run

    def _finish_pipeline_run(self, pipeline_run: OpsPipelineRun, status: str) -> None:
        pipeline_run.status = status
        pipeline_run.ended_at = self._now()
        self._session.flush()

    def _emit_job_event(
        self,
        pipeline_run_id: int,
        *,
        event_type: str,
        message: str,
        metrics_payload: dict[str, Any] | None = None,
    ) -> OpsJobEvent:
        event = OpsJobEvent(
            pipeline_run_id=pipeline_run_id,
            job_name=self.job_name,
            event_type=event_type,
            event_ts=self._now(),
            message=message,
            metrics_payload=metrics_payload,
        )
        self._assign_pk_if_sqlite(event, "job_event_id", OpsJobEvent)
        self._session.add(event)
        self._session.flush()
        return event

    def _quarantine_contract_violations(
        self,
        violations: list[ContractViolation],
        pipeline_run_id: int,
        *,
        target_table: str,
    ) -> None:
        if not violations:
            return
        rule = self._ensure_dq_rule(
            rule_code="RAW_CONTRACT_VALIDATION_FAILED",
            rule_name="Raw contract validation failed",
        )
        for violation in violations:
            dq_result = DqResult(
                dq_rule_id=rule.dq_rule_id,
                pipeline_run_id=pipeline_run_id,
                target_table=target_table,
                target_record_key=str(violation.row.get("symbol", "unknown")),
                status="FAIL",
                failure_reason=violation.reason,
                measured_value=json.dumps(self._jsonify_row(violation.row), sort_keys=True),
                threshold_value=None,
                evaluated_at=self._now(),
            )
            self._assign_pk_if_sqlite(dq_result, "dq_result_id", DqResult)
            self._session.add(dq_result)
            self._session.flush()

            dq_incident = DqIncident(
                dq_result_id=dq_result.dq_result_id,
                incident_status="OPEN",
                impact_level="MEDIUM",
                assigned_to="DATA_ENGINEERING",
                opened_at=self._now(),
                resolved_at=None,
                resolution_notes=violation.reason,
            )
            self._assign_pk_if_sqlite(dq_incident, "dq_incident_id", DqIncident)
            self._session.add(dq_incident)
            self._session.flush()

    def _ensure_dq_rule(self, *, rule_code: str, rule_name: str) -> DqRule:
        existing = self._session.scalar(
            select(DqRule).where(DqRule.rule_code == rule_code)
        )
        if existing is not None:
            return existing
        rule = DqRule(
            rule_code=rule_code,
            rule_name=rule_name,
            domain_name="INGESTION",
            severity="HIGH",
            rule_expression=rule_name,
            is_active=True,
            owner_team="DATA_ENGINEERING",
            created_at=self._now(),
            updated_at=self._now(),
        )
        self._assign_pk_if_sqlite(rule, "dq_rule_id", DqRule)
        self._session.add(rule)
        self._session.flush()
        return rule

    def _record_retry_exhaustion_incident(
        self,
        *,
        pipeline_run_id: int,
        trade_date: date,
        attempt: int,
        failure_reason: str,
    ) -> None:
        rule = self._ensure_dq_rule(
            rule_code="CONNECTOR_RETRY_EXHAUSTED",
            rule_name="Connector retries exhausted",
        )
        dq_result = DqResult(
            dq_rule_id=rule.dq_rule_id,
            pipeline_run_id=pipeline_run_id,
            target_table=f"raw_zone:{self.source_system}",
            target_record_key=trade_date.isoformat(),
            status="FAIL",
            failure_reason=failure_reason,
            measured_value=json.dumps(
                {
                    "attempt": attempt,
                    "source_system": self.source_system,
                },
                sort_keys=True,
            ),
            threshold_value=json.dumps(
                {"max_attempts": self._retry_policy.max_attempts},
                sort_keys=True,
            ),
            evaluated_at=self._now(),
        )
        self._assign_pk_if_sqlite(dq_result, "dq_result_id", DqResult)
        self._session.add(dq_result)
        self._session.flush()

        dq_incident = DqIncident(
            dq_result_id=dq_result.dq_result_id,
            incident_status="OPEN",
            impact_level="HIGH",
            assigned_to="DATA_ENGINEERING",
            opened_at=self._now(),
            resolved_at=None,
            resolution_notes=f"retry_exhausted source={self.source_system} reason={failure_reason}",
        )
        self._assign_pk_if_sqlite(dq_incident, "dq_incident_id", DqIncident)
        self._session.add(dq_incident)
        self._session.flush()

    def _assign_pk_if_sqlite(self, instance: Any, pk_name: str, model: type[Any]) -> None:
        bind = self._session.get_bind()
        if bind is None or bind.dialect.name != "sqlite":
            return
        if getattr(instance, pk_name, None) is not None:
            return
        pk_column = getattr(model, pk_name)
        current_max = self._session.scalar(select(func.max(pk_column)))
        setattr(instance, pk_name, 1 if current_max is None else int(current_max) + 1)

    @staticmethod
    def _jsonify_row(row: dict[str, Any]) -> dict[str, Any]:
        converted: dict[str, Any] = {}
        for key, value in row.items():
            if hasattr(value, "isoformat"):
                converted[key] = value.isoformat()
            elif isinstance(value, uuid.UUID):
                converted[key] = str(value)
            else:
                converted[key] = None if value is None else str(value)
        return converted

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    def _retry_delay_seconds(self, attempt: int) -> float:
        delay = self._retry_policy.initial_delay_seconds * (2 ** max(attempt - 1, 0))
        return min(delay, self._retry_policy.max_delay_seconds)

    @staticmethod
    def _is_retryable_exception(exc: Exception) -> bool:
        if isinstance(exc, HTTPError):
            return exc.code == 429 or 500 <= exc.code < 600
        return isinstance(exc, (TimeoutError, ConnectionError, URLError))
