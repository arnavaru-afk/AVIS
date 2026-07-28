"""Bootstrap fundamentals pipeline using yfinance as a development seed source."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Callable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from avis.db.models import FundStatementFact, OpsDataLineage, OpsJobEvent, OpsPipelineRun, RefInstrument
from etl.fundamentals.metrics import FundamentalsMetricBuilder
from etl.fundamentals.parser import ParsedStatementFact, TOTAL_REVENUE, YFinanceStatementParser

try:
    import yfinance as yf
except ImportError:  # pragma: no cover - exercised in environments without yfinance installed
    yf = None

YFinanceTickerFactory = Callable[[str], Any]


@dataclass(frozen=True, slots=True)
class FundamentalsRunResult:
    instrument_id: int
    symbol: str
    source_symbol: str
    pipeline_run_id: int
    statements_seen: int
    statements_stored: int
    metrics_computed: int
    skipped: bool
    skip_reason: str | None


class FundamentalsPipeline:
    """Fetch, validate, store, and derive annual fundamentals for one instrument."""

    pipeline_name = "fundamentals_bootstrap"
    job_name = "fundamentals_yfinance"

    def __init__(
        self,
        *,
        session: Session,
        parser: YFinanceStatementParser | None = None,
        metric_builder: FundamentalsMetricBuilder | None = None,
        ticker_factory: YFinanceTickerFactory | None = None,
        now_provider: Callable[[], datetime] | None = None,
    ) -> None:
        self._session = session
        self._parser = parser or YFinanceStatementParser(now_provider=now_provider)
        self._metric_builder = metric_builder or FundamentalsMetricBuilder(session, now_provider=now_provider)
        self._ticker_factory = ticker_factory or self._default_ticker_factory
        self._now_provider = now_provider or _utc_now

    def run(self, instrument_id: int, symbol: str) -> FundamentalsRunResult:
        pipeline_run = self._create_pipeline_run(instrument_id=instrument_id, symbol=symbol)
        source_symbol = symbol
        self._emit_job_event(pipeline_run.pipeline_run_id, event_type="START", message=f"bootstrap_start instrument_id={instrument_id}")
        try:
            instrument = self._session.get(RefInstrument, instrument_id)
            if instrument is None:
                raise ValueError(f"Unknown instrument_id={instrument_id}")
            source_symbol = self._yfinance_symbol(instrument)
            ticker = self._ticker_factory(source_symbol)
            income_stmt = getattr(ticker, "income_stmt", None)
            balance_sheet = getattr(ticker, "balance_sheet", None)
            cashflow = getattr(ticker, "cashflow", None)
            info = getattr(ticker, "info", {}) or {}

            parsed_rows = self._parser.parse(
                instrument_id=instrument_id,
                income_stmt=income_stmt,
                balance_sheet=balance_sheet,
                cashflow=cashflow,
                info=info,
            )
            valid_rows, skipped_periods = self._validated_rows(parsed_rows)
            for reason in skipped_periods:
                self._emit_job_event(pipeline_run.pipeline_run_id, event_type="WARN", message=reason)

            if not valid_rows:
                raise ValueError("No annual periods with non-zero total_revenue")

            statements_stored = self._upsert_statement_rows(valid_rows)
            statement_rows = list(
                self._session.scalars(
                    select(FundStatementFact)
                    .where(FundStatementFact.instrument_id == instrument_id, FundStatementFact.fiscal_period == "FY")
                    .order_by(FundStatementFact.as_reported_ts.asc(), FundStatementFact.statement_fact_id.asc())
                )
            )
            metric_rows = self._metric_builder.build_rows(
                instrument_id=instrument_id,
                statement_facts=statement_rows,
                info=info,
            )
            metrics_computed = self._metric_builder.upsert_rows(metric_rows)
            self._emit_lineage(
                pipeline_run_id=pipeline_run.pipeline_run_id,
                source_asset=f"yfinance:{source_symbol}",
                target_asset="fund_statement_fact+fund_metric_fact",
                transform_id="fundamentals_bootstrap_v1",
                record_count_in=len(valid_rows),
                record_count_out=statements_stored + metrics_computed,
            )
            self._emit_job_event(
                pipeline_run.pipeline_run_id,
                event_type="END",
                message=f"bootstrap_complete statements={statements_stored} metrics={metrics_computed}",
                metrics_payload={
                    "source_symbol": source_symbol,
                    "statements_stored": statements_stored,
                    "metrics_computed": metrics_computed,
                    "statements_seen": len(parsed_rows),
                },
            )
            self._finish_pipeline_run(pipeline_run, status="SUCCESS")
            return FundamentalsRunResult(
                instrument_id=instrument_id,
                symbol=symbol,
                source_symbol=source_symbol,
                pipeline_run_id=pipeline_run.pipeline_run_id,
                statements_seen=len(parsed_rows),
                statements_stored=statements_stored,
                metrics_computed=metrics_computed,
                skipped=False,
                skip_reason=None,
            )
        except Exception as exc:
            self._emit_job_event(
                pipeline_run.pipeline_run_id,
                event_type="ERROR",
                message=f"bootstrap_failed source_symbol={source_symbol}: {exc}",
            )
            self._finish_pipeline_run(pipeline_run, status="FAILED")
            return FundamentalsRunResult(
                instrument_id=instrument_id,
                symbol=symbol,
                source_symbol=source_symbol,
                pipeline_run_id=pipeline_run.pipeline_run_id,
                statements_seen=0,
                statements_stored=0,
                metrics_computed=0,
                skipped=True,
                skip_reason=str(exc),
            )

    def _validated_rows(self, rows: list[ParsedStatementFact]) -> tuple[list[ParsedStatementFact], list[str]]:
        by_period: dict[datetime, list[ParsedStatementFact]] = {}
        for row in rows:
            by_period.setdefault(row.as_reported_ts, []).append(row)

        valid_rows: list[ParsedStatementFact] = []
        skipped: list[str] = []
        for period_ts, period_rows in sorted(by_period.items()):
            revenue_row = next((row for row in period_rows if row.line_item_code == TOTAL_REVENUE), None)
            if revenue_row is None or revenue_row.value is None or revenue_row.value == 0:
                skipped.append(f"skipped_period period_end={period_ts.date().isoformat()} reason=missing_or_zero_total_revenue")
                continue
            valid_rows.extend(period_rows)
        return valid_rows, skipped

    def _upsert_statement_rows(self, rows: list[ParsedStatementFact]) -> int:
        stored = 0
        for row in rows:
            if row.value is None:
                continue
            existing = self._session.scalar(
                select(FundStatementFact).where(
                    FundStatementFact.instrument_id == row.instrument_id,
                    FundStatementFact.fiscal_year == row.fiscal_year,
                    FundStatementFact.fiscal_period == row.fiscal_period,
                    FundStatementFact.statement_type == row.statement_type,
                    FundStatementFact.line_item_code == row.line_item_code,
                )
            )
            if existing is None:
                existing = FundStatementFact(
                    instrument_id=row.instrument_id,
                    fiscal_year=row.fiscal_year,
                    fiscal_period=row.fiscal_period,
                    statement_type=row.statement_type,
                    line_item_code=row.line_item_code,
                    value=row.value,
                    currency_code=row.currency_code,
                    scale_code=row.scale_code,
                    as_reported_ts=row.as_reported_ts,
                    effective_from_ts=row.effective_from_ts,
                    effective_to_ts=row.effective_to_ts,
                    source_system=row.source_system,
                    source_record_id=row.source_record_id,
                    ingested_at=row.ingested_at,
                )
                _assign_pk_if_sqlite(self._session, existing, "statement_fact_id", FundStatementFact)
                self._session.add(existing)
            else:
                existing.value = row.value
                existing.currency_code = row.currency_code
                existing.scale_code = row.scale_code
                existing.as_reported_ts = row.as_reported_ts
                existing.effective_from_ts = row.effective_from_ts
                existing.effective_to_ts = row.effective_to_ts
                existing.source_system = row.source_system
                existing.source_record_id = row.source_record_id
                existing.ingested_at = row.ingested_at
            stored += 1
        self._session.flush()
        return stored

    def _create_pipeline_run(self, *, instrument_id: int, symbol: str) -> OpsPipelineRun:
        run = OpsPipelineRun(
            run_uuid=uuid.uuid4(),
            pipeline_name=self.pipeline_name,
            run_mode="MANUAL",
            triggered_by="SYSTEM",
            status="RUNNING",
            started_at=self._now_provider(),
            ended_at=None,
            run_context={"instrument_id": instrument_id, "symbol": symbol},
        )
        _assign_pk_if_sqlite(self._session, run, "pipeline_run_id", OpsPipelineRun)
        self._session.add(run)
        self._session.flush()
        return run

    def _finish_pipeline_run(self, pipeline_run: OpsPipelineRun, *, status: str) -> None:
        pipeline_run.status = status
        pipeline_run.ended_at = self._now_provider()
        self._session.flush()

    def _emit_job_event(
        self,
        pipeline_run_id: int,
        *,
        event_type: str,
        message: str,
        metrics_payload: dict[str, Any] | None = None,
    ) -> None:
        event = OpsJobEvent(
            pipeline_run_id=pipeline_run_id,
            job_name=self.job_name,
            event_type=event_type,
            event_ts=self._now_provider(),
            message=message,
            metrics_payload=metrics_payload,
        )
        _assign_pk_if_sqlite(self._session, event, "job_event_id", OpsJobEvent)
        self._session.add(event)
        self._session.flush()

    def _emit_lineage(
        self,
        *,
        pipeline_run_id: int,
        source_asset: str,
        target_asset: str,
        transform_id: str,
        record_count_in: int,
        record_count_out: int,
    ) -> None:
        lineage = OpsDataLineage(
            pipeline_run_id=pipeline_run_id,
            source_asset=source_asset,
            target_asset=target_asset,
            transform_id=transform_id,
            record_count_in=record_count_in,
            record_count_out=record_count_out,
            lineage_ts=self._now_provider(),
        )
        _assign_pk_if_sqlite(self._session, lineage, "lineage_id", OpsDataLineage)
        self._session.add(lineage)
        self._session.flush()

    @staticmethod
    def _yfinance_symbol(instrument: RefInstrument) -> str:
        exchange_code = instrument.exchange.exchange_code if instrument.exchange is not None else None
        if exchange_code == "NSE":
            return f"{instrument.symbol}.NS"
        if exchange_code == "BSE":
            return f"{instrument.symbol}.BO"
        return instrument.symbol

    @staticmethod
    def _default_ticker_factory(symbol: str) -> Any:
        if yf is None:
            raise RuntimeError("yfinance is not installed")
        return yf.Ticker(symbol)


def _assign_pk_if_sqlite(session: Session, instance: Any, pk_name: str, model: type[Any]) -> None:
    bind = session.get_bind()
    if bind is None or bind.dialect.name != "sqlite":
        return
    if getattr(instance, pk_name, None) is not None:
        return
    current_max = session.scalar(select(func.max(getattr(model, pk_name))))
    setattr(instance, pk_name, 1 if current_max is None else int(current_max) + 1)


def _utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)
