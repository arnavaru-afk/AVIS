# Valuation Specification

## Objective
Define institutional-grade valuation standards across DCF, relative valuation, SOTP, scenario modeling, and confidence attribution.

## Business Rules
- Valuation outputs must be explainable and reproducible.
- Assumptions must be versioned and auditable.
- Model disagreement must be visible, not hidden.
- Low-confidence outputs require analyst escalation workflow.

## Functional Requirements
- Define methodology requirements for DCF, relative, and SOTP.
- Define scenario framework requirements including probability weighting.
- Define confidence scoring components and aggregation rules.
- Define valuation attribution requirements for change analysis.
- Define governance for overrides and approval states.

## Non-Functional Requirements
- Runtime targets for full coverage valuation cycles.
- Determinism requirements for identical input reruns.
- Traceability requirements from output to assumptions/news/events.
- Stability requirements for model update releases.
- Backtesting and calibration reporting requirements.

## Acceptance Criteria
- Methodology specification is approved by Quant and Valuation owners.
- Assumption schema and versioning rules are documented.
- Attribution output requirements are documented.
- Confidence scoring rubric is documented.
- Validation framework and thresholds are documented.

## Edge Cases
- Negative cash flow businesses with unstable margins.
- Conglomerates with sparse segment disclosure.
- Extreme macro shocks causing regime discontinuity.
- High model dispersion with insufficient data quality.

## Out of Scope
- Broker-specific target price policies.
- Portfolio construction optimization logic.
- Trade execution recommendations.
