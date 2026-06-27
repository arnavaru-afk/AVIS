"""Confidence scoring for valuation trust signals."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class ConfidenceInput:
    data_quality_score: Decimal
    peer_count: int
    assumption_stability: Decimal
    model_agreement: Decimal


@dataclass(frozen=True, slots=True)
class ConfidenceScoreResult:
    composite_score: Decimal
    data_quality_score: Decimal
    peer_count_score: Decimal
    assumption_stability_score: Decimal
    model_agreement_score: Decimal
    analyst_override_required: bool

    def as_payload(self) -> dict[str, object]:
        return {
            "composite_score": str(self.composite_score),
            "data_quality_score": str(self.data_quality_score),
            "peer_count_score": str(self.peer_count_score),
            "assumption_stability_score": str(self.assumption_stability_score),
            "model_agreement_score": str(self.model_agreement_score),
            "analyst_override_required": self.analyst_override_required,
        }


class ConfidenceScoringEngine:
    """Scores a valuation run using the approved v1 input factors."""

    def score(self, inputs: ConfidenceInput) -> ConfidenceScoreResult:
        peer_count_score = min(Decimal(inputs.peer_count) * Decimal("20"), Decimal("100"))
        composite = (
            Decimal(inputs.data_quality_score)
            + peer_count_score
            + Decimal(inputs.assumption_stability)
            + Decimal(inputs.model_agreement)
        ) / Decimal("4")
        composite = composite.quantize(Decimal("0.01"))
        return ConfidenceScoreResult(
            composite_score=composite,
            data_quality_score=Decimal(inputs.data_quality_score).quantize(Decimal("0.01")),
            peer_count_score=peer_count_score.quantize(Decimal("0.01")),
            assumption_stability_score=Decimal(inputs.assumption_stability).quantize(Decimal("0.01")),
            model_agreement_score=Decimal(inputs.model_agreement).quantize(Decimal("0.01")),
            analyst_override_required=composite < Decimal("40"),
        )
