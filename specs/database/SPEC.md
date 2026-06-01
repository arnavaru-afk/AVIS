# Database Specification

## Objective
Define AVIS data model standards, schema domains, ownership boundaries, retention controls, and query-serving patterns for transactional and analytical use cases.

## Business Rules
- Curated data must be point-in-time correct.
- Raw source data is immutable.
- Schema changes must be versioned and reversible.
- Critical research outputs must be reproducible from stored inputs.

## Functional Requirements
- Define canonical entity model for companies, instruments, and aliases.
- Define schema domains for market, fundamentals, news, valuation, scenarios, confidence, and research memory.
- Define write/read ownership by module.
- Define data lineage capture fields and audit fields.
- Define data lifecycle tiers for hot, warm, and archive data.

## Non-Functional Requirements
- Partitioning/indexing strategy requirements for time-series workloads.
- Read latency requirements for reporting and APIs.
- Concurrency and locking requirements for peak updates.
- Backup and restore RPO/RTO targets.
- Access control and encryption requirements.

## Acceptance Criteria
- ER diagram is complete and approved.
- Every table includes primary key, ownership, and retention policy.
- Data lineage and audit fields are defined for critical tables.
- Migration governance process is documented.
- Performance validation criteria are defined.

## Edge Cases
- Corporate action backfills affecting historical joins.
- Late arriving corrections from upstream data sources.
- Conflicting symbol mappings across exchanges.
- Large-scale restatements requiring selective recomputation.

## Out of Scope
- Vendor-specific SQL tuning scripts.
- ORM implementation choices.
- Physical infrastructure provisioning details.
