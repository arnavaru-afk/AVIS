from avis.db.base import Base
import avis.db.models as m


def test_expected_tables_present() -> None:
    expected = {
        "ref_exchange",
        "ref_company",
        "ref_instrument",
        "ref_symbol_alias",
        "ref_calendar",
        "market_ohlcv_1d",
        "market_ohlcv_intraday",
        "market_corporate_action",
        "market_orderbook_snapshot",
        "fund_statement_fact",
        "fund_metric_fact",
        "fund_restatement_log",
        "fund_segment_fact",
        "news_source_profile",
        "news_article",
        "news_entity_link",
        "news_event",
        "news_reliability_score",
        "val_run",
        "val_assumption_set",
        "val_model_output",
        "val_sensitivity",
        "val_attribution",
        "val_confidence_snapshot",
        "dq_rule",
        "dq_result",
        "dq_incident",
        "dq_override",
        "ops_pipeline_run",
        "ops_job_event",
        "ops_data_lineage",
        "ops_sla_breach",
        "auth_user",
        "auth_role",
        "auth_permission",
        "auth_user_role",
        "auth_audit_event",
    }
    assert set(Base.metadata.tables.keys()) == expected


def test_prefix_groups_present() -> None:
    tables = set(Base.metadata.tables)
    for prefix in ("ref_", "market_", "fund_", "news_", "val_", "dq_", "ops_", "auth_"):
        assert any(name.startswith(prefix) for name in tables)


def test_core_constraints_exist() -> None:
    market_table = Base.metadata.tables["market_ohlcv_1d"]
    check_names = {c.name for c in market_table.constraints if getattr(c, "name", None)}
    assert "ck_market_ohlcv_1d_market_ohlcv_1d_high_valid" in check_names
    assert "ck_market_ohlcv_1d_market_ohlcv_1d_low_valid" in check_names

    val_conf = Base.metadata.tables["val_confidence_snapshot"]
    assert any(i.name == "ix_val_conf_overall" for i in val_conf.indexes)


def test_relationship_smoke() -> None:
    assert "instrument" in m.MarketOhlcv1D.__mapper__.relationships
    assert "model_outputs" in m.ValRun.__mapper__.relationships
    assert "rule" in m.DqResult.__mapper__.relationships
    assert "role" in m.AuthUserRole.__mapper__.relationships
