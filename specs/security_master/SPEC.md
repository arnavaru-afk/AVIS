# Security Master Specification

Status: Draft  
Owner: Spec Architect  
Phase: 4 (Security Master)

## Objective
Establish a canonical Security Master System that resolves company identity across tickers, ISINs, exchanges, aliases, and corporate actions so all downstream AVIS modules operate on a consistent entity identity.

## Business Rules
- A single economic entity must map to one canonical `company_id`.
- A tradable listing must map to one canonical `instrument_id` per exchange.
- Ticker aliases across vendors/exchanges/time must resolve deterministically.
- Identity resolution must be point-in-time aware.
- Corporate actions must preserve continuity between pre- and post-action identifiers.
- Resolution confidence and provenance must be stored and auditable.

## Functional Requirements
- Define canonical entity hierarchy:
  - Company identity layer
  - Instrument identity layer
  - Listing/exchange identity layer
- Define mapping rules for:
  - Company name variants
  - Ticker mapping
  - ISIN mapping
  - Exchange code mapping
  - Vendor symbol mapping
- Define alias tracking lifecycle:
  - Alias creation
  - Alias validity windows
  - Alias deactivation
- Define corporate action continuity rules for:
  - Splits
  - Bonus issues
  - Rights issues
  - Mergers
  - Demergers
  - Symbol changes
- Define resolution pipeline states:
  - Matched
  - Ambiguous
  - Unresolved
  - Manually resolved
- Define exception workflow for unresolved/ambiguous identities.
- Define versioning strategy for identity decisions and remaps.

## Non-Functional Requirements
- Deterministic resolution for identical inputs.
- Low-latency lookup for real-time ingestion pipelines.
- Full auditability of all mapping decisions.
- Idempotent remap/replay behavior for backfills.
- High availability for identity lookup service dependency.
- Backward compatibility guarantees for historical IDs.

## Acceptance Criteria
- Canonical mapping can resolve equivalent identifiers to a single entity:
  - `ITC`
  - `ITC.NS`
  - `INE154A01025`
  - all must map to one `company_id` and correct `instrument_id`.
- Resolution output includes:
  - canonical IDs
  - matched input key
  - rule used
  - confidence score
  - source provenance
  - effective timestamp
- Alias validity windows prevent overlapping active aliases for same source namespace.
- Corporate action events preserve historical linkage and do not orphan prior records.
- Ambiguous mappings are routed to manual review queue with reason codes.
- Reprocessing same source payload produces identical mapping outcomes.

## Edge Cases
- Same ticker symbol reused across different exchanges.
- Same company with multiple share classes and similar names.
- ISIN changes due to restructuring.
- Temporary symbol suspension/reactivation.
- Legacy vendor symbols conflicting with current exchange symbols.
- Demerger where one legacy symbol maps to multiple successor entities.

## Out of Scope
- Execution/routing logic for trading systems.
- Portfolio allocation or optimization rules.
- External legal entity registry procurement workflows.
