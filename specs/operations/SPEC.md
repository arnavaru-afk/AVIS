# Operations Specification

## Problem Framing

### Context
AVIS currently defines ingestion schedules in `apps/scheduler/ingestion.py`, but no runtime process executes those schedules. The repository also lacks unauthenticated liveness/readiness endpoints for container orchestration, lacks a scheduler service in `docker-compose.yml`, and does not yet enforce repository-wide Ruff hygiene under CI.

### Current Gaps
1. `IngestionSchedulerPolicy.daily_jobs()` is declarative only; no scheduler invokes it.
2. Exchange ingestion and fundamentals bootstrap pipelines cannot run automatically on schedule.
3. `OpsPipelineRun`, `OpsJobEvent`, and `OpsSlaBreach` exist in the schema, but scheduled operational visibility is incomplete because runtime execution is missing.
4. API containers do not expose `/health` or `/ready`, so container health cannot be asserted without authenticated business endpoints.
5. CI lint is scoped narrowly and does not yet enforce repository-wide operational hygiene.
6. Connector failure handling records runtime failure events, but does not implement production-grade retry exhaustion escalation into a DQ incident workflow.

### Objective
Turn AVIS scheduling from a policy definition into an actually-running operational subsystem, while improving runtime observability and repository hygiene to a production-ready baseline.

### Goals
- Run ingestion jobs on their configured schedules.
- Persist all scheduled executions and failures into existing `ops_*` tables.
- Detect and persist SLA breaches automatically.
- Add liveness/readiness endpoints and container healthchecks.
- Expand lint enforcement to `ruff check .` after making the repository pass.
- Add bounded connector retries with DQ escalation on exhaustion.

## Specification

### Design Decision: APScheduler vs Celery + Redis

#### Alternatives Considered
1. APScheduler in a dedicated scheduler process
2. Celery + Redis with a beat scheduler and worker fleet

#### Chosen Design
Use APScheduler in a dedicated scheduler/worker process for this phase.

#### Why APScheduler Is Chosen
- Existing connectors and fundamentals pipeline are synchronous SQLAlchemy-oriented code paths and fit a simple process model well.
- The required workload is small and deterministic: weekday NSE/BSE EOD jobs plus fundamentals bootstrap scheduling.
- The repo currently has no Redis/broker dependency, no existing task queue abstraction, and no need for horizontal fan-out in this phase.
- A dedicated scheduler container isolates scheduling from the FastAPI process without introducing distributed systems complexity.
- Acceptance criteria require visible scheduled execution and operational hygiene, not multi-node scheduling.

#### Why Celery Is Not Chosen in This Phase
- It adds Redis/broker infrastructure, task serialization, worker lifecycle management, and retry orchestration complexity that is disproportionate to the current workload.
- It would require additional operational specs and acceptance criteria for broker availability, queue durability, task idempotency, and worker scaling.
- No current requirement demands horizontal throughput or multi-region coordination.

#### Future Scalability Trigger for Revisit
Revisit Celery or another distributed scheduler when any of the following become true:
- Multiple scheduler replicas must coordinate without duplicate execution.
- Workload expands beyond a small set of predictable jobs.
- Long-running or high-volume workloads require queue-based backpressure and worker pools.
- Operational requirements demand distributed retries, delayed tasks, or workload isolation across teams/domains.

## Functional Requirements

### OPS-001 Scheduler Runtime
A dedicated scheduler process shall load `IngestionSchedulerPolicy.daily_jobs()` and register each job with APScheduler using the configured cron schedule in IST.

### OPS-002 Job Coverage
The scheduler runtime shall invoke the following workloads:
- `NSE_EOD` connector
- `BSE_EOD` connector
- fundamentals bootstrap pipeline for active instruments

### OPS-003 Startup Visibility
On scheduler startup, each scheduled job shall emit an informational log line containing at minimum:
- job name
- cron expression
- next fire time
- source system / pipeline name

### OPS-004 Scheduled Run Persistence
Every scheduled execution attempt shall persist an `OpsPipelineRun` row with:
- `run_mode = 'SCHEDULED'`
- `triggered_by = 'SCHEDULER'`
- `pipeline_name` matching the job definition
- lifecycle status transitions reflecting actual runtime outcome

### OPS-004A Operational Event Vocabulary Migration
The `ops_job_event` check constraint shall be migrated to allow `FAILED` in addition to the existing event types. Retry exhaustion shall emit this terminal event type; this is a backward-compatible schema expansion.

### OPS-005 Scheduled Job Events
Each scheduled execution shall persist `OpsJobEvent` rows covering at minimum:
- scheduler dispatch start
- connector/pipeline completion
- retry attempts
- retry exhaustion / terminal failure
- SLA breach recording when applicable

### OPS-006 API Operational Visibility
The scheduler’s persisted `ops_*` records shall remain compatible with the existing pipeline API and terminal operations module without requiring new database tables.

