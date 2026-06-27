"""Versioned DQ rule registry and quality gate orchestration."""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, is_dataclass
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from avis.db.models import (
    AuthUser,
    DqIncident,
    DqOverride,
    DqResult,
    DqRule,
    MarketOhlcv1D,
    OpsDataLineage,
    OpsJobEvent,
    OpsPipelineRun,
)

Severity = str
RuleCheckFn = Callable[[Any, "RuleContext"], "RuleEvaluation"]


@dataclass(frozen=True, slots=True)
class RuleDefinition:
    """Declarative rule definition stored in the rule registry."""

    name: str
    domain: str
    severity: Severity
    version: int
    check_fn: RuleCheckFn
    quarantine_on_fail: bool = False

    @property
    def rule_code(self) -> str:
        return f"{self.name.upper()}_V{self.version}"


@dataclass(frozen=True, slots=True)
class RuleEvaluation:
    """Result returned by a rule check function."""

    passed: bool
    failure_reason: str | None = None
    measured_value: str | None = None
    threshold_value: str | None = None


@dataclass(frozen=True, slots=True)
class RuleContext:
    """Context made available to every rule invocation."""

    session: Session
    pipeline_run_id: int | None
    source_table: str
    target_table: str
    now: datetime
    non_nullable_fields: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RecordDecision:
    """Outcome of processing a single record through the quality gate."""

    allowed: bool
    quarantined: bool
    quarantined_path: Path | None
    blocking_rule_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BatchDecision:
    """Aggregated batch outcome with lineage counts."""

    row_count_in: int
    row_count_out: int
    failed_count: int


class RuleRegistry:
    """Owns rule declarations and syncs their versions into `dq_rule`."""

    def __init__(self, session: Session, *, owner_team: str = "DATA_ENGINEERING") -> None:
        self._session = session
        self._owner_team = owner_team
        self._definitions: dict[str, RuleDefinition] = {}

    def register(self, definition: RuleDefinition) -> None:
        self._definitions[definition.rule_code] = definition

    def register_many(self, definitions: Iterable[RuleDefinition]) -> None:
        for definition in definitions:
            self.register(definition)

    def definitions(self) -> tuple[RuleDefinition, ...]:
        return tuple(self._definitions.values())

    def sync(self) -> dict[str, DqRule]:
        catalog: dict[str, DqRule] = {}
        for definition in self.definitions():
            for existing in self._session.scalars(select(DqRule).where(DqRule.rule_name == definition.name)):
                if existing.rule_code != definition.rule_code and existing.is_active:
                    existing.is_active = False
                    existing.updated_at = _utc_now()

            entry = self._session.scalar(
                select(DqRule).where(DqRule.rule_code == definition.rule_code)
            )
            expression = json.dumps(
                {
                    "name": definition.name,
                    "version": definition.version,
                    "severity": definition.severity,
                    "quarantine_on_fail": definition.quarantine_on_fail,
                },
                sort_keys=True,
            )
            if entry is None:
                entry = DqRule(
                    rule_code=definition.rule_code,
                    rule_name=definition.name,
                    domain_name=definition.domain,
                    severity=definition.severity,
                    rule_expression=expression,
                    is_active=True,
                    owner_team=self._owner_team,
                    created_at=_utc_now(),
                    updated_at=_utc_now(),
                )
                _assign_pk_if_sqlite(self._session, entry, "dq_rule_id", DqRule)
                self._session.add(entry)
            else:
                entry.domain_name = definition.domain
                entry.severity = definition.severity
                entry.rule_expression = expression
                entry.is_active = True
                entry.updated_at = _utc_now()
            self._session.flush()
            catalog[definition.rule_code] = entry
        return catalog


