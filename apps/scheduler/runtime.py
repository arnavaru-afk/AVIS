"""Executable APScheduler runtime for AVIS ingestion jobs (OPS-006 to OPS-011)."""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from apps.scheduler.ingestion import IST, IngestionSchedulerPolicy, ScheduledIngestionJob
from avis.db.models import RefCompany, RefInstrument, RefSymbolAlias
from etl.fundamentals.pipeline import FundamentalsPipeline
from etl.ingest.connectors.bse_eod import BseEodConnector
from etl.ingest.connectors.nse_eod import NseEodConnector

logger = logging.getLogger(__name__)


class SchedulerRuntime:
    """Registers policy jobs and executes each in an isolated DB session."""

    def __init__(
        self,
        session_factory: Callable[[], Session],
        *,
        raw_zone_root: Path = Path("storage/raw_lake"),
        contract_root: Path = Path("data/contracts"),
    ) -> None:
        self._session_factory = session_factory
        self._raw_zone_root = raw_zone_root
        self._contract_root = contract_root
        self.scheduler = BackgroundScheduler(timezone=IST)

    def register_daily_jobs(self) -> None:
        """Register policy jobs and log their next fire time for operations."""

        for job in IngestionSchedulerPolicy.daily_jobs():
            trigger = CronTrigger.from_crontab(job.cron_expression, timezone=IST)
            self.scheduler.add_job(
                self.run_ingestion_job,
                trigger=trigger,
                args=[job],
                id=job.pipeline_name,
                replace_existing=True,
                coalesce=True,
                max_instances=1,
                misfire_grace_time=300,
            )
            scheduled_job = self.scheduler.get_job(job.pipeline_name)
            logger.info(
                "scheduler_job_registered job=%s cron=%s next_fire_time=%s",
                job.pipeline_name,
                job.cron_expression,
                (
                    scheduled_job.next_run_time
                    if scheduled_job is not None and hasattr(scheduled_job, "next_run_time")
                    else trigger.get_next_fire_time(None, datetime.now(IST))
                ),
            )

        fundamentals_trigger = CronTrigger(hour=20, minute=30, day_of_week="mon-fri", timezone=IST)
        self.scheduler.add_job(
            self.run_fundamentals,
            trigger=fundamentals_trigger,
            id="fundamentals_bootstrap",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
            misfire_grace_time=300,
        )
        fundamentals_job = self.scheduler.get_job("fundamentals_bootstrap")
        logger.info(
            "scheduler_job_registered job=%s cron=%s next_fire_time=%s",
            "fundamentals_bootstrap",
            "30 20 * * 1-5",
            (
                fundamentals_job.next_run_time
                if fundamentals_job is not None and hasattr(fundamentals_job, "next_run_time")
                else fundamentals_trigger.get_next_fire_time(None, datetime.now(IST))
            ),
        )

    def run_ingestion_job(self, job: ScheduledIngestionJob, trade_date: date | None = None) -> None:
        """Run one connector, then attach an SLA result to its pipeline run."""

        run_date = trade_date or datetime.now(IST).date()
        session = self._session_factory()
        connector = None
        try:
            connector = self._build_connector(session, job)
            result = connector.run(run_date)
            IngestionSchedulerPolicy(session).emit_sla_breach_if_needed(
                job=job,
                trade_date=run_date,
                raw_write_confirmed_at=result.confirmed_at,
                pipeline_run_id=result.pipeline_run_id,
            )
            session.commit()
        except Exception:
            # Connector persists an auditable failure and retry-exhaustion DQ incident.
            if connector is not None:
                IngestionSchedulerPolicy(session).emit_sla_breach_if_needed(
                    job=job,
                    trade_date=run_date,
                    raw_write_confirmed_at=None,
                    pipeline_run_id=connector.last_pipeline_run_id,
                )
                session.commit()
            logger.exception("scheduled_connector_failed source=%s trade_date=%s", job.source_system, run_date)
        finally:
            session.close()

    def run_fundamentals(self) -> None:
        """Run the development fundamentals bootstrap for active instruments."""

        session = self._session_factory()
        try:
            instruments = list(
                session.scalars(select(RefInstrument).where(RefInstrument.is_active.is_(True)))
            )
            pipeline = FundamentalsPipeline(session=session)
            for instrument in instruments:
                pipeline.run(
                    instrument.instrument_id,
                    instrument.symbol,
                    run_mode="SCHEDULED",
                    triggered_by="SCHEDULER",
                )
            session.commit()
        except Exception:
            session.rollback()
            logger.exception("scheduled_fundamentals_failed")
        finally:
            session.close()

    def _build_connector(self, session: Session, job: ScheduledIngestionJob):
        if job.source_system == "NSE_EOD":
            return NseEodConnector(
                session=session,
                raw_zone_root=self._raw_zone_root,
                contract_path=self._contract_root / "nse_eod.json",
            )
        if job.source_system == "BSE_EOD":
            return BseEodConnector(
                session=session,
                raw_zone_root=self._raw_zone_root,
                contract_path=self._contract_root / "bse_eod.json",
                scrip_code_to_isin=self._bse_isin_mapping(session),
            )
        raise ValueError(f"Unsupported scheduled source: {job.source_system}")

    @staticmethod
    def _bse_isin_mapping(session: Session) -> dict[str, str]:
        aliases = session.execute(
            select(RefSymbolAlias.alias_symbol, RefCompany.isin_primary)
            .join(RefInstrument, RefInstrument.instrument_id == RefSymbolAlias.instrument_id)
            .join(RefCompany, RefCompany.company_id == RefInstrument.company_id)
            .where(RefSymbolAlias.source_system == "BSE", RefCompany.isin_primary.is_not(None))
        )
        return {alias: isin for alias, isin in aliases if isin is not None}


def run_worker() -> None:
    """CLI entry point used by the compose scheduler service."""

    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    database_url = os.environ["DATABASE_URL"]
    engine = create_engine(database_url, future=True, pool_pre_ping=True)
    runtime = SchedulerRuntime(sessionmaker(engine, expire_on_commit=False))
    runtime.register_daily_jobs()
    runtime.scheduler.start()
    logger.info("scheduler_started jobs=%s", len(runtime.scheduler.get_jobs()))
    try:
        import time

        while True:
            time.sleep(60)
    except (KeyboardInterrupt, SystemExit):
        runtime.scheduler.shutdown(wait=False)
        engine.dispose()
