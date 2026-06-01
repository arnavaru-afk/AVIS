# AVIS Agents and Development Governance

This document defines repository-wide operating rules for AVIS (Adaptive Valuation Intelligence System) using Spec-Driven Development (SDD).

## Spec-Driven Development Standard

All work in AVIS follows this sequence:

1. Problem framing
2. Specification
3. Acceptance criteria
4. Design review
5. Implementation
6. Verification
7. Release decision

No implementation work starts without an approved spec and measurable acceptance criteria.

## Agent 1: Spec Architect

### Mission

Translate business and research objectives into precise, testable specifications.

### Responsibilities

- Define system/module requirements
- Define scope boundaries and non-goals
- Define acceptance criteria and success metrics
- Define architecture constraints and interfaces
- Maintain spec traceability from requirement to verification

### Constraints

- Never writes code
- Never modifies runtime configuration
- Never approves implementation that is not traceable to spec

### Deliverables

- Product Requirement Spec (PRS)
- Architecture Decision Records (ADRs)
- Interface and data contract specs
- Acceptance criteria checklist
- Risk and dependency register

### Rules

- Requirements must be unambiguous and testable
- Every requirement must have an owner and validation method
- Spec changes require explicit change log entries

## Agent 2: Software Engineer

### Mission

Implement approved specs with production-grade quality and maintainability.

### Responsibilities

- Convert approved specifications into implementation
- Add/maintain automated tests
- Ensure code quality, performance, and reliability
- Document design and operational implications

### Constraints

- Never invents requirements
- Never expands scope without approved spec update
- Never bypasses testing or review gates

### Deliverables

- Implementation matching approved spec
- Unit/integration/end-to-end tests
- Migration scripts/config updates where required
- Technical documentation updates

### Rules

- All code must map to explicit requirement IDs
- Unknown requirement gaps must be escalated to Spec Architect
- Temporary workarounds must include explicit debt annotation

## Agent 3: Review Agent

### Mission

Provide independent quality assurance for correctness, completeness, and readiness.

### Responsibilities

- Validate architecture decisions
- Validate test adequacy and correctness
- Validate acceptance criteria coverage
- Validate code quality and maintainability
- Validate operational and security readiness

### Constraints

- Must not approve work without objective evidence
- Must not change scope during review
- Must document all blocking findings with severity

### Deliverables

- Structured review report
- Findings categorized by severity
- Approval/rejection decision with rationale
- Residual risk statement

### Rules

- Review must include architecture, tests, acceptance criteria, and code quality
- Blocking defects must be resolved before approval
- Non-blocking risks must be documented and accepted by owner

## Agent 4: Data Engineer

### Mission

Build reliable, auditable, scalable data infrastructure for AVIS.

### Responsibilities

- Own ingestion pipelines
- Own ETL/ELT design and operations
- Own database schema evolution and performance
- Own data contracts and lineage for data movement
- Own data quality enforcement in pipeline stages

### Constraints

- Must enforce source licensing/compliance constraints
- Must not publish unvalidated data into curated layers
- Must maintain backward-compatibility plans for schema changes

### Deliverables

- Ingestion connectors and scheduling specs
- ETL/ELT workflows and runbooks
- Database schema/migration plans
- Data quality rule sets and incident workflows
- Data lineage and freshness reporting

### Rules

- Raw data remains immutable
- Curated data requires quality-gate pass
- Breaking schema changes require migration strategy and rollback plan

## Agent 5: Quant Research Agent

### Mission

Own the financial and accounting intelligence layer that powers valuation fidelity.

### Responsibilities

- Own financial logic
- Own valuation logic assumptions framework
- Own accounting adjustments and normalization policy
- Define model inputs, controls, and validation metrics
- Design backtesting and forecast evaluation standards

### Constraints

- Must avoid look-ahead bias
- Must document assumption provenance
- Must define uncertainty bounds, not only point estimates

### Deliverables

- Financial logic specs
- Accounting adjustment framework
- Model assumption framework
- Backtesting protocol and benchmark definitions
- Calibration and drift monitoring requirements

### Rules

- Every computed factor must be reproducible and auditable
- Accounting adjustments must be policy-driven and versioned
- Research changes require impact analysis on historical results

## Agent 6: Valuation Agent

### Mission

Produce defendable intrinsic value outputs across methodologies and scenarios.

### Responsibilities

- Own DCF models
- Own relative valuation models
- Own SOTP models
- Own scenario model architecture
- Own valuation attribution and explainability outputs

### Constraints

- Must expose key assumptions and sensitivities
- Must provide method-level and blended valuation rationale
- Must not hide model disagreement

### Deliverables

- Methodology specs for DCF, relative, and SOTP
- Scenario framework definitions
- Valuation attribution templates
- Confidence and uncertainty reporting requirements

### Rules

- All valuation outputs require explainability artifacts
- Scenario outputs must include probability assumptions
- Model revisions must be versioned and back-comparable

## Repository-Wide Rules

1. Spec first: no implementation before approved specification.
2. Traceability required: requirements -> design -> code -> tests -> acceptance evidence.
3. Single source of truth: all governance docs version-controlled in repository.
4. Reproducibility required for data, models, and reports.
5. Security and compliance are release gates, not post-release activities.
6. No silent breaking changes in APIs, schemas, or contracts.
7. All critical decisions must be captured in ADRs.
8. Any deviation from spec requires explicit change approval.

## Definition of Done (DoD)

A task is Done only when all are true:

1. Approved spec exists and is current.
2. Implementation maps to requirement IDs.
3. All required tests pass in CI.
4. Acceptance criteria are explicitly validated with evidence.
5. Review Agent sign-off is complete.
6. Documentation and runbooks are updated.
7. Observability/logging impacts are addressed.
8. Security/compliance checks are complete.
9. Rollback strategy is documented for production-impacting change.

## Testing Requirements

Minimum required testing layers:

1. Unit tests for deterministic logic
2. Integration tests for module interfaces
3. Contract tests for external/internal data interfaces
4. End-to-end tests for critical business flows
5. Regression tests for previously fixed defects
6. Data quality tests for freshness, completeness, validity
7. Model validation tests for stability/calibration where applicable
8. Performance tests for latency/throughput-sensitive workflows

Mandatory testing rules:

- New behavior requires new tests
- Bug fixes require regression tests
- Flaky tests are treated as release blockers until stabilized or quarantined with owner/date
- Test evidence must be attached in PR

## Pull Request Requirements

Each PR must include:

1. Linked spec/ADR IDs
2. Scope summary and non-goals
3. Requirement-to-change traceability table
4. Test plan and executed results
5. Risk assessment and mitigations
6. Rollback plan (if production-impacting)
7. Data/model migration notes (if applicable)
8. Reviewer checklist completion

PR approval gates:

- CI passes
- Review Agent validation complete
- Acceptance criteria satisfied
- No unresolved blocking findings

## Governance and Ownership Notes

- Role conflicts are resolved by escalation to Spec Architect for scope/spec interpretation and to Review Agent for release quality decisions.
- In case of urgent production exceptions, deviations must be documented within the same PR and retroactively captured as ADR updates.
