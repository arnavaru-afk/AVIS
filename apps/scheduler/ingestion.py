"""Daily schedule definitions and SLA checks for ingestion jobs."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from avis.db.models import OpsJobEvent, OpsPipelineRun, OpsSlaBreach

IST = ZoneInfo("Asia/Kolkata")


@dataclass(frozen=True, slots=True)
class ScheduledIngestionJob:
    """Cron-style definition for a scheduled ingestion job."""

    source_system: str
    cron_expression: str
    scheduled_time_ist: time
    sla_deadline_ist: time
    pipeline_name: str


class IngestionSchedulerPolicy:
    """Provides daily schedules and SLA breach recording."""

    def __init__(self, session: Session) -> None:
        self._session = session

    @staticmethod
    def daily_jobs() -> tuple[ScheduledIngestionJob, ...]:
        return (
            ScheduledIngestionJob(
                source_system="NSE_EOD",
                cron_expression="30 18 * * 1-5",
                scheduled_time_ist=time(hour=18, minute=30),
                sla_deadline_ist=time(hour=20, minute=0),
                pipeline_name="nse_eod_ingest",
            ),
            ScheduledIngestionJob(
                source_system="BSE_EOD",
                cron_expression="0 19 * * 1-5",
                scheduled_time_ist=time(hour=19, minute=0),
                sla_deadline_ist=time(hour=20, minute=0),
                pipeline_name="bse_eod_ingest",
            ),
        )

    def emit_sla_breach_if_needed(
        self,
        *,
        job: ScheduledIngestionJob,
        trade_date: date,
        raw_write_confirmed_at: datetime | None,
    ) -> OpsSlaBreach | None:
        """Emit an SLA breach when the raw write missed the expected deadline."""

        deadline = datetime.combine(trade_date, job.sla_deadline_ist, tzinfo=IST)
        if raw_write_confirmed_at is not None:
            confirmed_ist = raw_write_confirmed_at.astimezone(IST)
            if confirmed_ist <= deadline:
                return None

        pipeline_run = OpsPipelineRun(
            run_uuid=uuid.uuid4(),
            pipeline_name=job.pipeline_name,
            run_mode="SCHEDULED",
            triggered_by="SCHEDULER",
            status="FAILED",
            started_at=datetime.now(timezone.utc),
            ended_at=datetime.now(timezone.utc),
            run_context={"trade_date": trade_date.isoformat(), "source_system": job.source_system},
        )
        self._assign_pk_if_sqlite(pipeline_run, "pipeline_run_id", OpsPipelineRun)
        self._session.add(pipeline_run)
        self._session.flush()

        event = OpsJobEvent(
            pipeline_run_id=pipeline_run.pipeline_run_id,
            job_name=f"{job.pipeline_name}_sla",
            event_type="ERROR",
            event_ts=datetime.now(timezone.utc),
            message=f"SLA breach for {job.source_system} on {trade_date.isoformat()}",
            metrics_payload={"deadline_ist": deadline.isoformat()},
        )
        self._assign_pk_if_sqlite(event, "job_event_id", OpsJobEvent)
        self._session.add(event)

        breach = OpsSlaBreach(
            pipeline_run_id=pipeline_run.pipeline_run_id,
            sla_type="RAW_WRITE_BY_20_00_IST",
            service_name=job.pipeline_name,
            expected_value=2000,
            actual_value=None,
            breach_detected_at=datetime.now(timezone.utc),
            resolved_at=None,
            status="OPEN",
        )
        self._assign_pk_if_sqlite(breach, "sla_breach_id", OpsSlaBreach)
        self._session.add(breach)
        self._session.flush()
        return breach

    def _assign_pk_if_sqlite(self, instance, pk_name: str, model) -> None:
        bind = self._session.get_bind()
        if bind is None or bind.dialect.name != "sqlite":
            return
        if getattr(instance, pk_name, None) is not None:
            return
        pk_column = getattr(model, pk_name)
        current_max = self._session.scalar(select(func.max(pk_column)))
        setattr(instance, pk_name, 1 if current_max is None else int(current_max) + 1)