class QualityGateEngine:
    """Evaluates DQ rules, routes quarantines, and emits ops lineage."""

    def __init__(
        self,
        *,
        session: Session,
        quarantine_root: Path,
        owner_team: str = "DATA_ENGINEERING",
        now_provider: Callable[[], datetime] | None = None,
    ) -> None:
        self._session = session
        self._quarantine_root = quarantine_root
        self._now_provider = now_provider or _utc_now
        self._registry = RuleRegistry(session, owner_team=owner_team)
        self._registry.register_many(build_builtin_rules())
        self._catalog = self._registry.sync()

    @property
    def registry(self) -> RuleRegistry:
        return self._registry

    def evaluate_record(
        self,
        record: Any,
        *,
        pipeline_run_id: int | None,
        source_table: str,
        target_table: str,
        non_nullable_fields: Iterable[str] = (),
        persist_fn: Callable[[Any], Any] | None = None,
    ) -> RecordDecision:
        context = RuleContext(
            session=self._session,
            pipeline_run_id=pipeline_run_id,
            source_table=source_table,
            target_table=target_table,
            now=self._now_provider(),
            non_nullable_fields=tuple(non_nullable_fields),
        )

        blocking_rule_codes: list[str] = []
        quarantine_path: Path | None = None
        quarantined = False

        for definition in self._registry.definitions():
            evaluation = definition.check_fn(record, context)
            dq_result = self._store_result(definition, context, record, evaluation)
            if evaluation.passed:
                continue

            if definition.severity == "CRITICAL":
                blocking_rule_codes.append(definition.rule_code)
                self._open_incident(dq_result, impact_level="CRITICAL", resolution_notes=evaluation.failure_reason)
                self._emit_job_event(
                    pipeline_run_id,
                    job_name=target_table,
                    event_type="ERROR",
                    message=f"critical_quality_failure rule={definition.rule_code} key={self._record_key(record)}",
                )
            elif definition.severity == "HIGH":
                blocking_rule_codes.append(definition.rule_code)
                self._open_incident(dq_result, impact_level="HIGH", resolution_notes=evaluation.failure_reason)
                quarantine_path = self._write_quarantine_record(
                    definition=definition,
                    source_table=source_table,
                    target_table=target_table,
                    record=record,
                )
                quarantined = True
                self._emit_job_event(
                    pipeline_run_id,
                    job_name=target_table,
                    event_type="WARN",
                    message=f"quarantined_record rule={definition.rule_code} key={self._record_key(record)}",
                )
            elif definition.severity in {"MEDIUM", "LOW"}:
                self._emit_job_event(
                    pipeline_run_id,
                    job_name=target_table,
                    event_type="WARN",
                    message=f"quality_warning rule={definition.rule_code} key={self._record_key(record)}",
                )

        allowed = not blocking_rule_codes
        if allowed and persist_fn is not None:
            persist_fn(record)
            self._session.flush()

        return RecordDecision(
            allowed=allowed,
            quarantined=quarantined,
            quarantined_path=quarantine_path,
            blocking_rule_codes=tuple(blocking_rule_codes),
        )

    def process_records(
        self,
        records: Iterable[Any],
        *,
        pipeline_run_id: int,
        source_table: str,
        target_table: str,
        transform_id: str,
        non_nullable_fields: Iterable[str] = (),
        persist_fn: Callable[[Any], Any] | None = None,
    ) -> BatchDecision:
        records_list = list(records)
        row_count_out = 0
        for record in records_list:
            decision = self.evaluate_record(
                record,
                pipeline_run_id=pipeline_run_id,
                source_table=source_table,
                target_table=target_table,
                non_nullable_fields=non_nullable_fields,
                persist_fn=persist_fn,
            )
            if decision.allowed:
                row_count_out += 1

        failed_count = len(records_list) - row_count_out
        self.emit_lineage(
            pipeline_run_id=pipeline_run_id,
            source_table=source_table,
            target_table=target_table,
            transform_id=transform_id,
            row_count_in=len(records_list),
            failed_count=failed_count,
        )
        return BatchDecision(
            row_count_in=len(records_list),
            row_count_out=row_count_out,
            failed_count=failed_count,
        )

    def emit_lineage(
        self,
        *,
        pipeline_run_id: int,
        source_table: str,
        target_table: str,
        transform_id: str,
        row_count_in: int,
        failed_count: int,
        ts: datetime | None = None,
    ) -> OpsDataLineage:
        lineage = OpsDataLineage(
            pipeline_run_id=pipeline_run_id,
            source_asset=source_table,
            target_asset=target_table,
            transform_id=transform_id,
            record_count_in=row_count_in,
            record_count_out=max(row_count_in - failed_count, 0),
            lineage_ts=_db_datetime(ts or self._now_provider()),
        )
        _assign_pk_if_sqlite(self._session, lineage, "lineage_id", OpsDataLineage)
        self._session.add(lineage)
        self._session.flush()
        return lineage

    def record_override(
        self,
        *,
        incident_id: int,
        approved_by_user_id: int,
        justification: str,
        expiry: datetime | None = None,
    ) -> DqOverride:
        justification_text = justification.strip()
        if len(justification_text) < 10:
            raise ValueError("Override justification must be at least 10 characters")
        incident = self._session.get(DqIncident, incident_id)
        if incident is None:
            raise ValueError(f"Unknown DQ incident: {incident_id}")
        approver = self._session.get(AuthUser, approved_by_user_id)
        if approver is None:
            raise ValueError(f"Unknown approver: {approved_by_user_id}")

        override = DqOverride(
            dq_incident_id=incident_id,
            approved_by_user_id=approved_by_user_id,
            override_reason=justification_text,
            override_expiry_ts=_db_datetime(expiry) if expiry is not None else None,
            created_at=_db_datetime(self._now_provider()),
        )
        _assign_pk_if_sqlite(self._session, override, "dq_override_id", DqOverride)
        self._session.add(override)
        incident.incident_status = "RESOLVED"
        incident.resolution_notes = justification_text
        incident.resolved_at = _db_datetime(self._now_provider())
        self._session.flush()
        return override

    def _store_result(
        self,
        definition: RuleDefinition,
        context: RuleContext,
        record: Any,
        evaluation: RuleEvaluation,
    ) -> DqResult:
        status = "PASS"
        if not evaluation.passed:
            status = "FAIL" if definition.severity in {"HIGH", "CRITICAL"} else "WARN"

        dq_result = DqResult(
            dq_rule_id=self._catalog[definition.rule_code].dq_rule_id,
            pipeline_run_id=context.pipeline_run_id,
            target_table=context.target_table,
            target_record_key=self._record_key(record),
            status=status,
            failure_reason=evaluation.failure_reason,
            measured_value=_truncate(evaluation.measured_value),
            threshold_value=_truncate(evaluation.threshold_value),
            evaluated_at=_db_datetime(context.now),
        )
        _assign_pk_if_sqlite(self._session, dq_result, "dq_result_id", DqResult)
        self._session.add(dq_result)
        self._session.flush()
        return dq_result

    def _open_incident(
        self,
        dq_result: DqResult,
        *,
        impact_level: str,
        resolution_notes: str | None,
    ) -> DqIncident:
        incident = DqIncident(
            dq_result_id=dq_result.dq_result_id,
            incident_status="OPEN",
            impact_level=impact_level,
            assigned_to="DATA_ENGINEERING",
            opened_at=_db_datetime(self._now_provider()),
            resolved_at=None,
            resolution_notes=resolution_notes,
        )
        _assign_pk_if_sqlite(self._session, incident, "dq_incident_id", DqIncident)
        self._session.add(incident)
        self._session.flush()
        return incident

    def _write_quarantine_record(
        self,
        *,
        definition: RuleDefinition,
        source_table: str,
        target_table: str,
        record: Any,
    ) -> Path:
        record_key = self._record_key(record).replace("/", "_")
        base_path = self._quarantine_root / target_table / definition.rule_code
        base_path.mkdir(parents=True, exist_ok=True)
        output_path = base_path / f"{record_key}.json"
        output_path.write_text(
            json.dumps(
                {
                    "rule_code": definition.rule_code,
                    "source_table": source_table,
                    "target_table": target_table,
                    "record_key": self._record_key(record),
                    "captured_at": self._now_provider().isoformat(),
                    "record": _jsonify(record),
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        return output_path

    def _emit_job_event(
        self,
        pipeline_run_id: int | None,
        *,
        job_name: str,
        event_type: str,
        message: str,
    ) -> OpsJobEvent | None:
        if pipeline_run_id is None:
            return None
        event = OpsJobEvent(
            pipeline_run_id=pipeline_run_id,
            job_name=job_name,
            event_type=event_type,
            event_ts=_db_datetime(self._now_provider()),
            message=message,
            metrics_payload=None,
        )
        _assign_pk_if_sqlite(self._session, event, "job_event_id", OpsJobEvent)
        self._session.add(event)
        self._session.flush()
        return event

    @staticmethod
    def _record_key(record: Any) -> str:
        instrument_id = _value(record, "instrument_id")
        trade_date = _value(record, "trade_date") or _value(record, "timestamp")
        source = _value(record, "source_system") or _value(record, "source")
        if instrument_id is None:
            symbol = _value(record, "symbol") or "UNKNOWN"
            return f"{symbol}:{trade_date}"
        return f"{instrument_id}:{trade_date}:{source}"


def build_builtin_rules() -> tuple[RuleDefinition, ...]:
    return (
        RuleDefinition(
            name="null_check",
            domain="MARKET",
            severity="HIGH",
            version=1,
            check_fn=_null_check,
            quarantine_on_fail=True,
        ),
        RuleDefinition(
            name="range_check",
            domain="MARKET",
            severity="HIGH",
            version=1,
            check_fn=_range_check,
            quarantine_on_fail=True,
        ),
        RuleDefinition(
            name="stale_check",
            domain="MARKET",
            severity="HIGH",
            version=1,
            check_fn=_stale_check,
            quarantine_on_fail=True,
        ),
        RuleDefinition(
            name="duplicate_check",
            domain="MARKET",
            severity="HIGH",
            version=1,
            check_fn=_duplicate_check,
            quarantine_on_fail=True,
        ),
        RuleDefinition(
            name="symbol_resolved_check",
            domain="MARKET",
            severity="HIGH",
            version=1,
            check_fn=_symbol_resolved_check,
            quarantine_on_fail=True,
        ),
        RuleDefinition(
            name="circuit_breaker",
            domain="MARKET",
            severity="CRITICAL",
            version=1,
            check_fn=_circuit_breaker_check,
            quarantine_on_fail=False,
        ),
    )


def _null_check(record: Any, context: RuleContext) -> RuleEvaluation:
    missing = [field for field in context.non_nullable_fields if _value(record, field) is None]
    if not missing:
        return RuleEvaluation(passed=True)
    return RuleEvaluation(
        passed=False,
        failure_reason=f"null_fields={','.join(missing)}",
        measured_value="NULL",
        threshold_value="NOT_NULL",
    )


def _range_check(record: Any, _context: RuleContext) -> RuleEvaluation:
    fields = ("open_px", "high_px", "low_px", "close_px")
    fallback_fields = {"open_px": "open", "high_px": "high", "low_px": "low", "close_px": "close"}
    for field in fields:
        value = _value(record, field)
        if value is None:
            value = _value(record, fallback_fields[field])
        if value is None:
            continue
        price = Decimal(str(value))
        if price < Decimal("0.01") or price > Decimal("1000000"):
            return RuleEvaluation(
                passed=False,
                failure_reason=f"price_out_of_range field={field}",
                measured_value=str(price),
                threshold_value="[0.01,1000000]",
            )
    return RuleEvaluation(passed=True)


def _stale_check(record: Any, context: RuleContext) -> RuleEvaluation:
    event_ts = _value(record, "event_ts")
    if event_ts is None:
        return RuleEvaluation(passed=True)
    event_dt = _coerce_datetime(event_ts)
    threshold = _subtract_trading_days(context.now.date(), 5)
    if event_dt.date() < threshold:
        return RuleEvaluation(
            passed=False,
            failure_reason="event_ts_stale",
            measured_value=event_dt.isoformat(),
            threshold_value=threshold.isoformat(),
        )
    return RuleEvaluation(passed=True)


def _duplicate_check(record: Any, context: RuleContext) -> RuleEvaluation:
    instrument_id = _value(record, "instrument_id")
    trade_date = _value(record, "trade_date")
    source_system = _value(record, "source_system") or _value(record, "source")
    if instrument_id is None or trade_date is None or source_system is None:
        return RuleEvaluation(passed=True)
    existing = context.session.scalar(
        select(MarketOhlcv1D).where(
            MarketOhlcv1D.instrument_id == instrument_id,
            MarketOhlcv1D.trade_date == _coerce_date(trade_date),
            MarketOhlcv1D.source_system == str(source_system),
        )
    )
    if existing is None:
        return RuleEvaluation(passed=True)
    return RuleEvaluation(
        passed=False,
        failure_reason="duplicate_curated_row",
        measured_value=f"{instrument_id}:{trade_date}:{source_system}",
        threshold_value="UNIQUE",
    )


def _symbol_resolved_check(record: Any, _context: RuleContext) -> RuleEvaluation:
    instrument_id = _value(record, "instrument_id")
    if instrument_id is not None:
        return RuleEvaluation(passed=True)
    return RuleEvaluation(
        passed=False,
        failure_reason="instrument_id_unresolved",
        measured_value="NULL",
        threshold_value="NOT_NULL",
    )


def _circuit_breaker_check(record: Any, context: RuleContext) -> RuleEvaluation:
    instrument_id = _value(record, "instrument_id")
    trade_date = _value(record, "trade_date")
    close_px = _value(record, "close_px")
    if instrument_id is None or trade_date is None or close_px is None:
        return RuleEvaluation(passed=True)
    previous_close = context.session.scalar(
        select(MarketOhlcv1D.close_px)
        .where(
            MarketOhlcv1D.instrument_id == instrument_id,
            MarketOhlcv1D.trade_date < _coerce_date(trade_date),
        )
        .order_by(MarketOhlcv1D.trade_date.desc())
        .limit(1)
    )
    if previous_close is None or previous_close == 0:
        return RuleEvaluation(passed=True)
    daily_return = (Decimal(str(close_px)) - Decimal(previous_close)) / Decimal(previous_close)
    if daily_return > Decimal("0.5") or daily_return < Decimal("-0.5"):
        return RuleEvaluation(
            passed=False,
            failure_reason="circuit_breaker_triggered",
            measured_value=str(daily_return.quantize(Decimal("0.0001"))),
            threshold_value="[-0.5,0.5]",
        )
    return RuleEvaluation(passed=True)


def _value(record: Any, field: str) -> Any:
    if isinstance(record, Mapping):
        return record.get(field)
    return getattr(record, field, None)


def _jsonify(record: Any) -> dict[str, Any]:
    if isinstance(record, Mapping):
        values = record.items()
    elif is_dataclass(record):
        values = asdict(record).items()
    else:
        values = vars(record).items()
    result: dict[str, Any] = {}
    for key, value in values:
        result[key] = _jsonify_value(value)
    return result


def _jsonify_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _jsonify_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonify_value(item) for item in value]
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, (uuid.UUID, Decimal)):
        return str(value)
    return value


def _subtract_trading_days(as_of_date: date, trading_days: int) -> date:
    current = as_of_date
    remaining = trading_days
    while remaining > 0:
        current -= timedelta(days=1)
        if current.weekday() < 5:
            remaining -= 1
    return current


def _coerce_date(value: Any) -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    return date.fromisoformat(str(value))


def _coerce_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


def _truncate(value: str | None, limit: int = 256) -> str | None:
    if value is None:
        return None
    return value if len(value) <= limit else value[: limit - 3] + "..."


def _db_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _assign_pk_if_sqlite(session: Session, instance: Any, pk_name: str, model: type[Any]) -> None:
    bind = session.get_bind()
    if bind is None or bind.dialect.name != "sqlite":
        return
    if getattr(instance, pk_name, None) is not None:
        return
    pk_column = getattr(model, pk_name)
    current_max = session.scalar(select(func.max(pk_column)))
    setattr(instance, pk_name, 1 if current_max is None else int(current_max) + 1)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)
