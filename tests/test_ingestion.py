from __future__ import annotations

import io
import json
import zipfile
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

from apps.scheduler.ingestion import IngestionSchedulerPolicy
from apps.scheduler.runtime import SchedulerRuntime
from avis.db.base import Base
from avis.db.models import DqIncident, DqResult, DqRule, OpsJobEvent, OpsPipelineRun, OpsSlaBreach
from etl.ingest.base import BaseConnector, RawWriteResult, RetryPolicy
from etl.ingest.connectors.bse_eod import BseEodConnector
from etl.ingest.connectors.nse_eod import NseEodConnector


class _ContractDriftConnector(BaseConnector):
    source_system = "TEST_EOD"
    pipeline_name = "test_eod_ingest"
    job_name = "test_contract_connector"

    def __init__(self, *, session: Session, raw_zone_root: Path, contract_path: Path) -> None:
        super().__init__(session=session, raw_zone_root=raw_zone_root, contract_path=contract_path)

    def fetch(self, trade_date: date) -> bytes:
        return b"payload"

    def validate(self, payload: bytes, trade_date: date) -> list[dict[str, object]]:
        return [
            {
                "symbol": "ITC",
                "series": "EQ",
                "open": Decimal("100"),
                "high": Decimal("101"),
                "low": Decimal("99"),
                "close": Decimal("100"),
                "volume": Decimal("1000"),
                "delivery_qty": Decimal("500"),
                "timestamp": trade_date,
            },
            {
                "symbol": "BAD",
                "series": "EQ",
                "open": Decimal("100"),
                "high": Decimal("101"),
                "low": Decimal("99"),
                "close": Decimal("100"),
                "volume": "not-a-number",
                "delivery_qty": Decimal("500"),
                "timestamp": trade_date,
            },
        ]

    def write_raw(
        self,
        rows: list[dict[str, object]],
        payload: bytes,
        trade_date: date,
        pipeline_run_id: int,
    ) -> RawWriteResult:
        return self._write_raw_payload(
            rows=rows,
            payload=payload,
            trade_date=trade_date,
            source_system=self.source_system,
        )


class _TransientFailureConnector(_ContractDriftConnector):
    source_system = "TRANSIENT_EOD"

    def __init__(self, *, session: Session, raw_zone_root: Path, contract_path: Path) -> None:
        super().__init__(session=session, raw_zone_root=raw_zone_root, contract_path=contract_path)
        self._retry_policy = RetryPolicy(max_attempts=2, initial_delay_seconds=0)
        self._sleep_fn = lambda _delay: None

    def fetch(self, trade_date: date) -> bytes:
        raise TimeoutError("source unavailable")


def test_nse_validation_checksum_and_idempotency(tmp_path: Path) -> None:
    with _session_with_tables() as session:
        connector = NseEodConnector(
            session=session,
            raw_zone_root=tmp_path / "raw",
            contract_path=Path("data/contracts/nse_eod.json"),
            fetcher=lambda _url: _nse_csv_payload(),
        )
        trade_date = date(2026, 6, 26)

        result_one = connector.run(trade_date)
        result_two = connector.run(trade_date)

        assert result_one.row_count == 2
        assert result_one.created is True
        assert result_two.created is False
        assert result_two.updated is False
        assert result_one.checksum == connector.compute_checksum(_nse_csv_payload())

        payload_files = list((tmp_path / "raw" / "NSE_EOD" / trade_date.isoformat()).glob("*.json"))
        assert len([path for path in payload_files if path.name != "manifest.json"]) == 1


def test_bse_zip_payload_and_isin_mapping(tmp_path: Path) -> None:
    with _session_with_tables() as session:
        connector = BseEodConnector(
            session=session,
            raw_zone_root=tmp_path / "raw",
            contract_path=Path("data/contracts/bse_eod.json"),
            scrip_code_to_isin={"500875": "INE154A01025"},
            fetcher=lambda _url: _bse_zip_payload(),
        )

        result = connector.run(date(2026, 6, 26))

        assert result.row_count == 1
        assert result.created is True


def test_contract_drift_rows_are_quarantined(tmp_path: Path) -> None:
    with _session_with_tables() as session:
        connector = _ContractDriftConnector(
            session=session,
            raw_zone_root=tmp_path / "raw",
            contract_path=Path("data/contracts/nse_eod.json"),
        )

        result = connector.run(date(2026, 6, 26))

        assert result.row_count == 1
        incidents = list(session.scalars(select(DqIncident)))
        results = list(session.scalars(select(DqResult)))
        assert len(incidents) == 1
        assert len(results) == 1
        assert results[0].failure_reason == "type_mismatch field=volume expected=decimal"


