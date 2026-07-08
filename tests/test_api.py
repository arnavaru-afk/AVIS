from __future__ import annotations

import os
import sys
import uuid
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import jwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///./avis_api_import_guard.db")
os.environ.setdefault("AVIS_JWT_SECRET", "import-guard-secret")

from apps.api.config import Settings
from apps.api.dependencies import get_current_user, get_db
from apps.api.main import create_app
from apps.api.middleware import AuthMiddleware, ComplianceMiddleware
from apps.api.middleware.compliance import DEFAULT_SOURCE_POLICIES
from apps.api.routers import instruments, market, pipeline, quality, valuations
from apps.api.dependencies import register_compliance_check
from avis.db.base import Base
from avis.db.models import (
    AuthAuditEvent,
    AuthRole,
    AuthUser,
    AuthUserRole,
    DqIncident,
    DqOverride,
    DqResult,
    DqRule,
    FundMetricFact,
    FundStatementFact,
    MarketOhlcv1D,
    OpsJobEvent,
    OpsPipelineRun,
    OpsSlaBreach,
    RefCompany,
    RefExchange,
    RefInstrument,
    RefSymbolAlias,
    ValAssumptionSet,
    ValAttribution,
    ValConfidenceSnapshot,
    ValModelOutput,
    ValRun,
)


class FakeScalarResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)


class FakeExecuteResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)

    def scalars(self):
        return FakeScalarResult(self._rows)


class FakeSession:
    def __init__(self, *, scalar_values=None, execute_values=None, scalars_values=None):
        self._scalar_values = list(scalar_values or [])
        self._execute_values = list(execute_values or [])
        self._scalars_values = list(scalars_values or [])

    async def scalar(self, _stmt):
        return self._scalar_values.pop(0)

    async def execute(self, _stmt):
        return FakeExecuteResult(self._execute_values.pop(0))

    async def scalars(self, _stmt):
        return FakeScalarResult(self._scalars_values.pop(0))


