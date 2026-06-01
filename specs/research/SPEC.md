# Research Intelligence Specification

## Objective
Define the end-to-end research workflow architecture for thesis generation, memory tracking, monitorables, and analyst decision support.

## Business Rules
- Research outputs must distinguish fact, inference, and assumption.
- Thesis evolution must be time-versioned and auditable.
- Monitorables must map to valuation drivers.
- Human overrides must capture rationale and impact.

## Functional Requirements
- Define thesis template requirements (bull, bear, risks, catalysts, monitorables).
- Define research memory schema requirements for quarterly change tracking.
- Define what-changed workflow requirements across valuation cycles.
- Define analyst question-generation and override lifecycle requirements.
- Define recommendation state machine and review checkpoints.

## Non-Functional Requirements
- Consistency requirements for repeated research generation.
- Traceability requirements from thesis claims to evidence.
- Latency targets for research refresh after new critical events.
- Audit and retention requirements for analyst interventions.
- Security requirements for role-based research access.

## Acceptance Criteria
- Research note structure is documented and approved.
- Memory and change-tracking requirements are documented.
- Override workflow requirements are documented.
- Evidence traceability requirements are documented.
- Governance review checklist is approved.

## Edge Cases
- New information invalidating key prior thesis assumptions.
- Divergence between model output and analyst judgment.
- Conflicting catalysts with opposite valuation impact.
- Incomplete evidence at decision deadlines.

## Out of Scope
- Trading execution tooling.
- Portfolio risk budgeting workflow.
- Investor-facing marketing collateral.
