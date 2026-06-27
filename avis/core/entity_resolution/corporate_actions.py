"""Corporate action adjustment helpers for security master workflows."""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from avis.core.entity_resolution.exceptions import EntityNotFoundError
from avis.db.models import MarketCorporateAction, MarketOhlcv1D, RefInstrument

SUPPORTED_ACTION_TYPES = frozenset(
    {
        "SPLIT",
        "BONUS",
        "RIGHTS",
        "DIVIDEND",
        "MERGER",
        "DEMERGER",
        "DELISTING",
    }
)


class CorporateActionAdjustmentService:
    """Stores and applies corporate action adjustment chains."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create_action(
        self,
        *,
        instrument_id: int,
        action_type: str,
        ex_date: date,
        source_system: str,
        announcement_date: date | None = None,
        record_date: date | None = None,
        effective_date: date | None = None,
        action_value: Decimal | None = None,
        action_ratio_num: Decimal | None = None,
        action_ratio_den: Decimal | None = None,
        currency_code: str | None = None,
        source_record_id: str | None = None,
    ) -> MarketCorporateAction:
        """Persist a corporate action in the existing market action table."""

        normalized_type = action_type.strip().upper()
        if normalized_type not in SUPPORTED_ACTION_TYPES:
            raise ValueError(f"Unsupported action type: {action_type}")
        if self._session.get(RefInstrument, instrument_id) is None:
            raise EntityNotFoundError(f"Instrument {instrument_id} was not found")

        action = MarketCorporateAction(
            instrument_id=instrument_id,
            action_type=normalized_type,
            announcement_date=announcement_date,
            ex_date=ex_date,
            record_date=record_date,
            effective_date=effective_date,
            action_value=action_value,
            action_ratio_num=action_ratio_num,
            action_ratio_den=action_ratio_den,
            currency_code=currency_code,
            source_system=source_system.strip().upper(),
            source_record_id=source_record_id,
            ingested_at=self._now(),
        )
        if self._uses_sqlite():
            action.corp_action_id = self._next_corporate_action_id()
        self._session.add(action)
        self._session.flush()
        return action

    def adjusted_close(self, instrument_id: int, as_of_date: date) -> Decimal:
        """Return the historical close adjusted by the post-date action chain."""

        instrument = self._session.get(RefInstrument, instrument_id)
        if instrument is None:
            raise EntityNotFoundError(f"Instrument {instrument_id} was not found")
        if instrument.delisting_date is not None and as_of_date > instrument.delisting_date:
            raise EntityNotFoundError(
                f"Instrument {instrument_id} was delisted before {as_of_date}"
            )

        price_row = self._session.scalar(
            select(MarketOhlcv1D)
            .where(
                MarketOhlcv1D.instrument_id == instrument_id,
                MarketOhlcv1D.trade_date == as_of_date,
            )
        )
        if price_row is None:
            raise EntityNotFoundError(
                f"No OHLCV row for instrument {instrument_id} on {as_of_date}"
            )

        factor = Decimal("1")
        action_stmt: Select[tuple[MarketCorporateAction]] = (
            select(MarketCorporateAction)
            .where(
                MarketCorporateAction.instrument_id == instrument_id,
                MarketCorporateAction.ex_date > as_of_date,
            )
            .order_by(MarketCorporateAction.ex_date.asc(), MarketCorporateAction.corp_action_id.asc())
        )
        for action in self._session.scalars(action_stmt):
            factor *= self._factor_for_action(action)
        return (price_row.close_px * factor).quantize(Decimal("0.000001"))

    def _factor_for_action(self, action: MarketCorporateAction) -> Decimal:
        action_type = action.action_type.upper()
        if action_type in {"SPLIT", "BONUS", "RIGHTS"}:
            if action.action_ratio_num is None or action.action_ratio_den is None:
                return Decimal("1")
            if action.action_ratio_num == 0:
                return Decimal("1")
            return Decimal(action.action_ratio_den) / Decimal(action.action_ratio_num)

        if action_type == "DIVIDEND":
            if action.action_value is None:
                return Decimal("1")
            prior_close = self._session.scalar(
                select(MarketOhlcv1D.close_px)
                .where(
                    MarketOhlcv1D.instrument_id == action.instrument_id,
                    MarketOhlcv1D.trade_date < action.ex_date,
                )
                .order_by(MarketOhlcv1D.trade_date.desc())
                .limit(1)
            )
            if prior_close is None or prior_close <= 0:
                return Decimal("1")
            ratio = (Decimal(prior_close) - Decimal(action.action_value)) / Decimal(
                prior_close
            )
            return ratio if ratio > 0 else Decimal("1")

        if action_type in {"MERGER", "DEMERGER", "DELISTING"}:
            return Decimal("1")

        return Decimal("1")

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    def _next_corporate_action_id(self) -> int:
        current_max = self._session.scalar(
            select(func.max(MarketCorporateAction.corp_action_id))
        )
        return 1 if current_max is None else int(current_max) + 1

    def _uses_sqlite(self) -> bool:
        bind = self._session.get_bind()
        return bind is not None and bind.dialect.name == "sqlite"