@pytest.fixture()
def api_env(tmp_path: Path):
    db_path = tmp_path / "avis_api.sqlite"
    sync_url = f"sqlite+pysqlite:///{db_path}"
    engine = create_engine(sync_url, future=True)

    @event.listens_for(engine, "connect")
    def _enable_sqlite_features(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()
        dbapi_connection.create_function("GREATEST", 2, max)
        dbapi_connection.create_function("LEAST", 2, min)

    Base.metadata.create_all(
        engine,
        tables=[
            RefExchange.__table__,
            RefCompany.__table__,
            RefInstrument.__table__,
            RefSymbolAlias.__table__,
            MarketOhlcv1D.__table__,
            FundStatementFact.__table__,
            FundMetricFact.__table__,
            OpsPipelineRun.__table__,
            OpsJobEvent.__table__,
            OpsSlaBreach.__table__,
            DqRule.__table__,
            DqResult.__table__,
            DqIncident.__table__,
            AuthUser.__table__,
            DqOverride.__table__,
            AuthAuditEvent.__table__,
            AuthRole.__table__,
            AuthUserRole.__table__,
            ValRun.__table__,
            ValAssumptionSet.__table__,
            ValModelOutput.__table__,
            ValAttribution.__table__,
            ValConfidenceSnapshot.__table__,
        ],
    )

    ids = _seed_database(engine)
    settings = Settings(database_url=sync_url, jwt_secret="test-secret", avis_env="dev", log_level="INFO")
    app = create_app(settings=settings)
    with TestClient(app) as client:
        yield {
            "client": client,
            "engine": engine,
            "ids": ids,
            "secret": settings.jwt_secret,
        }
    engine.dispose()


@pytest.fixture()
def auth_headers(api_env):
    analyst_token = _token(api_env["ids"]["analyst_uuid"], api_env["secret"])
    viewer_token = _token(api_env["ids"]["viewer_uuid"], api_env["secret"])
    return {
        "analyst": {"Authorization": f"Bearer {analyst_token}"},
        "viewer": {"Authorization": f"Bearer {viewer_token}"},
    }


def test_instruments_router_isolation_lists_instruments() -> None:
    app = FastAPI()
    app.include_router(instruments.router, prefix="/api/v1")
    instrument = SimpleNamespace(instrument_uuid=uuid.uuid4(), symbol="ITC")
    company = SimpleNamespace(isin_primary="INE154A01025", display_name="ITC Ltd")
    exchange_row = SimpleNamespace(exchange_code="NSE")
    fake_session = FakeSession(scalar_values=[1], execute_values=[[(instrument, company, exchange_row)]])
    app.dependency_overrides[get_db] = lambda: fake_session
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(user_id=1)
    client = TestClient(app)

    response = client.get("/api/v1/instruments")

    assert response.status_code == 200
    assert response.json()["items"][0]["symbol"] == "ITC"


def test_market_router_isolation_returns_rows() -> None:
    app = FastAPI()
    app.include_router(market.router, prefix="/api/v1")
    instrument_uuid = uuid.uuid4()
    instrument = SimpleNamespace(
        instrument_uuid=instrument_uuid,
        instrument_id=101,
        listing_date=date(2020, 1, 1),
        delisting_date=None,
    )
    market_row = SimpleNamespace(
        trade_date=date(2026, 6, 25),
        open_px=Decimal("100"),
        high_px=Decimal("101"),
        low_px=Decimal("99"),
        close_px=Decimal("100"),
        adj_close_px=Decimal("95"),
        volume=Decimal("1000"),
        source_system="INTERNAL_MODEL",
    )
    fake_session = FakeSession(scalar_values=[instrument], scalars_values=[[market_row]])
    app.dependency_overrides[get_db] = lambda: fake_session
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(user_id=1)
    client = TestClient(app)

    response = client.get(f"/api/v1/market/{instrument_uuid}/ohlcv")

    assert response.status_code == 200
    assert response.json()["rows"][0]["source_system"] == "INTERNAL_MODEL"


def test_quality_router_isolation_lists_incidents() -> None:
    app = FastAPI()
    app.include_router(quality.router, prefix="/api/v1")
    incident = SimpleNamespace(
        dq_incident_id=1,
        incident_status="OPEN",
        impact_level="HIGH",
        opened_at=datetime(2026, 6, 28, 10, 0),
        assigned_to=None,
    )
    rule = SimpleNamespace(severity="HIGH", domain_name="MARKET")
    fake_session = FakeSession(scalar_values=[1], execute_values=[[(incident, rule)]])
    app.dependency_overrides[get_db] = lambda: fake_session
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(user_id=1)
    client = TestClient(app)

    response = client.get("/api/v1/quality/incidents")

    assert response.status_code == 200
    assert response.json()["items"][0]["severity"] == "HIGH"


def test_pipeline_router_isolation_lists_runs() -> None:
    app = FastAPI()
    app.include_router(pipeline.router, prefix="/api/v1")
    run = SimpleNamespace(
        pipeline_run_id=1,
        run_uuid=uuid.uuid4(),
        pipeline_name="ingestion",
        run_mode="DAILY",
        triggered_by="scheduler",
        status="SUCCESS",
        started_at=datetime(2026, 6, 28, 10, 0),
        ended_at=datetime(2026, 6, 28, 10, 5),
    )
    fake_session = FakeSession(scalars_values=[[run]])
    app.dependency_overrides[get_db] = lambda: fake_session
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(user_id=1)
    client = TestClient(app)

    response = client.get("/api/v1/pipeline/runs")

    assert response.status_code == 200
    assert response.json()["items"][0]["pipeline_name"] == "ingestion"


def test_valuations_router_isolation_sensitivity_404() -> None:
    app = FastAPI()
    app.include_router(valuations.router, prefix="/api/v1")
    fake_session = FakeSession(scalars_values=[[]])
    app.dependency_overrides[get_db] = lambda: fake_session
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(user_id=1)
    client = TestClient(app)

    response = client.get("/api/v1/valuations/1/sensitivity")

    assert response.status_code == 404
    assert response.json()["detail"]["message"] == "sensitivity not yet computed"


def test_auth_valid_token_passes(api_env, auth_headers) -> None:
    response = api_env["client"].get("/api/v1/pipeline/runs", headers=auth_headers["analyst"])

    assert response.status_code == 200


def test_auth_expired_token_returns_401(api_env) -> None:
    expired = _token(api_env["ids"]["analyst_uuid"], api_env["secret"], expires_delta=timedelta(seconds=-1))

    response = api_env["client"].get("/api/v1/pipeline/runs", headers={"Authorization": f"Bearer {expired}"})

    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "token_expired"


def test_auth_missing_token_returns_401(api_env) -> None:
    response = api_env["client"].get("/api/v1/pipeline/runs")

    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "missing_token"


def test_auth_wrong_role_returns_403(api_env, auth_headers) -> None:
    response = api_env["client"].post(
        "/api/v1/valuations/run",
        headers=auth_headers["viewer"],
        json=_valuation_request(api_env["ids"]["instrument_uuid"]),
    )

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "insufficient_role"


def test_compliance_restricted_source_returns_403(api_env, auth_headers) -> None:
    response = api_env["client"].get(
        f"/api/v1/market/{api_env['ids']['restricted_instrument_uuid']}/ohlcv",
        headers=auth_headers["analyst"],
    )

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "source_policy_blocked"


def test_compliance_unrestricted_source_returns_200(api_env, auth_headers) -> None:
    response = api_env["client"].get(
        f"/api/v1/market/{api_env['ids']['instrument_uuid']}/ohlcv",
        headers=auth_headers["analyst"],
    )

    assert response.status_code == 200


def test_post_valuation_round_trip_preserves_assumption_set(api_env, auth_headers) -> None:
    run_response = api_env["client"].post(
        "/api/v1/valuations/run",
        headers=auth_headers["analyst"],
        json=_valuation_request(api_env["ids"]["instrument_uuid"]),
    )

    assert run_response.status_code == 200, run_response.text
    payload = run_response.json()
    detail = api_env["client"].get(
        f"/api/v1/valuations/{payload['val_run_id']}",
        headers=auth_headers["analyst"],
    )

    assert detail.status_code == 200
    assumptions = {row["key"]: row for row in detail.json()["assumption_set"]}
    assert Decimal(assumptions["WACC"]["numeric_value"]) == Decimal("0.11")
    assert assumptions["RUN_LABEL"]["text_value"] == "api-regression"


def test_price_history_enforces_as_of_window(api_env, auth_headers) -> None:
    response = api_env["client"].get(
        f"/api/v1/instruments/{api_env['ids']['instrument_uuid']}/price-history",
        headers=auth_headers["analyst"],
        params={"from_date": "2026-06-25", "to_date": "2026-06-26"},
    )

    assert response.status_code == 200
    assert [row["trade_date"] for row in response.json()["rows"]] == ["2026-06-25", "2026-06-26"]


def test_quality_override_records_dq_override(api_env, auth_headers) -> None:
    response = api_env["client"].post(
        f"/api/v1/quality/incidents/{api_env['ids']['incident_id']}/override",
        headers=auth_headers["analyst"],
        json={
            "justification": "This override is justified by validated manual review.",
            "analyst_id": str(api_env["ids"]["analyst_uuid"]),
        },
    )

    assert response.status_code == 200
    with Session(api_env["engine"]) as session:
        overrides = session.scalars(select(DqOverride)).all()
        assert len(overrides) == 1
        assert session.get(DqIncident, api_env["ids"]["incident_id"]).incident_status == "RESOLVED"


def test_pipeline_runs_endpoint_reflects_rows(api_env, auth_headers) -> None:
    response = api_env["client"].get("/api/v1/pipeline/runs", headers=auth_headers["analyst"])

    assert response.status_code == 200
    assert response.json()["items"][0]["pipeline_name"] == "daily_ingestion"


def test_unknown_instrument_uuid_returns_404(api_env, auth_headers) -> None:
    response = api_env["client"].get(f"/api/v1/instruments/{uuid.uuid4()}", headers=auth_headers["analyst"])

    assert response.status_code == 404


def test_from_date_after_to_date_returns_422(api_env, auth_headers) -> None:
    response = api_env["client"].get(
        f"/api/v1/instruments/{api_env['ids']['instrument_uuid']}/price-history",
        headers=auth_headers["analyst"],
        params={"from_date": "2026-06-27", "to_date": "2026-06-26"},
    )

    assert response.status_code == 422


def test_low_confidence_run_sets_override_required_and_null_relative(api_env, auth_headers) -> None:
    with Session(api_env["engine"]) as session:
        _insert_open_incident(session, incident_id=999, dq_result_id=999)
        session.commit()

    response = api_env["client"].post(
        "/api/v1/valuations/run",
        headers=auth_headers["analyst"],
        json=_valuation_request(api_env["ids"]["solo_instrument_uuid"]),
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["override_required"] is True
    assert payload["relative_value"] is None


def _valuation_request(instrument_uuid: uuid.UUID) -> dict[str, object]:
    return {
        "instrument_uuid": str(instrument_uuid),
        "run_label": "api-regression",
        "assumption_set": {
            "wacc": "0.11",
            "terminal_growth": "0.04",
            "forecast_years": 5,
            "revenue_cagr": "0.08",
            "ebit_margin": "0.24",
            "da": ["40", "42", "44", "46", "48"],
            "capex": ["55", "58", "60", "62", "65"],
            "nwc_delta": ["10", "11", "12", "13", "14"],
        },
    }


def _token(user_uuid: uuid.UUID, secret: str, *, expires_delta: timedelta = timedelta(hours=1)) -> str:
    payload = {
        "user_uuid": str(user_uuid),
        "exp": datetime.now(UTC) + expires_delta,
    }
    return jwt.encode(payload, secret, algorithm="HS256")


def _seed_database(engine) -> dict[str, object]:
    now = datetime(2026, 6, 28, 12, 0)
    instrument_uuid = uuid.uuid4()
    restricted_uuid = uuid.uuid4()
    solo_uuid = uuid.uuid4()
    analyst_uuid = uuid.uuid4()
    viewer_uuid = uuid.uuid4()
    with Session(engine) as session:
        session.add(RefExchange(exchange_id=1, exchange_code="NSE", exchange_name="NSE", country_code="IN", timezone="Asia/Kolkata", currency_code="INR", is_active=True, created_at=now, updated_at=now))
        companies = [
            RefCompany(company_id=10, company_uuid=uuid.uuid4(), legal_name="TargetCo", display_name="TargetCo", isin_primary="INE000A01010", sector_code="FMCG", industry_code="FMCG_CORE", incorporation_country="IN", is_listed=True, is_active=True, created_at=now, updated_at=now),
            RefCompany(company_id=20, company_uuid=uuid.uuid4(), legal_name="Peer1", display_name="Peer1", isin_primary="INE000A01020", sector_code="FMCG", industry_code="FMCG_CORE", incorporation_country="IN", is_listed=True, is_active=True, created_at=now, updated_at=now),
            RefCompany(company_id=30, company_uuid=uuid.uuid4(), legal_name="Peer2", display_name="Peer2", isin_primary="INE000A01030", sector_code="FMCG", industry_code="FMCG_CORE", incorporation_country="IN", is_listed=True, is_active=True, created_at=now, updated_at=now),
            RefCompany(company_id=40, company_uuid=uuid.uuid4(), legal_name="Peer3", display_name="Peer3", isin_primary="INE000A01040", sector_code="FMCG", industry_code="FMCG_CORE", incorporation_country="IN", is_listed=True, is_active=True, created_at=now, updated_at=now),
            RefCompany(company_id=50, company_uuid=uuid.uuid4(), legal_name="Peer4", display_name="Peer4", isin_primary="INE000A01050", sector_code="FMCG", industry_code="FMCG_CORE", incorporation_country="IN", is_listed=True, is_active=True, created_at=now, updated_at=now),
            RefCompany(company_id=60, company_uuid=uuid.uuid4(), legal_name="SoloCo", display_name="SoloCo", isin_primary="INE000A01060", sector_code="BANK", industry_code="BANK_PRIVATE", incorporation_country="IN", is_listed=True, is_active=True, created_at=now, updated_at=now),
            RefCompany(company_id=70, company_uuid=uuid.uuid4(), legal_name="RestrictedCo", display_name="RestrictedCo", isin_primary="INE000A01070", sector_code="FMCG", industry_code="FMCG_CORE", incorporation_country="IN", is_listed=True, is_active=True, created_at=now, updated_at=now),
        ]
        session.add_all(companies)
        instruments_seed = [
            RefInstrument(instrument_id=101, instrument_uuid=instrument_uuid, company_id=10, exchange_id=1, symbol="TGT", instrument_type="EQUITY", listing_date=date(2020, 1, 1), delisting_date=None, tick_size=Decimal("0.05"), lot_size=1, is_active=True, created_at=now, updated_at=now),
            RefInstrument(instrument_id=201, instrument_uuid=uuid.uuid4(), company_id=20, exchange_id=1, symbol="P1", instrument_type="EQUITY", listing_date=date(2020, 1, 1), delisting_date=None, tick_size=Decimal("0.05"), lot_size=1, is_active=True, created_at=now, updated_at=now),
            RefInstrument(instrument_id=202, instrument_uuid=uuid.uuid4(), company_id=30, exchange_id=1, symbol="P2", instrument_type="EQUITY", listing_date=date(2020, 1, 1), delisting_date=None, tick_size=Decimal("0.05"), lot_size=1, is_active=True, created_at=now, updated_at=now),
            RefInstrument(instrument_id=203, instrument_uuid=uuid.uuid4(), company_id=40, exchange_id=1, symbol="P3", instrument_type="EQUITY", listing_date=date(2020, 1, 1), delisting_date=None, tick_size=Decimal("0.05"), lot_size=1, is_active=True, created_at=now, updated_at=now),
            RefInstrument(instrument_id=204, instrument_uuid=uuid.uuid4(), company_id=50, exchange_id=1, symbol="P4", instrument_type="EQUITY", listing_date=date(2020, 1, 1), delisting_date=None, tick_size=Decimal("0.05"), lot_size=1, is_active=True, created_at=now, updated_at=now),
            RefInstrument(instrument_id=301, instrument_uuid=solo_uuid, company_id=60, exchange_id=1, symbol="SOLO", instrument_type="EQUITY", listing_date=date(2020, 1, 1), delisting_date=None, tick_size=Decimal("0.05"), lot_size=1, is_active=True, created_at=now, updated_at=now),
            RefInstrument(instrument_id=401, instrument_uuid=restricted_uuid, company_id=70, exchange_id=1, symbol="REST", instrument_type="EQUITY", listing_date=date(2020, 1, 1), delisting_date=None, tick_size=Decimal("0.05"), lot_size=1, is_active=True, created_at=now, updated_at=now),
        ]
        session.add_all(instruments_seed)
        session.add(RefSymbolAlias(alias_id=1, instrument_id=101, source_system="NSE", alias_symbol="TARGET", valid_from=date(2020, 1, 1), valid_to=None, created_at=now))
        session.add_all([
            MarketOhlcv1D(ohlcv_1d_id=1, instrument_id=101, trade_date=date(2026, 6, 25), open_px=Decimal("100"), high_px=Decimal("101"), low_px=Decimal("99"), close_px=Decimal("100"), adj_close_px=Decimal("95"), volume=Decimal("1000"), turnover=None, source_system="INTERNAL_MODEL", source_record_id="row1", ingested_at=now),
            MarketOhlcv1D(ohlcv_1d_id=2, instrument_id=101, trade_date=date(2026, 6, 26), open_px=Decimal("102"), high_px=Decimal("103"), low_px=Decimal("101"), close_px=Decimal("102"), adj_close_px=Decimal("96"), volume=Decimal("1100"), turnover=None, source_system="INTERNAL_MODEL", source_record_id="row2", ingested_at=now),
            MarketOhlcv1D(ohlcv_1d_id=3, instrument_id=101, trade_date=date(2026, 6, 27), open_px=Decimal("104"), high_px=Decimal("105"), low_px=Decimal("103"), close_px=Decimal("104"), adj_close_px=Decimal("97"), volume=Decimal("1200"), turnover=None, source_system="INTERNAL_MODEL", source_record_id="row3", ingested_at=now),
            MarketOhlcv1D(ohlcv_1d_id=4, instrument_id=401, trade_date=date(2026, 6, 25), open_px=Decimal("50"), high_px=Decimal("51"), low_px=Decimal("49"), close_px=Decimal("50"), adj_close_px=None, volume=Decimal("900"), turnover=None, source_system="NSE_EOD", source_record_id="row4", ingested_at=now),
        ])
        _seed_financials(session, now)
        session.add(OpsPipelineRun(pipeline_run_id=1, run_uuid=uuid.uuid4(), pipeline_name="daily_ingestion", run_mode="DAILY", triggered_by="scheduler", status="SUCCESS", started_at=now, ended_at=now + timedelta(minutes=5), run_context={"source": "scheduler"}))
        session.add(OpsJobEvent(job_event_id=1, pipeline_run_id=1, job_name="nse_eod", event_type="START", event_ts=now, message="started", metrics_payload=None))
        session.add(OpsSlaBreach(sla_breach_id=1, pipeline_run_id=1, sla_type="INGESTION", service_name="NSE_EOD", expected_value=Decimal("1"), actual_value=Decimal("2"), breach_detected_at=now, resolved_at=None, status="OPEN"))
        session.add(DqRule(dq_rule_id=1, rule_code="NULL_CHECK", rule_name="Null Check", domain_name="MARKET", severity="HIGH", rule_expression="close_px IS NOT NULL", is_active=True, owner_team="data", created_at=now, updated_at=now))
        session.add(DqResult(dq_result_id=1, dq_rule_id=1, pipeline_run_id=1, target_table="market_ohlcv_1d", target_record_key="101", status="FAIL", failure_reason="missing value", measured_value=None, threshold_value=None, evaluated_at=now))
        session.add(DqIncident(dq_incident_id=1, dq_result_id=1, incident_status="OPEN", impact_level="HIGH", assigned_to=None, opened_at=now, resolved_at=None, resolution_notes=None))
        session.add_all([
            AuthUser(user_id=1, user_uuid=analyst_uuid, email="analyst@example.com", full_name="Analyst", status="ACTIVE", is_mfa_enabled=False, last_login_at=None, created_at=now, updated_at=now),
            AuthUser(user_id=2, user_uuid=viewer_uuid, email="viewer@example.com", full_name="Viewer", status="ACTIVE", is_mfa_enabled=False, last_login_at=None, created_at=now, updated_at=now),
        ])
        session.add_all([
            AuthRole(role_id=1, role_code="ANALYST", role_name="Analyst", description=None, is_system_role=True, created_at=now),
            AuthRole(role_id=2, role_code="VIEWER", role_name="Viewer", description=None, is_system_role=True, created_at=now),
        ])
        session.add_all([
            AuthUserRole(user_role_id=1, user_id=1, role_id=1, granted_by_user_id=None, valid_from=now - timedelta(days=1), valid_to=None, created_at=now),
            AuthUserRole(user_role_id=2, user_id=2, role_id=2, granted_by_user_id=None, valid_from=now - timedelta(days=1), valid_to=None, created_at=now),
        ])
        session.commit()

    return {
        "instrument_uuid": instrument_uuid,
        "restricted_instrument_uuid": restricted_uuid,
        "solo_instrument_uuid": solo_uuid,
        "incident_id": 1,
        "analyst_uuid": analyst_uuid,
        "viewer_uuid": viewer_uuid,
    }


def _seed_financials(session: Session, now: datetime) -> None:
    revenue_map = {101: "1000", 201: "1500", 202: "1400", 203: "1450", 204: "1520", 301: "1000", 401: "900"}
    metrics_map = {
        101: {"TAX_RATE": "0.25", "SHARE_COUNT": "100", "NET_DEBT": "200", "ENTERPRISE_VALUE": "1500", "EQUITY_VALUE": "1300", "EBITDA": "160", "EARNINGS": "110", "BOOK_VALUE": "700", "SALES": "1200", "DEPRECIATION_AND_AMORTIZATION": "40", "CAPEX": "55", "DELTA_NWC": "10"},
        201: {"TAX_RATE": "0.25", "SHARE_COUNT": "100", "NET_DEBT": "200", "ENTERPRISE_VALUE": "1800", "EQUITY_VALUE": "1600", "EBITDA": "180", "EARNINGS": "120", "BOOK_VALUE": "900", "SALES": "1500", "DEPRECIATION_AND_AMORTIZATION": "40", "CAPEX": "55", "DELTA_NWC": "10"},
        202: {"TAX_RATE": "0.25", "SHARE_COUNT": "100", "NET_DEBT": "200", "ENTERPRISE_VALUE": "1650", "EQUITY_VALUE": "1450", "EBITDA": "170", "EARNINGS": "115", "BOOK_VALUE": "850", "SALES": "1400", "DEPRECIATION_AND_AMORTIZATION": "40", "CAPEX": "55", "DELTA_NWC": "10"},
        203: {"TAX_RATE": "0.25", "SHARE_COUNT": "100", "NET_DEBT": "250", "ENTERPRISE_VALUE": "1750", "EQUITY_VALUE": "1500", "EBITDA": "175", "EARNINGS": "118", "BOOK_VALUE": "880", "SALES": "1450", "DEPRECIATION_AND_AMORTIZATION": "40", "CAPEX": "55", "DELTA_NWC": "10"},
        204: {"TAX_RATE": "0.25", "SHARE_COUNT": "100", "NET_DEBT": "200", "ENTERPRISE_VALUE": "1900", "EQUITY_VALUE": "1700", "EBITDA": "190", "EARNINGS": "125", "BOOK_VALUE": "920", "SALES": "1520", "DEPRECIATION_AND_AMORTIZATION": "40", "CAPEX": "55", "DELTA_NWC": "10"},
        301: {"TAX_RATE": "0.25", "SHARE_COUNT": "100", "NET_DEBT": "200", "ENTERPRISE_VALUE": "1400", "EQUITY_VALUE": "1200", "EBITDA": "150", "EARNINGS": "100", "BOOK_VALUE": "650", "SALES": "1100", "DEPRECIATION_AND_AMORTIZATION": "40", "CAPEX": "55", "DELTA_NWC": "10"},
        401: {"TAX_RATE": "0.25", "SHARE_COUNT": "100", "NET_DEBT": "180", "ENTERPRISE_VALUE": "1200", "EQUITY_VALUE": "1020", "EBITDA": "120", "EARNINGS": "80", "BOOK_VALUE": "500", "SALES": "900", "DEPRECIATION_AND_AMORTIZATION": "30", "CAPEX": "45", "DELTA_NWC": "8"},
    }
    statement_id = 1
    metric_id = 1
    for instrument_id, revenue in revenue_map.items():
        session.add(FundStatementFact(statement_fact_id=statement_id, instrument_id=instrument_id, fiscal_year=2025, fiscal_period="FY", statement_type="PL", line_item_code="Revenue", value=Decimal(revenue), currency_code="INR", scale_code="ABS", as_reported_ts=now, effective_from_ts=now, effective_to_ts=None, source_system="INTERNAL_MODEL", source_record_id=f"revenue-{instrument_id}", ingested_at=now))
        statement_id += 1
    for instrument_id, metric_values in metrics_map.items():
        for metric_code, metric_value in metric_values.items():
            session.add(FundMetricFact(metric_fact_id=metric_id, instrument_id=instrument_id, metric_code=metric_code, metric_value=Decimal(metric_value), metric_unit="ratio" if metric_code == "TAX_RATE" else "amount", as_of_date=date(2026, 3, 31), lookback_period=None, calc_version="v1", input_hash=f"hash-{instrument_id}-{metric_code}", created_at=now))
            metric_id += 1


def _insert_open_incident(session: Session, *, incident_id: int, dq_result_id: int) -> None:
    now = datetime(2026, 6, 28, 12, 0)
    session.add(DqResult(dq_result_id=dq_result_id, dq_rule_id=1, pipeline_run_id=1, target_table="market_ohlcv_1d", target_record_key="solo", status="FAIL", failure_reason="manual low confidence trigger", measured_value=None, threshold_value=None, evaluated_at=now))
    session.add(DqIncident(dq_incident_id=incident_id, dq_result_id=dq_result_id, incident_status="OPEN", impact_level="HIGH", assigned_to=None, opened_at=now, resolved_at=None, resolution_notes=None))
