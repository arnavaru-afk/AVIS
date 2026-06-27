"""Alias registry services for the security master."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from avis.core.entity_resolution.exceptions import (
    AliasConflictError,
    EntityNotFoundError,
    UnsupportedAliasSourceError,
)
from avis.db.models import RefInstrument, RefSymbolAlias

ALIAS_SOURCES = frozenset(
    {"NSE", "BSE", "BSE_SME", "ISIN", "BLOOMBERG", "REFINITIV"}
)


@dataclass(slots=True)
class AliasMutation:
    """Input payload for alias create and update flows."""

    instrument_id: int
    source_system: str
    alias_symbol: str
    valid_from: date
    valid_to: date | None = None


class AliasRegistryService:
    """Owns lifecycle rules for `ref_symbol_alias`."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create_alias(self, mutation: AliasMutation) -> RefSymbolAlias:
        """Create a new alias after enforcing source and overlap rules."""

        normalized = self._normalize_mutation(mutation)
        self._assert_instrument_exists(normalized.instrument_id)
        self._assert_no_conflict(
            normalized.source_system,
            normalized.alias_symbol,
            normalized.valid_from,
            normalized.valid_to,
            normalized.instrument_id,
        )

        alias = RefSymbolAlias(
            instrument_id=normalized.instrument_id,
            source_system=normalized.source_system,
            alias_symbol=normalized.alias_symbol,
            valid_from=normalized.valid_from,
            valid_to=normalized.valid_to,
            created_at=self._now(),
        )
        if self._uses_sqlite():
            alias.alias_id = self._next_alias_id()
        self._session.add(alias)
        self._session.flush()
        return alias

    def get_alias(self, alias_id: int) -> RefSymbolAlias:
        """Fetch a single alias or raise when missing."""

        alias = self._session.get(RefSymbolAlias, alias_id)
        if alias is None:
            raise EntityNotFoundError(f"Alias {alias_id} was not found")
        return alias

    def list_aliases(
        self,
        *,
        instrument_id: int | None = None,
        source_system: str | None = None,
    ) -> list[RefSymbolAlias]:
        """List aliases filtered by instrument and/or source."""

        stmt: Select[tuple[RefSymbolAlias]] = select(RefSymbolAlias)
        if instrument_id is not None:
            stmt = stmt.where(RefSymbolAlias.instrument_id == instrument_id)
        if source_system is not None:
            stmt = stmt.where(
                RefSymbolAlias.source_system == source_system.strip().upper()
            )
        stmt = stmt.order_by(
            RefSymbolAlias.source_system,
            RefSymbolAlias.alias_symbol,
            RefSymbolAlias.valid_from,
        )
        return list(self._session.scalars(stmt))

    def update_alias(self, alias_id: int, mutation: AliasMutation) -> RefSymbolAlias:
        """Update an alias while preserving lifecycle integrity."""

        alias = self.get_alias(alias_id)
        normalized = self._normalize_mutation(mutation)
        self._assert_instrument_exists(normalized.instrument_id)
        self._assert_no_conflict(
            normalized.source_system,
            normalized.alias_symbol,
            normalized.valid_from,
            normalized.valid_to,
            normalized.instrument_id,
            exclude_alias_id=alias_id,
        )

        alias.instrument_id = normalized.instrument_id
        alias.source_system = normalized.source_system
        alias.alias_symbol = normalized.alias_symbol
        alias.valid_from = normalized.valid_from
        alias.valid_to = normalized.valid_to
        self._session.flush()
        return alias

    def deactivate_alias(self, alias_id: int, valid_to: date) -> RefSymbolAlias:
        """Close an alias validity window without deleting history."""

        alias = self.get_alias(alias_id)
        if valid_to < alias.valid_from:
            raise AliasConflictError("valid_to cannot be earlier than valid_from")
        alias.valid_to = valid_to
        self._session.flush()
        return alias

    def delete_alias(self, alias_id: int) -> None:
        """Delete an alias entry."""

        alias = self.get_alias(alias_id)
        self._session.delete(alias)
        self._session.flush()

    def _normalize_mutation(self, mutation: AliasMutation) -> AliasMutation:
        source_system = mutation.source_system.strip().upper()
        alias_symbol = mutation.alias_symbol.strip().upper()
        if source_system not in ALIAS_SOURCES:
            raise UnsupportedAliasSourceError(
                f"Unsupported alias source: {mutation.source_system}"
            )
        if mutation.valid_to is not None and mutation.valid_to < mutation.valid_from:
            raise AliasConflictError("valid_to cannot be earlier than valid_from")
        return AliasMutation(
            instrument_id=mutation.instrument_id,
            source_system=source_system,
            alias_symbol=alias_symbol,
            valid_from=mutation.valid_from,
            valid_to=mutation.valid_to,
        )

    def _assert_instrument_exists(self, instrument_id: int) -> None:
        if self._session.get(RefInstrument, instrument_id) is None:
            raise EntityNotFoundError(f"Instrument {instrument_id} was not found")

    def _assert_no_conflict(
        self,
        source_system: str,
        alias_symbol: str,
        valid_from: date,
        valid_to: date | None,
        instrument_id: int,
        *,
        exclude_alias_id: int | None = None,
    ) -> None:
        stmt = select(RefSymbolAlias).where(
            RefSymbolAlias.source_system == source_system,
            RefSymbolAlias.alias_symbol == alias_symbol,
        )
        if exclude_alias_id is not None:
            stmt = stmt.where(RefSymbolAlias.alias_id != exclude_alias_id)

        for existing in self._session.scalars(stmt):
            if existing.instrument_id == instrument_id:
                continue
            if self._ranges_overlap(
                existing.valid_from,
                existing.valid_to,
                valid_from,
                valid_to,
            ):
                raise AliasConflictError(
                    "No two active aliases from the same source may point to "
                    "different instruments"
                )

    @staticmethod
    def _ranges_overlap(
        left_start: date,
        left_end: date | None,
        right_start: date,
        right_end: date | None,
    ) -> bool:
        left_bound = left_end or date.max
        right_bound = right_end or date.max
        return left_start <= right_bound and right_start <= left_bound

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    def _next_alias_id(self) -> int:
        current_max = self._session.scalar(select(func.max(RefSymbolAlias.alias_id)))
        return 1 if current_max is None else int(current_max) + 1

    def _uses_sqlite(self) -> bool:
        bind = self._session.get_bind()
        return bind is not None and bind.dialect.name == "sqlite"
