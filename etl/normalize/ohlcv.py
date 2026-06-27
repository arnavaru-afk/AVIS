"""OHLCV normalization services built on the approved AVIS specs."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Protocol
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from avis.core.entity_resolution import CanonicalIdentityResolver, EntityNotFoundError
from avis.db.models import DqIncident, DqResult, DqRule, MarketCorporateAction, MarketOhlcv1D

IST = ZoneInfo("Asia/Kolkata")
INR = "INR"


class MissingFxRateError(LookupError):
    """Raised when a required FX conversion rate is not available."""


class FxRateProvider(Protocol):
    """Minimal interface for point-in-time FX lookup."""

    def get_rate(self, from_currency: str, to_currency: str, as_of_date: date) -> Decimal | None:
        """Return the FX rate or `None` when unavailable."""


@dataclass(slots=True)
class NormalizedOhlcvRecord:
    """Canonical normalized OHLCV record plus PIT audit timestamps."""

    instrument_id: int
    trade_date: date
    open_px: Decimal
    high_px: Decimal
    low_px: Decimal
    close_px: Decimal
    adj_close_px: Decimal
    volume: Decimal
    turnover: Decimal | None
    source_system: str
    source_record_id: str
    event_ts: datetime
    ingested_ts: datetime
    valid_from: datetime


class OhlcvNormalizationService:
    """Normalize raw EOD rows into curated `market_ohlcv_1d` records."""

    def __init__(
        self,
        *,
        session: Session,
        resolver: CanonicalIdentityResolver,
        fx_rate_provider: FxRateProvider | None = None,
        now_provider: Callable[[], datetime] | None = None,
    ) -> None:
        self._session = session
        self._resolver = resolver
        self._fx_rate_provider = fx_rate_provider
        self._now_provider = now_provider or self._utc_now

    def normalize_manifest(
        self,
        manifest_path: Path,
        *,
        exchange: str,
        source_system: str,
        currency_code: str = INR,
        adjustment_window_start: date | None = None,
    ) -> list[NormalizedOhlcvRecord]:
        """Load raw rows from the file-backed raw zone and normalize them."""

        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        payload_path = manifest_path.parent / manifest["current_payload_file"]
        payload = json.loads(payload_path.read_text(encoding="utf-8"))

        ingested_ts = datetime.fromisoformat(payload["written_at"])
        normalized: list[NormalizedOhlcvRecord] = []
        for index, raw_row in enumerate(payload["rows"]):
            record = self.normalize_row(
                raw_row,
                exchange=exchange,
                source_system=source_system,
                currency_code=currency_code,
                ingested_ts=ingested_ts,
                source_record_id=f"{payload_path.name}:{index}",
                adjustment_window_start=adjustment_window_start,
            )
            if record is None:
                continue
            self.upsert_record(record)
            normalized.append(record)
        self._session.flush()
        return normalized

    def normalize_row(
        self,
        raw_row: dict[str, Any],
        *,
        exchange: str,
        source_system: str,
        currency_code: str,
        ingested_ts: datetime,
        source_record_id: str,
        adjustment_window_start: date | None = None,
        event_ts: datetime | None = None,
    ) -> NormalizedOhlcvRecord | None:
        """Normalize one raw EOD row or quarantine it via DQ records."""

        trade_date = self._coerce_date(raw_row["timestamp"])
        normalized_source = source_system.strip().upper()
        event_ts = event_ts or self._default_event_ts(trade_date)
        valid_from = ingested_ts

        if event_ts > self._now_provider() + timedelta(hours=1):
            self._log_failure(
                rule_code="FUTURE_EVENT_TS",
                rule_name="Future event timestamp guard",
                target_record_key=self._target_key(raw_row),
                reason="future_event_ts",
                measured_value={"event_ts": event_ts.isoformat()},
            )
            return None

        try:
            instrument_id = self._resolver.resolve_as_of(
                str(raw_row["symbol"]),
                exchange,
                trade_date,
            )
        except EntityNotFoundError:
            self._log_failure(
                rule_code="UNRESOLVED_SYMBOL",
                rule_name="Unresolved point-in-time symbol",
                target_record_key=self._target_key(raw_row),
                reason="unresolved_symbol",
                measured_value={"exchange": exchange, "symbol": raw_row["symbol"]},
            )
            return None

        try:
            open_px = self._coerce_decimal(raw_row["open"])
            high_px = self._coerce_decimal(raw_row["high"])
            low_px = self._coerce_decimal(raw_row["low"])
            close_px = self._coerce_decimal(raw_row["close"])
            volume = self._coerce_decimal(raw_row["volume"])
            turnover = self._coerce_decimal(raw_row["turnover"]) if raw_row.get("turnover") else None
            converted = self._convert_currency(
                currency_code=currency_code,
                trade_date=trade_date,
                open_px=open_px,
                high_px=high_px,
                low_px=low_px,
                close_px=close_px,
                turnover=turnover,
            )
        except MissingFxRateError:
            self._log_failure(
                rule_code="MISSING_FX_RATE",
                rule_name="Missing FX rate for normalization",
                target_record_key=self._target_key(raw_row),
                reason="missing_fx_rate",
                measured_value={"currency_code": currency_code, "trade_date": trade_date.isoformat()},
            )
            return None
        except (KeyError, ValueError) as exc:
            self._log_failure(
                rule_code="INVALID_OHLCV_PAYLOAD",
                rule_name="Invalid OHLCV payload",
                target_record_key=self._target_key(raw_row),
                reason=str(exc),
                measured_value=raw_row,
            )
            return None

        open_px, high_px, low_px, close_px, turnover = converted

        if close_px <= 0:
            self._log_failure(
                rule_code="INVALID_OHLCV_PAYLOAD",
                rule_name="Invalid OHLCV payload",
                target_record_key=self._target_key(raw_row),
                reason="close_must_be_positive",
                measured_value=raw_row,
            )
            return None
        if high_px < low_px or high_px < open_px or high_px < close_px:
            self._log_failure(
                rule_code="INVALID_OHLCV_PAYLOAD",
                rule_name="Invalid OHLCV payload",
                target_record_key=self._target_key(raw_row),
                reason="high_bound_violation",
                measured_value=raw_row,
            )
            return None
        if low_px > open_px or low_px > close_px:
            self._log_failure(
                rule_code="INVALID_OHLCV_PAYLOAD",
                rule_name="Invalid OHLCV payload",
                target_record_key=self._target_key(raw_row),
                reason="low_bound_violation",
                measured_value=raw_row,
            )
            return None

        adj_close_px = self._adjusted_close_for_input(
            instrument_id=instrument_id,
            trade_date=trade_date,
            close_px=close_px,
            adjustment_window_start=adjustment_window_start,
        )

        if self._trips_circuit_breaker(
            instrument_id=instrument_id,
            trade_date=trade_date,
            adjusted_close=adj_close_px,
        ):
            self._log_failure(
                rule_code="ADJUSTED_CLOSE_CIRCUIT_BREAKER",
                rule_name="Adjusted close circuit breaker",
                target_record_key=self._target_key(raw_row),
                reason="adjusted_close_outlier",
                measured_value={"adjusted_close": str(adj_close_px)},
            )
            return None

        return NormalizedOhlcvRecord(
            instrument_id=instrument_id,
            trade_date=trade_date,
            open_px=open_px,
            high_px=high_px,
            low_px=low_px,
            close_px=close_px,
            adj_close_px=adj_close_px,
            volume=volume,
            turnover=turnover,
            source_system=normalized_source,
            source_record_id=source_record_id,
            event_ts=event_ts,
            ingested_ts=ingested_ts,
            valid_from=valid_from,
        )

    def upsert_record(self, record: NormalizedOhlcvRecord) -> MarketOhlcv1D:
        """Persist a normalized row and keep only the latest ingested version."""

        existing = self._session.scalar(
            select(MarketOhlcv1D).where(
                MarketOhlcv1D.instrument_id == record.instrument_id,
                MarketOhlcv1D.trade_date == record.trade_date,
            )
        )
        if existing is None:
            row = MarketOhlcv1D(
                instrument_id=record.instrument_id,
                trade_date=record.trade_date,
                open_px=record.open_px,
                high_px=record.high_px,
                low_px=record.low_px,
                close_px=record.close_px,
                adj_close_px=record.adj_close_px,
                volume=record.volume,
                turnover=record.turnover,
                source_system=record.source_system,
                source_record_id=record.source_record_id,
                ingested_at=self._db_datetime(record.ingested_ts),
            )
            self._assign_pk_if_sqlite(row, "ohlcv_1d_id", MarketOhlcv1D)
            self._session.add(row)
            self._session.flush()
            return row

        if self._comparable_dt(existing.ingested_at) >= self._comparable_dt(record.ingested_ts):
            return existing

        self._log_superseded_row(existing, record)
        existing.open_px = record.open_px
        existing.high_px = record.high_px
        existing.low_px = record.low_px
        existing.close_px = record.close_px
        existing.adj_close_px = record.adj_close_px
        existing.volume = record.volume
        existing.turnover = record.turnover
        existing.source_system = record.source_system
        existing.source_record_id = record.source_record_id
        existing.ingested_at = self._db_datetime(record.ingested_ts)
        self._session.flush()
        return existing

    def _convert_currency(
        self,
        *,
        currency_code: str,
        trade_date: date,
        open_px: Decimal,
        high_px: Decimal,
        low_px: Decimal,
        close_px: Decimal,
        turnover: Decimal | None,
    ) -> tuple[Decimal, Decimal, Decimal, Decimal, Decimal | None]:
        normalized_currency = currency_code.strip().upper()
        if normalized_currency == INR:
            return open_px, high_px, low_px, close_px, turnover
        if self._fx_rate_provider is None:
            raise MissingFxRateError(normalized_currency)
        fx_rate = self._fx_rate_provider.get_rate(normalized_currency, INR, trade_date)
        if fx_rate is None:
            raise MissingFxRateError(normalized_currency)
        return (
            open_px * fx_rate,
            high_px * fx_rate,
            low_px * fx_rate,
            close_px * fx_rate,
            None if turnover is None else turnover * fx_rate,
        )

    def _adjusted_close_for_input(
        self,
        *,
        instrument_id: int,
        trade_date: date,
        close_px: Decimal,
        adjustment_window_start: date | None,
    ) -> Decimal:
        factor = Decimal("1")
        action_stmt = (
            select(MarketCorporateAction)
            .where(
                MarketCorporateAction.instrument_id == instrument_id,
                MarketCorporateAction.ex_date > trade_date,
            )
            .order_by(
                MarketCorporateAction.ex_date.asc(),
                MarketCorporateAction.corp_action_id.asc(),
            )
        )
        for action in self._session.scalars(action_stmt):
            if adjustment_window_start is not None and action.ex_date < adjustment_window_start:
                continue
            factor *= self._factor_for_action(action)
        return (close_px * factor).quantize(Decimal("0.000001"))

    def _factor_for_action(self, action: MarketCorporateAction) -> Decimal:
        if action.action_type in {"SPLIT", "BONUS", "RIGHTS"}:
            if action.action_ratio_num in (None, Decimal("0")) or action.action_ratio_den is None:
                return Decimal("1")
            return Decimal(action.action_ratio_den) / Decimal(action.action_ratio_num)
        if action.action_type == "DIVIDEND":
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
            ratio = (Decimal(prior_close) - Decimal(action.action_value)) / Decimal(prior_close)
            return ratio if ratio > 0 else Decimal("1")
        return Decimal("1")

    def _trips_circuit_breaker(
        self,
        *,
        instrument_id: int,
        trade_date: date,
        adjusted_close: Decimal,
    ) -> bool:
        previous_row = self._session.scalar(
            select(MarketOhlcv1D)
            .where(
                MarketOhlcv1D.instrument_id == instrument_id,
                MarketOhlcv1D.trade_date < trade_date,
            )
            .order_by(MarketOhlcv1D.trade_date.desc())
            .limit(1)
        )
        if previous_row is None or previous_row.close_px <= 0:
            return False
        ratio = adjusted_close / Decimal(previous_row.close_px)
        return ratio > Decimal("5") or ratio < Decimal("0.2")

    def _log_superseded_row(
        self,
        existing: MarketOhlcv1D,
        incoming: NormalizedOhlcvRecord,
    ) -> None:
        rule = self._ensure_dq_rule(
            rule_code="SUPERSEDED_NORMALIZED_ROW",
            rule_name="Superseded normalized row",
            severity="LOW",
        )
        result = DqResult(
            dq_rule_id=rule.dq_rule_id,
            pipeline_run_id=None,
            target_table="market_ohlcv_1d",
            target_record_key=f"{incoming.instrument_id}:{incoming.trade_date.isoformat()}",
            status="WARN",
            failure_reason="superseded_row",
            measured_value=json.dumps(
                {
                    "existing_ingested_at": existing.ingested_at.isoformat(),
                    "incoming_ingested_at": incoming.ingested_ts.isoformat(),
                },
                sort_keys=True,
            ),
            threshold_value=None,
            evaluated_at=self._now_provider(),
        )
        self._assign_pk_if_sqlite(result, "dq_result_id", DqResult)
        self._session.add(result)
        self._session.flush()

    def _log_failure(
        self,
        *,
        rule_code: str,
        rule_name: str,
        target_record_key: str,
        reason: str,
        measured_value: dict[str, Any],
    ) -> None:
        rule = self._ensure_dq_rule(
            rule_code=rule_code,
            rule_name=rule_name,
            severity="HIGH",
        )
        result = DqResult(
            dq_rule_id=rule.dq_rule_id,
            pipeline_run_id=None,
            target_table="market_ohlcv_1d",
            target_record_key=target_record_key,
            status="FAIL",
            failure_reason=reason,
            measured_value=json.dumps(self._jsonify_row(measured_value), sort_keys=True),
            threshold_value=None,
            evaluated_at=self._now_provider(),
        )
        self._assign_pk_if_sqlite(result, "dq_result_id", DqResult)
        self._session.add(result)
        self._session.flush()

        incident = DqIncident(
            dq_result_id=result.dq_result_id,
            incident_status="OPEN",
            impact_level="MEDIUM",
            assigned_to="DATA_ENGINEERING",
            opened_at=self._now_provider(),
            resolved_at=None,
            resolution_notes=reason,
        )
        self._assign_pk_if_sqlite(incident, "dq_incident_id", DqIncident)
        self._session.add(incident)
        self._session.flush()

    def _ensure_dq_rule(self, *, rule_code: str, rule_name: str, severity: str) -> DqRule:
        existing = self._session.scalar(select(DqRule).where(DqRule.rule_code == rule_code))
        if existing is not None:
            return existing
        rule = DqRule(
            rule_code=rule_code,
            rule_name=rule_name,
            domain_name="NORMALIZATION",
            severity=severity,
            rule_expression=rule_name,
            is_active=True,
            owner_team="DATA_ENGINEERING",
            created_at=self._now_provider(),
            updated_at=self._now_provider(),
        )
        self._assign_pk_if_sqlite(rule, "dq_rule_id", DqRule)
        self._session.add(rule)
        self._session.flush()
        return rule

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
    def _default_event_ts(trade_date: date) -> datetime:
        return datetime.combine(trade_date, time(hour=15, minute=30), tzinfo=IST).astimezone(
            timezone.utc
        )

    @staticmethod
    def _db_datetime(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value
        return value.astimezone(timezone.utc).replace(tzinfo=None)

    @classmethod
    def _comparable_dt(cls, value: datetime) -> datetime:
        return cls._db_datetime(value)

    @staticmethod
    def _coerce_decimal(value: Any) -> Decimal:
        return Decimal(str(value))

    @staticmethod
    def _coerce_date(value: Any) -> date:
        if isinstance(value, date) and not isinstance(value, datetime):
            return value
        if isinstance(value, datetime):
            return value.date()
        return date.fromisoformat(str(value))

    @staticmethod
    def _target_key(raw_row: dict[str, Any]) -> str:
        symbol = str(raw_row.get("symbol", "UNKNOWN"))
        trade_date = str(raw_row.get("timestamp", "UNKNOWN"))
        return f"{symbol}:{trade_date}"

    @staticmethod
    def _jsonify_row(row: dict[str, Any]) -> dict[str, Any]:
        converted: dict[str, Any] = {}
        for key, value in row.items():
            if isinstance(value, dict):
                converted[key] = OhlcvNormalizationService._jsonify_row(value)
            elif hasattr(value, "isoformat"):
                converted[key] = value.isoformat()
            elif isinstance(value, uuid.UUID):
                converted[key] = str(value)
            else:
                converted[key] = None if value is None else str(value)
        return converted

    @staticmethod
    def _utc_now() -> datetime:
        return datetime.now(timezone.utc)