### OPS-007 SLA Breach Runtime Detection
The scheduler runtime shall trigger SLA evaluation for each scheduled ingestion job using the configured `sla_deadline_ist` and the actual confirmed completion timestamp. If a job misses its deadline, the runtime shall persist an `OpsSlaBreach` row.

### OPS-008 No Synthetic Duplicate Pipeline Runs for SLA
When a scheduled job already has an `OpsPipelineRun`, SLA breach persistence shall attach to that run rather than creating a second synthetic run solely for the breach.

### OPS-009 Retry Policy
Connector execution shall support bounded exponential backoff with configurable:
- initial delay
- maximum delay
- maximum attempts

### OPS-010 Retry Scope
Retry logic shall apply only to transient connector failures, including:
- timeout
- DNS / transport failure
- transient HTTP `429`
- transient HTTP `5xx`

Non-transient schema/validation errors shall not be retried.

### OPS-011 Retry Eventing
Each retry attempt shall emit an `OpsJobEvent` entry with attempt count and next retry timing in `metrics_payload`.

### OPS-012 Retry Exhaustion DQ Escalation
If retries are exhausted, the system shall create a `DqResult` and `DqIncident` using the existing quality-engine / DQ persistence path so the failure is visible via `GET /api/v1/quality/incidents`.

### OPS-013 Failure Event Type Compatibility
Terminal scheduled failures shall be represented in persisted events using the existing schema-compatible `OpsJobEvent.event_type` taxonomy. The system shall expose terminal failure semantics without violating current database constraints.

### OPS-014 Health Endpoint
The API shall expose `GET /health` as an unauthenticated liveness endpoint returning `200` when the process is up.

### OPS-015 Ready Endpoint
The API shall expose `GET /ready` as an unauthenticated readiness endpoint returning `200` only when the database is reachable.

### OPS-016 Auth Middleware Bypass
`/health` and `/ready` shall be excluded from authentication middleware enforcement.

### OPS-017 Docker Compose Scheduler Service
`docker-compose.yml` shall define a `scheduler` service that depends on:
- `db` being healthy
- `api` being healthy

### OPS-018 API Healthcheck Integration
`docker-compose.yml` shall configure the `api` service healthcheck to call the unauthenticated readiness or liveness endpoints rather than relying on container start alone.

### OPS-019 Scheduler Container Command
The scheduler service shall run the dedicated scheduler process entrypoint, not the API server entrypoint.

### OPS-020 Repository-Wide Lint Enforcement
CI shall run `ruff check .` over the repository root with excludes for generated/runtime directories such as `.venv`, `.deps`, and `__pycache__`.

### OPS-021 Zero-Error Lint Baseline
The repository shall be cleaned such that `ruff check .` passes with zero errors before the stricter CI job is enabled.

### OPS-022 Test Coverage
Automated tests shall cover:
- scheduler job trigger execution
- SLA breach persistence when completion occurs after the deadline
- `/health` and `/ready`
- retry then DQ incident escalation
- unchanged green regression suite via `pytest -q`

## Non-Functional Requirements

### NFR-001 Simplicity
The scheduling architecture shall minimize new infrastructure dependencies in this phase.

### NFR-002 Determinism
Scheduled jobs shall be idempotent with respect to their existing connector/pipeline contracts so restart/replay does not create uncontrolled duplication.

### NFR-003 Timezone Correctness
Cron schedules and SLA deadlines shall be evaluated in IST consistently, regardless of container host timezone.

### NFR-004 Observability
Operational state shall be visible using existing AVIS operational surfaces: logs, `ops_*` tables, pipeline API, and terminal operations module.

### NFR-005 Failure Auditability
Failures, retries, and SLA breaches shall leave durable audit trails in existing operational tables.

### NFR-006 Production Hygiene
Healthchecks and repo-wide linting shall be treated as release gates for this phase.

### NFR-007 Backward Compatibility
Changes shall preserve compatibility with the current API routes, terminal operations module, and existing database schema unless a migration is explicitly required and documented.

## Interfaces and Component Design

### Component A: Scheduler Runtime
Responsibilities:
- bootstrap SQLAlchemy session factory
- instantiate APScheduler
- register policy-defined jobs
- invoke job runner functions
- record startup/dispatch logs
- coordinate SLA checks post-run

Suggested module area:
- `apps/scheduler/runtime.py`
- `apps/scheduler/worker.py` or equivalent entrypoint

### Component B: Scheduled Job Runner
Responsibilities:
- map `ScheduledIngestionJob.source_system` to executable workload
- create and update `OpsPipelineRun`
- emit `OpsJobEvent`
- execute retries with backoff
- invoke DQ escalation on exhaustion

### Component C: SLA Monitor Hook
Responsibilities:
- compute deadline in IST
- determine confirmed completion timestamp from actual run completion/raw write confirmation
- create `OpsSlaBreach` for missed deadlines
- attach breach to the actual run

### Component D: API Health Endpoints
Responsibilities:
- return fast liveness response for `/health`
- perform lightweight DB reachability check for `/ready`
- remain unauthenticated

