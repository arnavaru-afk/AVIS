"""Canonical identity and point-in-time resolution services."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone

from sqlalchemy import Select, func, or_, select
from sqlalchemy.orm import Session

from avis.core.entity_resolution.exceptions import EntityNotFoundError
from avis.db.models import RefCompany, RefExchange, RefInstrument, RefSymbolAlias

EXCHANGE_SUFFIX_MAP = {
    "NS": "NSE",
    "NSE": "NSE",
    "BO": "BSE",
    "BSE": "BSE",
    "SME": "BSE_SME",
    "BSE_SME": "BSE_SME",
}

ISIN_PATTERN = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")


@dataclass(slots=True)
class ResolutionResult:
    """Traceable resolution payload used by the security master."""

    company_uuid: uuid.UUID
    instrument_id: int
    matched_input: str
    rule_used: str
    confidence_score: int
    source_provenance: str
    effective_timestamp: datetime


class CanonicalIdentityResolver:
    """Resolve security identifiers to canonical company UUIDs."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def resolve(
        self,
        identifier: str,
        *,
        exchange_mic: str | None = None,
        as_of_date: date | None = None,
    ) -> uuid.UUID:
        """Resolve to a canonical company UUID or raise."""

        return self.resolve_with_metadata(
            identifier,
            exchange_mic=exchange_mic,
            as_of_date=as_of_date,
        ).company_uuid

    def resolve_with_metadata(
        self,
        identifier: str,
        *,
        exchange_mic: str | None = None,
        as_of_date: date | None = None,
    ) -> ResolutionResult:
        """Resolve an identifier and return trace metadata."""

        normalized = self._normalize_identifier(identifier)
        effective_date = as_of_date or date.today()

        if ISIN_PATTERN.match(normalized):
            result = self._resolve_by_isin(normalized, effective_date)
            if result is not None:
                return result

        parsed_symbol, parsed_exchange = self._parse_symbol_exchange(
            normalized,
            exchange_mic=exchange_mic,
        )
        result = self._resolve_by_symbol_exact(
            parsed_symbol,
            parsed_exchange,
            effective_date,
        )
        if result is not None:
            return result

        result = self._resolve_by_alias_fuzzy(normalized, effective_date)
        if result is not None:
            return result

        raise EntityNotFoundError(f"Unable to resolve identifier: {identifier}")

    def resolve_as_of(
        self,
        symbol: str,
        exchange: str,
        as_of_date: date,
    ) -> int:
        """Resolve a point-in-time symbol to the matching instrument id."""

        normalized_symbol = self._normalize_identifier(symbol)
        normalized_exchange = self._normalize_exchange(exchange)

        instrument_stmt: Select[tuple[RefInstrument]] = (
            select(RefInstrument)
            .join(RefExchange)
            .where(
                func.upper(RefInstrument.symbol) == normalized_symbol,
                RefExchange.exchange_code == normalized_exchange,
                RefInstrument.listing_date.is_(None)
                | (RefInstrument.listing_date <= as_of_date),
                RefInstrument.delisting_date.is_(None)
                | (RefInstrument.delisting_date >= as_of_date),
            )
        )
        instruments = list(self._session.scalars(instrument_stmt))
        if len(instruments) == 1:
            return instruments[0].instrument_id
        if len(instruments) > 1:
            raise EntityNotFoundError(
                f"Ambiguous symbol resolution for {symbol} on {exchange}"
            )

        alias_stmt: Select[tuple[RefSymbolAlias]] = (
            select(RefSymbolAlias)
            .join(RefInstrument)
            .join(RefExchange)
            .where(
                func.upper(RefSymbolAlias.alias_symbol) == normalized_symbol,
                RefExchange.exchange_code == normalized_exchange,
                RefSymbolAlias.valid_from <= as_of_date,
                or_(
                    RefSymbolAlias.valid_to.is_(None),
                    RefSymbolAlias.valid_to >= as_of_date,
                ),
                RefInstrument.listing_date.is_(None)
                | (RefInstrument.listing_date <= as_of_date),
                RefInstrument.delisting_date.is_(None)
                | (RefInstrument.delisting_date >= as_of_date),
            )
        )
        aliases = list(self._session.scalars(alias_stmt))
        if len(aliases) == 1:
            return aliases[0].instrument_id
        raise EntityNotFoundError(
            f"No point-in-time symbol mapping for {symbol} on {exchange} as of {as_of_date}"
        )

    def _resolve_by_isin(
        self,
        isin: str,
        as_of_date: date,
    ) -> ResolutionResult | None:
        stmt = (
            select(RefCompany, RefInstrument)
            .join(RefInstrument)
            .where(
                func.upper(RefCompany.isin_primary) == isin,
                RefInstrument.listing_date.is_(None)
                | (RefInstrument.listing_date <= as_of_date),
                RefInstrument.delisting_date.is_(None)
                | (RefInstrument.delisting_date >= as_of_date),
            )
            .order_by(RefInstrument.instrument_id.asc())
        )
        rows = list(self._session.execute(stmt))
        if not rows:
            return None
        company, instrument = rows[0]
        return ResolutionResult(
            company_uuid=company.company_uuid,
            instrument_id=instrument.instrument_id,
            matched_input=isin,
            rule_used="ISIN_EXACT",
            confidence_score=100,
            source_provenance="ref_company.isin_primary",
            effective_timestamp=self._now(),
        )

    def _resolve_by_symbol_exact(
        self,
        symbol: str,
        exchange: str | None,
        as_of_date: date,
    ) -> ResolutionResult | None:
        stmt = select(RefCompany, RefInstrument).join(RefInstrument)
        if exchange is not None:
            stmt = stmt.join(RefExchange).where(RefExchange.exchange_code == exchange)
        stmt = stmt.where(
            func.upper(RefInstrument.symbol) == symbol,
            RefInstrument.listing_date.is_(None)
            | (RefInstrument.listing_date <= as_of_date),
            RefInstrument.delisting_date.is_(None)
            | (RefInstrument.delisting_date >= as_of_date),
        )
        rows = list(self._session.execute(stmt))
        if len(rows) != 1:
            return None
        company, instrument = rows[0]
        provenance = (
            f"ref_instrument.symbol+ref_exchange.exchange_code={exchange}"
            if exchange is not None
            else "ref_instrument.symbol"
        )
        return ResolutionResult(
            company_uuid=company.company_uuid,
            instrument_id=instrument.instrument_id,
            matched_input=symbol if exchange is None else f"{symbol}.{exchange}",
            rule_used="TICKER_EXACT",
            confidence_score=95 if exchange is not None else 90,
            source_provenance=provenance,
            effective_timestamp=self._now(),
        )

    def _resolve_by_alias_fuzzy(
        self,
        identifier: str,
        as_of_date: date,
    ) -> ResolutionResult | None:
        stmt = (
            select(RefSymbolAlias, RefCompany, RefInstrument)
            .join(RefInstrument, RefSymbolAlias.instrument_id == RefInstrument.instrument_id)
            .join(RefCompany, RefInstrument.company_id == RefCompany.company_id)
            .where(
                RefSymbolAlias.valid_from <= as_of_date,
                or_(
                    RefSymbolAlias.valid_to.is_(None),
                    RefSymbolAlias.valid_to >= as_of_date,
                ),
                RefInstrument.listing_date.is_(None)
                | (RefInstrument.listing_date <= as_of_date),
                RefInstrument.delisting_date.is_(None)
                | (RefInstrument.delisting_date >= as_of_date),
            )
        )
        matches: list[tuple[int, RefSymbolAlias, RefCompany, RefInstrument]] = []
        for alias, company, instrument in self._session.execute(stmt):
            distance = self._levenshtein_distance(
                identifier,
                self._normalize_identifier(alias.alias_symbol),
            )
            if distance <= 2:
                matches.append((distance, alias, company, instrument))

        if not matches:
            return None

        matches.sort(key=lambda item: (item[0], item[3].instrument_id))
        best_distance = matches[0][0]
        top_matches = [item for item in matches if item[0] == best_distance]
        if len(top_matches) > 1:
            raise EntityNotFoundError(
                f"Ambiguous fuzzy alias resolution for {identifier}"
            )

        _, alias, company, instrument = top_matches[0]
        confidence = 90 if best_distance == 0 else max(70, 90 - (best_distance * 10))
        return ResolutionResult(
            company_uuid=company.company_uuid,
            instrument_id=instrument.instrument_id,
            matched_input=alias.alias_symbol,
            rule_used="ALIAS_FUZZY",
            confidence_score=confidence,
            source_provenance=f"ref_symbol_alias.source_system={alias.source_system}",
            effective_timestamp=self._now(),
        )

    @staticmethod
    def _normalize_identifier(identifier: str) -> str:
        return identifier.strip().upper()

    def _parse_symbol_exchange(
        self,
        identifier: str,
        *,
        exchange_mic: str | None,
    ) -> tuple[str, str | None]:
        exchange = self._normalize_exchange(exchange_mic) if exchange_mic else None
        if "." not in identifier:
            return identifier, exchange

        symbol, suffix = identifier.rsplit(".", 1)
        resolved_exchange = self._normalize_exchange(suffix)
        return symbol, exchange or resolved_exchange

    def _normalize_exchange(self, exchange: str | None) -> str:
        if exchange is None:
            raise EntityNotFoundError("Exchange is required for this lookup")
        normalized = exchange.strip().upper()
        return EXCHANGE_SUFFIX_MAP.get(normalized, normalized)

    @staticmethod
    def _levenshtein_distance(left: str, right: str) -> int:
        if left == right:
            return 0
        if not left:
            return len(right)
        if not right:
            return len(left)

        previous_row = list(range(len(right) + 1))
        for left_index, left_char in enumerate(left, start=1):
            current_row = [left_index]
            for right_index, right_char in enumerate(right, start=1):
                insertions = previous_row[right_index] + 1
                deletions = current_row[right_index - 1] + 1
                substitutions = previous_row[right_index - 1] + (
                    0 if left_char == right_char else 1
                )
                current_row.append(min(insertions, deletions, substitutions))
            previous_row = current_row
        return previous_row[-1]

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)
