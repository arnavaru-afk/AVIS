from __future__ import annotations

import sys
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from avis.db.base import Base
from avis.db.models import (
    AuthRole,
    AuthUser,
    AuthUserRole,
    RefCompany,
    RefExchange,
    RefInstrument,
    RefSymbolAlias,
)
from scripts.bootstrap import seed_database


def test_bootstrap_seeds_expected_reference_counts() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(
        engine,
        tables=[
            RefExchange.__table__,
            RefCompany.__table__,
            RefInstrument.__table__,
            RefSymbolAlias.__table__,
            AuthUser.__table__,
            AuthRole.__table__,
            AuthUserRole.__table__,
        ],
    )

    with Session(engine) as session:
        counts = seed_database(session)

        assert counts["exchanges"] == 2
        assert counts["companies"] == 5
        assert counts["instruments"] == 5
        assert counts["aliases"] == 15
        assert session.scalar(select(RefExchange.exchange_code).where(RefExchange.exchange_id == 1)) == "NSE"
        assert session.scalar(select(RefExchange.exchange_code).where(RefExchange.exchange_id == 2)) == "BSE"


def test_bootstrap_is_idempotent() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(
        engine,
        tables=[
            RefExchange.__table__,
            RefCompany.__table__,
            RefInstrument.__table__,
            RefSymbolAlias.__table__,
            AuthUser.__table__,
            AuthRole.__table__,
            AuthUserRole.__table__,
        ],
    )

    with Session(engine) as session:
        seed_database(session)
        counts_after_first = {
            "exchanges": session.query(RefExchange).count(),
            "companies": session.query(RefCompany).count(),
            "instruments": session.query(RefInstrument).count(),
            "aliases": session.query(RefSymbolAlias).count(),
        }
        seed_database(session)
        counts_after_second = {
            "exchanges": session.query(RefExchange).count(),
            "companies": session.query(RefCompany).count(),
            "instruments": session.query(RefInstrument).count(),
            "aliases": session.query(RefSymbolAlias).count(),
        }

        assert counts_after_first == counts_after_second
        assert counts_after_second == {
            "exchanges": 2,
            "companies": 5,
            "instruments": 5,
            "aliases": 15,
        }
