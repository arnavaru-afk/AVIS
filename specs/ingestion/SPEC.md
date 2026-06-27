# Ingestion Specification

## Objective
Define reliable and compliant ingestion standards for exchange data, filings, transcripts, and news sources.

## Business Rules
- No source is ingested without documented usage rights.
- Source contracts must exist before production onboarding.
- Ingestion must preserve raw payload and metadata.
- Duplicate event handling must be deterministic.

## Functional Requirements
- Define source onboarding process and contract checks.
- Define ingestion modes (batch, streaming, scheduled pull).
- Define metadata captured per ingestion event.
- Define deduplication and idempotency requirements.
- Define retry, backoff, and dead-letter handling behavior.

## Non-Functional Requirements
- Throughput targets by source category.
- Freshness SLA by source category.
- Error budget and alert thresholds.
- Resilience requirements for partial upstream failure.
- Cost-efficiency constraints for high-frequency feeds.

## Acceptance Criteria
- Source inventory and ownership are documented.
- Ingestion contract checklist is complete per source.
- Idempotency and dedupe rules are defined.
- Failure handling runbook is documented.
- SLA and monitoring requirements are approved.

## Edge Cases
- Source schema drift without prior notice.
- Source anti-bot protection changes.
- Rate-limit spikes during major market events.
- Duplicate payloads with different timestamps.

## Out of Scope
- Connector implementation code.
- Third-party procurement negotiation details.
- UI workflows for source administration.

## Connector-Level Specifications

### Connector 1: NSE EOD Connector

#### Objective
- Ingest daily NSE Bhav Copy end-of-day market data into the raw zone as the canonical EOD exchange payload for NSE-listed instruments.

#### Source Definition
- Source: `nseindia.com` Bhav Copy public daily CSV
- Frequency: once per trading day
- Trigger schedule: daily at `18:30 IST` after expected Bhav Copy publication
- SLA breach threshold: if Bhav Copy is unavailable by `20:00 IST`, raise an `ops_sla_breach` event

#### Schema Contract
- `symbol` : `string` : not null
- `series` : `string` : not null
- `open` : `decimal(18,6)` : not null
- `high` : `decimal(18,6)` : not null
- `low` : `decimal(18,6)` : not null
- `close` : `decimal(18,6)` : not null
- `volume` : `decimal(24,0)` : not null
- `delivery_qty` : `decimal(24,0)` : nullable
- `timestamp` : `date` : not null

#### Validation Rules Before Writing to Raw Zone
- File must be parseable as CSV with expected header names.
- `symbol` must be non-empty after trim and normalized to uppercase.
- `series` must be non-empty after trim.
- Price fields must be numeric and non-negative.
- `high` must be greater than or equal to `open`, `low`, and `close`.
- `low` must be less than or equal to `open`, `high`, and `close`.
- `volume` and `delivery_qty` must be zero or positive when present.
- `timestamp` must equal the expected trading date for the run.
- Rows failing parse or numeric conversion must be quarantined and counted in ingestion-quality metrics.

#### Failure and Retry Policy
- Initial fetch at `18:30 IST`.
- Retry cadence: every 15 minutes until `20:00 IST`.
- Retry class: transient transport, DNS, TLS, timeout, HTTP `429`, HTTP `5xx`.
- Non-retry class: permanent schema mismatch, malformed payload, compliance block.
- If unavailable by `20:00 IST`, mark pipeline run failed and emit SLA breach.

#### robots.txt / ToS Compliance Status
- Compliance status: `Conditional`
- Public file access may still be subject to exchange terms and downstream usage restrictions.
- Production use requires legal review of exchange terms before commercial redistribution or derivative analytics usage.
- Connector must be disabled if legal/compliance review status is not approved.

#### Rate Limiting Constraints
- Maximum one scheduled fetch attempt per 15-minute retry window.
- No concurrent duplicate fetches for the same trading date.
- Backoff is mandatory after HTTP `429` or anti-bot response.

#### Idempotency Guarantee
- Idempotency key: `(source_system, trading_date, payload_hash)`
- Reprocessing the same Bhav Copy file for the same trading date must not create duplicate raw-zone records.
- A corrected file for the same date must be stored as a distinct raw payload version with lineage linkage.

### Connector 2: BSE EOD Connector

#### Objective
- Ingest daily BSE Bhav Copy end-of-day market data into the raw zone for BSE-listed instruments.

#### Source Definition
- Source: `bseindia.com` Bhav Copy
- Frequency: once per trading day
- Trigger schedule: daily after expected BSE Bhav Copy availability
- Run timing target: align with same-day AVIS EOD processing window

#### Schema Contract
- `symbol` : `string` : not null
- `series` : `string` : not null
- `open` : `decimal(18,6)` : not null
- `high` : `decimal(18,6)` : not null
- `low` : `decimal(18,6)` : not null
- `close` : `decimal(18,6)` : not null
- `volume` : `decimal(24,0)` : not null
- `delivery_qty` : `decimal(24,0)` : nullable
- `timestamp` : `date` : not null

#### Validation Rules Before Writing to Raw Zone
- File must be parseable as CSV with expected BSE header definitions.
- BSE-specific `series` codes must map to approved BSE series taxonomy.
- `symbol` must be normalized to uppercase and trimmed.
- Price integrity rules match NSE EOD validation rules.
- `timestamp` must equal the intended BSE trade date.
- Unknown BSE series codes must be quarantined for mapping review.