### Component E: Compose / Container Runtime
Responsibilities:
- start scheduler as its own service
- ensure startup ordering via health dependencies
- expose API healthcheck commands

## Data and Persistence Rules

### Persistence Mapping
- Scheduler dispatches create/update `ops_pipeline_run`
- Runtime milestones create `ops_job_event`
- Missed deadlines create `ops_sla_breach`
- Retry exhaustion creates `dq_result` + `dq_incident`

### Status Mapping
`OpsPipelineRun.status` shall continue to use the existing allowed set:
- `QUEUED`
- `RUNNING`
- `SUCCESS`
- `FAILED`
- `CANCELLED`

`OpsJobEvent.event_type` shall continue to use the existing allowed set:
- `START`
- `END`
- `WARN`
- `ERROR`
- `RETRY`
- `FAILED`

Terminal job failure semantics shall be represented by:
- `OpsPipelineRun.status = 'FAILED'`
- at least one `OpsJobEvent.event_type = 'FAILED'`

The `FAILED` vocabulary addition is a backward-compatible schema migration and makes terminal
connector failure directly queryable.

## Validation Rules

### Retry Classification
Only retry exceptions classified as transient transport/service failures. Validation or schema contract failures shall fail fast and escalate without retry.

### Readiness Check
`/ready` shall only return `200` after a successful lightweight DB query/connection check. Any DB connectivity failure shall return a non-200 status.

### Fundamentals Scheduling Scope
This phase may schedule the fundamentals bootstrap as a daily operational job or explicit startup/default recurring job, but it shall not introduce production-grade filings ingestion beyond the yfinance bootstrap already in scope for development.

## Acceptance Criteria
1. `docker compose up -d` starts `db`, `api`, `terminal`, and `scheduler`.
2. Scheduler logs show at least one startup dry-run / next-fire-time confirmation line per job from `IngestionSchedulerPolicy.daily_jobs()`.
3. `curl http://localhost:8000/health` returns `200` without an `Authorization` header.
4. `curl http://localhost:8000/ready` returns `200` without an `Authorization` header when DB is reachable.
5. A forced connector failure writes an `OpsJobEvent` terminal failure trail and, after retries are exhausted, creates a DQ incident visible via `GET /api/v1/quality/incidents`.
6. An ingestion job that completes after its `sla_deadline_ist` records an `OpsSlaBreach` attached to the actual pipeline run.
7. `pytest -q` remains fully green.
8. New tests cover scheduler triggering, SLA breach persistence, health/ready endpoints, and retry exhaustion to DQ incident path.
9. `ruff check .` passes with zero errors locally.
10. GitHub Actions lint job runs `ruff check .` and remains green.

## Edge Cases
- Scheduler process starts after one or more scheduled times have already passed for the day.
- Duplicate execution risk on scheduler restart during a running job.
- Readiness endpoint during transient DB startup before Postgres accepts connections.
- Connector raises non-transient validation errors that should not be retried.
- Connector succeeds after one or more transient retries but before retry exhaustion.
- Job finishes successfully after SLA deadline; result is both successful and breached.
- SQLite-based tests require manual PK assignment helpers already present across the repo.

## Out of Scope
- Distributed or multi-region scheduling
- Redis/Celery task broker infrastructure
- Leader election between multiple scheduler replicas
- Dynamic user-managed schedule editing via API/UI
- Backfill orchestration for historical dates beyond explicit test scenarios
- Industry, News, Research, or Reporting spec changes
- Replacing the development bootstrap fundamentals source with production filings ingestion

## Design Review Summary

### Architecture Fit
APScheduler in a dedicated scheduler container is the lowest-complexity design that satisfies the operational requirements without conflicting with the API’s async SQLAlchemy usage. The scheduler can use its own synchronous SQLAlchemy session lifecycle and does not need to run in the FastAPI process.

### Risks
- Single scheduler process means no HA scheduling in this phase.
- Retry logic must remain idempotent with existing raw-write semantics.
- Expanding lint to `ruff check .` may surface repo-wide debt that must be cleared before the stricter CI gate is enabled.

### Mitigations
- Keep scheduler isolated in its own process/container.
- Reuse existing idempotent connector writes and DQ persistence patterns.
- Treat lint cleanup as part of the scoped operational hygiene work before flipping CI to repo-wide enforcement.

## Verification Plan
- Unit tests for retry classification/backoff behavior and health/readiness helpers
- Integration tests for scheduler-triggered run persistence and SLA breach recording
- Integration test for retry exhaustion to DQ incident path
- Full regression run with `pytest -q`
- Repo-wide lint verification with `ruff check .`
- Manual container validation via `docker compose up -d` and endpoint curl checks

## Release Decision Criteria
Release this phase only when:
- spec artifact exists and matches implementation
- `pytest -q` is green
- `ruff check .` is green
- scheduler service starts and logs job registration
- API liveness/readiness endpoints work unauthenticated
- retry exhaustion and SLA breach persistence are demonstrated by automated tests
