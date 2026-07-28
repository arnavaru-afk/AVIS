"""Seed annual fundamentals into fund_* tables using the bootstrap yfinance source."""

from __future__ import annotations

from collections import Counter

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, selectinload

from avis.db.models import RefInstrument
from etl.fundamentals import FundamentalsPipeline
from scripts.bootstrap import get_database_url


def main() -> None:
    engine = create_engine(get_database_url(), future=True)
    with Session(engine) as session:
        instruments = list(
            session.scalars(
                select(RefInstrument)
                .options(selectinload(RefInstrument.exchange))
                .where(RefInstrument.is_active.is_(True))
                .order_by(RefInstrument.instrument_id.asc())
            )
        )
        pipeline = FundamentalsPipeline(session=session)
        summary = Counter()
        skipped: list[str] = []
        for instrument in instruments:
            result = pipeline.run(instrument.instrument_id, instrument.symbol)
            if result.skipped:
                summary["instruments_skipped"] += 1
                skipped.append(f"{instrument.symbol}: {result.skip_reason}")
            else:
                summary["instruments_processed"] += 1
                summary["statements_stored"] += result.statements_stored
                summary["metrics_computed"] += result.metrics_computed
            session.commit()

    print("Fundamentals seed summary")
    print(f"instruments_processed={summary['instruments_processed']}")
    print(f"statements_stored={summary['statements_stored']}")
    print(f"metrics_computed={summary['metrics_computed']}")
    print(f"instruments_skipped={summary['instruments_skipped']}")
    if skipped:
        print("skipped_reasons=")
        for reason in skipped:
            print(f"- {reason}")


if __name__ == "__main__":
    main()