def test_end_to_end_fetch_raw_write_and_job_events(tmp_path: Path) -> None:
    with _session_with_tables(include_sla=True) as session:
        connector = NseEodConnector(
            session=session,
            raw_zone_root=tmp_path / "raw",
            contract_path=Path("data/contracts/nse_eod.json"),
            fetcher=lambda _url: _nse_csv_payload(),
        )
        trade_date = date(2026, 6, 26)

        result = connector.run(trade_date)

        manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
        events = list(session.scalars(select(OpsJobEvent).order_by(OpsJobEvent.job_event_id.asc())))

        assert manifest["current_checksum"] == result.checksum
        assert [event.event_type for event in events] == ["START", "END"]
        assert "raw_write_confirmed" in (events[-1].message or "")

        policy = IngestionSchedulerPolicy(session)
        breach = policy.emit_sla_breach_if_needed(
            job=policy.daily_jobs()[0],
            trade_date=trade_date,
            raw_write_confirmed_at=datetime(2026, 6, 26, 20, 30, tzinfo=timezone.utc),
        )
        assert breach is not None
        assert session.scalar(select(OpsSlaBreach.sla_breach_id)) is not None


def test_retry_exhaustion_creates_failed_event_and_dq_incident(tmp_path: Path) -> None:
    with _session_with_tables() as session:
        connector = _TransientFailureConnector(
            session=session,
            raw_zone_root=tmp_path / "raw",
            contract_path=Path("data/contracts/nse_eod.json"),
        )

        with pytest.raises(TimeoutError):
            connector.run(date(2026, 6, 26))

        events = list(session.scalars(select(OpsJobEvent).order_by(OpsJobEvent.job_event_id)))
        assert [event.event_type for event in events] == ["START", "RETRY", "FAILED"]
        incident = session.scalar(select(DqIncident))
        assert incident is not None
        assert incident.impact_level == "HIGH"


def test_scheduler_registers_and_triggers_policy_job(tmp_path: Path) -> None:
    with _session_with_tables(include_sla=True) as session:
        runtime = SchedulerRuntime(lambda: session, raw_zone_root=tmp_path / "raw")
        runtime.register_daily_jobs()
        assert {job.id for job in runtime.scheduler.get_jobs()} >= {
            "nse_eod_ingest",
            "bse_eod_ingest",
            "fundamentals_bootstrap",
        }

        called: list[date] = []

        class FakeConnector:
            last_pipeline_run_id = None

            def run(self, trade_date: date) -> RawWriteResult:
                called.append(trade_date)
                return RawWriteResult(
                    source_system="NSE_EOD",
                    trade_date=trade_date,
                    checksum="checksum",
                    row_count=0,
                    manifest_path=tmp_path / "manifest.json",
                    created=True,
                    updated=True,
                    confirmed_at=datetime(2026, 6, 26, 19, 0, tzinfo=timezone.utc),
                )

        runtime._build_connector = lambda _session, _job: FakeConnector()  # type: ignore[method-assign]
        runtime.run_ingestion_job(IngestionSchedulerPolicy.daily_jobs()[0], date(2026, 6, 26))
        assert called == [date(2026, 6, 26)]


def _session_with_tables(*, include_sla: bool = False):
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)

    @event.listens_for(engine, "connect")
    def _enable_sqlite_features(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()
        dbapi_connection.create_function("GREATEST", 2, max)
        dbapi_connection.create_function("LEAST", 2, min)

    tables = [
        DqRule.__table__,
        DqResult.__table__,
        DqIncident.__table__,
        OpsPipelineRun.__table__,
        OpsJobEvent.__table__,
    ]
    if include_sla:
        tables.append(OpsSlaBreach.__table__)
    Base.metadata.create_all(engine, tables=tables)
    return Session(engine)


def _nse_csv_payload() -> bytes:
    return (
        "SYMBOL,SERIES,OPEN,HIGH,LOW,CLOSE,TOTTRDQTY,DELIV_QTY,TIMESTAMP\n"
        "ITC,EQ,100,102,99,101,1000,450,2026-06-26\n"
        "HDFCBANK,EQ,1500,1512,1490,1505,900,300,2026-06-26\n"
    ).encode("utf-8")


def _bse_zip_payload() -> bytes:
    csv_payload = (
        "SC_CODE,SC_NAME,SC_GROUP,OPEN,HIGH,LOW,CLOSE,NO_OF_SHRS,DELIV_QTY,DT_TM\n"
        "500875,ITC,A,100,102,99,101,1000,450,2026-06-26\n"
    ).encode("utf-8")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("EQ260626.CSV", csv_payload)
    return buffer.getvalue()
