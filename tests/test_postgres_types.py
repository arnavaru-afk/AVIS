from sqlalchemy.schema import CreateTable
from sqlalchemy.dialects import postgresql

from avis.db.base import Base


def test_postgres_specific_types_compile() -> None:
    orderbook_sql = str(
        CreateTable(Base.metadata.tables["market_orderbook_snapshot"]).compile(
            dialect=postgresql.dialect()
        )
    )
    assert "JSONB" in orderbook_sql

    audit_sql = str(
        CreateTable(Base.metadata.tables["auth_audit_event"]).compile(
            dialect=postgresql.dialect()
        )
    )
    assert "INET" in audit_sql
