# Reporting Specification

## Objective
Define output standards for institutional research notes, dashboards, API payloads, and valuation attribution reporting.

## Business Rules
- Every published valuation must include timestamp and confidence context.
- Reported insights must be traceable to validated upstream data.
- Point-in-time consistency is required across all report sections.
- Sensitive/internal-only fields must be access controlled.

## Functional Requirements
- Define report types and required sections.
- Define API response contracts for core research endpoints.
- Define scheduled reporting workflows and artifact versioning.
- Define valuation attribution and what-changed report requirements.
- Define role-based access requirements for report visibility.

## Non-Functional Requirements
- Latency targets for API and dashboard reads.
- Availability targets for reporting services.
- Export reliability targets for scheduled notes.
- Observability requirements for report generation failures.
- Accessibility and readability standards for research consumers.

## Acceptance Criteria
- Reporting contract catalog is documented.
- Required fields per report type are approved.
- Access-control matrix is documented.
- Attribution and confidence display rules are documented.
- Reporting QA checklist is approved.

## Edge Cases
- Partial upstream data availability at reporting cutoff time.
- Conflicting values across cached and fresh data windows.
- Backfilled data changing prior published values.
- Large client batch export at market open.

## Out of Scope
- Frontend styling implementation details.
- Client-specific branding templates.
- Distribution channel commercial packaging.