#### Failure and Retry Policy
- Retry cadence: every 15 minutes until the EOD ingestion cutoff window closes.
- Retry class: transient transport, timeout, HTTP `429`, HTTP `5xx`.
- Non-retry class: malformed file, schema drift, compliance block.
- Repeated failure past cutoff must raise ingestion incident and freshness alert.

#### robots.txt / ToS Compliance Status
- Compliance status: `Conditional`
- Public file accessibility does not eliminate contractual usage review requirements.
- Production activation requires explicit legal/compliance approval for exchange usage.

#### Rate Limiting Constraints
- Maximum one active fetch attempt at a time per trade date.
- Retry jitter required to avoid synchronized scraping patterns.
- Aggressive polling before expected publication time is prohibited.

#### Idempotency Guarantee
- Idempotency key: `(source_system, trading_date, payload_hash)`
- Same-file replays must be no-op for raw-zone duplication.
- Corrected re-publications must be stored as versioned payloads with source lineage.

### Connector 3: NSE Corporate Actions Connector

#### Objective
- Ingest NSE corporate action events into the raw zone and support downstream normalization into `market_corporate_action`.

#### Source Definition
- Source: `nseindia.com` corporate actions endpoint
- Event types in scope:
  - dividend
  - split
  - bonus
  - rights
  - amalgamation

#### Schema Contract
- `symbol` : `string` : not null
- `series` : `string` : nullable
- `company_name` : `string` : nullable
- `action_type` : `string` : not null
- `announcement_date` : `date` : nullable
- `ex_date` : `date` : not null
- `record_date` : `date` : nullable
- `purpose` : `string` : nullable
- `ratio_text` : `string` : nullable
- `cash_amount` : `decimal(20,8)` : nullable
- `source_timestamp` : `datetime` : not null

#### Validation Rules Before Writing to Raw Zone
- Payload must be parseable and conform to expected NSE corporate action field set.
- `action_type` must normalize to approved taxonomy values.
- `ex_date` must be present and parseable.
- At least one entity key must be present:
  - `symbol`
  - `company_name`
- `cash_amount` must be numeric and zero or positive when present.
- `ratio_text` may remain raw in ingestion; structured parsing is deferred to normalization.

#### Failure and Retry Policy
- Retry class: timeout, HTTP `429`, HTTP `5xx`, transient endpoint unavailability.
- Retry cadence: exponential backoff with upper bound of 30 minutes between attempts.
- Non-retry class: invalid payload schema, compliance block, unrecoverable parse error.
- Persistent failure must raise ingestion incident and block downstream normalization for that run.

#### robots.txt / ToS Compliance Status
- Compliance status: `Conditional`
- Endpoint use must be reviewed against exchange website terms before production enablement.
- No uncontrolled concurrent scraping is permitted.

#### Rate Limiting Constraints
- Maximum one active pull per connector instance.
- Burst fetching prohibited.
- Respect server throttling and anti-bot responses with immediate backoff.

#### Idempotency Guarantee
- Deduplication key in normalized target: `(instrument_id, ex_date, action_type)`
- Raw-zone idempotency key: `(source_system, source_timestamp, payload_hash)`
- Replays of identical payloads must not create duplicate downstream events.

### Connector 4: Filings Connector (Stub Specification Only)

#### Objective
- Define the raw-zone ingestion contract for exchange announcement and filings payload capture while deferring parsing and business extraction to the normalization layer.

#### Source Definition
- Source: BSE/NSE announcement feeds
- Raw payload formats in scope:
  - XML
  - JSON

#### Schema Contract
- `source_system` : `string` : not null
- `feed_name` : `string` : not null
- `external_id` : `string` : nullable
- `symbol` : `string` : nullable
- `company_name` : `string` : nullable
- `published_at` : `datetime` : nullable
- `captured_at` : `datetime` : not null
- `content_type` : `string` : not null
- `raw_payload` : `bytes or text blob` : not null
- `payload_hash` : `string` : not null

#### Validation Rules Before Writing to Raw Zone
- Payload must be syntactically valid XML or JSON according to declared `content_type`.
- `captured_at` must be populated from ingestion runtime.
- `payload_hash` must be computed before persistence.
- Empty payloads are rejected.
- Parsing into business fields beyond lightweight metadata extraction is explicitly deferred.

#### Failure and Retry Policy
- Retry class: transport failure, timeout, HTTP `429`, HTTP `5xx`.
- Non-retry class: malformed transport envelope, compliance block, unsupported content type.
- Repeated failures create ingestion incident; parsing failures are handled later in normalization.

#### robots.txt / ToS Compliance Status
- Compliance status: `Conditional`
- Exchange feed usage must be reviewed and approved before production ingestion.
- Stub remains non-production until legal/compliance sign-off is complete.

#### Rate Limiting Constraints
- Follow exchange feed polling limits once production values are approved.
- Until then, connector remains stub-only and implementation-deferred.

#### Idempotency Guarantee
- Idempotency key: `(source_system, external_id, payload_hash)` when `external_id` exists
- Fallback idempotency key: `(source_system, captured_date, payload_hash)`
- Identical raw payload replays must not duplicate raw-zone records.
