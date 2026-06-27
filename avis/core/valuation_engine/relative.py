"""Relative valuation engine using peer multiple distributions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from statistics import median
from typing import Protocol


class InsufficientPeerSetError(ValueError):
    """Raised when too few peers remain after validation."""


class CompPeerMapProvider(Protocol):
    """Security-master peer map provider."""

    def get_peer_ids(self, instrument_id: int, *, as_of_date: date) -> list[int]:
        """Return peer instrument ids for the target instrument."""


class PeerMetricsProvider(Protocol):
    """Provider of peer market and accounting metrics."""

    def get_metrics(self, instrument_id: int, *, as_of_date: date) -> "RelativePeerPoint":
        """Return metrics used by relative valuation."""


@dataclass(frozen=True, slots=True)
class RelativePeerPoint:
    instrument_id: int
    enterprise_value: Decimal
    equity_value: Decimal
    ebitda: Decimal
    earnings: Decimal
    book_value: Decimal
    sales: Decimal
    net_debt: Decimal
    share_count: Decimal


@dataclass(frozen=True, slots=True)
class RelativeImpliedRange:
    low: Decimal
    median: Decimal
    high: Decimal


@dataclass(frozen=True, slots=True)
class RelativeMultipleResult:
    multiple_name: str
    peer_count: int
    multiple_range: RelativeImpliedRange
    implied_equity_value_range: RelativeImpliedRange
    implied_target_price_range: RelativeImpliedRange
    excluded_peers: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class RelativeValuationResult:
    point_estimate_equity_value: Decimal
    point_estimate_target_price: Decimal
    point_estimate_enterprise_value: Decimal
    multiple_results: tuple[RelativeMultipleResult, ...]

    def as_payload(self) -> dict[str, object]:
        return {
            "point_estimate_equity_value": str(self.point_estimate_equity_value),
            "point_estimate_target_price": str(self.point_estimate_target_price),
            "point_estimate_enterprise_value": str(self.point_estimate_enterprise_value),
            "multiple_results": [
                {
                    "multiple_name": result.multiple_name,
                    "peer_count": result.peer_count,
                    "multiple_range": {
                        "low": str(result.multiple_range.low),
                        "median": str(result.multiple_range.median),
                        "high": str(result.multiple_range.high),
                    },
                    "implied_equity_value_range": {
                        "low": str(result.implied_equity_value_range.low),
                        "median": str(result.implied_equity_value_range.median),
                        "high": str(result.implied_equity_value_range.high),
                    },
                    "implied_target_price_range": {
                        "low": str(result.implied_target_price_range.low),
                        "median": str(result.implied_target_price_range.median),
                        "high": str(result.implied_target_price_range.high),
                    },
                    "excluded_peers": list(result.excluded_peers),
                }
                for result in self.multiple_results
            ],
        }


class RelativeEngine:
    """Computes relative valuation from peer median/IQR multiples."""

    SUPPORTED_MULTIPLES = ("EV/EBITDA", "P/E", "P/B", "EV/Sales")

    def __init__(
        self,
        *,
        comp_peer_map: CompPeerMapProvider,
        peer_metrics_provider: PeerMetricsProvider,
        minimum_valid_peers: int = 3,
    ) -> None:
        self._comp_peer_map = comp_peer_map
        self._peer_metrics_provider = peer_metrics_provider
        self._minimum_valid_peers = minimum_valid_peers

    def run(
        self,
        *,
        target_instrument_id: int,
        as_of_date: date,
        target_metrics: RelativePeerPoint,
        multiples: tuple[str, ...] | None = None,
    ) -> RelativeValuationResult:
        selected_multiples = multiples or self.SUPPORTED_MULTIPLES
        peer_ids = self._comp_peer_map.get_peer_ids(target_instrument_id, as_of_date=as_of_date)
        peer_metrics = [
            self._peer_metrics_provider.get_metrics(peer_id, as_of_date=as_of_date)
            for peer_id in peer_ids
        ]

        results: list[RelativeMultipleResult] = []
        for multiple_name in selected_multiples:
            results.append(self._run_single_multiple(multiple_name, target_metrics, peer_metrics))

        if not results:
            raise InsufficientPeerSetError("No valid relative multiples were available")

        point_estimate_equity_value = Decimal(str(median([result.implied_equity_value_range.median for result in results])))
        point_estimate_target_price = Decimal(str(median([result.implied_target_price_range.median for result in results])))
        point_estimate_enterprise_value = point_estimate_equity_value + target_metrics.net_debt
        return RelativeValuationResult(
            point_estimate_equity_value=point_estimate_equity_value,
            point_estimate_target_price=point_estimate_target_price,
            point_estimate_enterprise_value=point_estimate_enterprise_value,
            multiple_results=tuple(results),
        )

    def _run_single_multiple(
        self,
        multiple_name: str,
        target_metrics: RelativePeerPoint,
        peers: list[RelativePeerPoint],
    ) -> RelativeMultipleResult:
        valid_multiples: list[Decimal] = []
        excluded_peers: list[int] = []
        for peer in peers:
            maybe_multiple = self._peer_multiple(multiple_name, peer)
            if maybe_multiple is None:
                excluded_peers.append(peer.instrument_id)
                continue
            valid_multiples.append(maybe_multiple)

        if len(valid_multiples) < self._minimum_valid_peers:
            raise InsufficientPeerSetError(
                f"Insufficient valid peers for {multiple_name}: {len(valid_multiples)}"
            )

        q1, med, q3 = _quartiles(valid_multiples)
        implied_low_equity, implied_low_price = self._implied_values(multiple_name, q1, target_metrics)
        implied_med_equity, implied_med_price = self._implied_values(multiple_name, med, target_metrics)
        implied_high_equity, implied_high_price = self._implied_values(multiple_name, q3, target_metrics)
        return RelativeMultipleResult(
            multiple_name=multiple_name,
            peer_count=len(valid_multiples),
            multiple_range=RelativeImpliedRange(low=q1, median=med, high=q3),
            implied_equity_value_range=RelativeImpliedRange(
                low=implied_low_equity,
                median=implied_med_equity,
                high=implied_high_equity,
            ),
            implied_target_price_range=RelativeImpliedRange(
                low=implied_low_price,
                median=implied_med_price,
                high=implied_high_price,
            ),
            excluded_peers=tuple(excluded_peers),
        )

    @staticmethod
    def implied_value(
        *,
        multiple_name: str,
        multiple: Decimal,
        target_metrics: RelativePeerPoint,
    ) -> tuple[Decimal, Decimal]:
        return RelativeEngine._implied_values(multiple_name, multiple, target_metrics)

    @staticmethod
    def _peer_multiple(multiple_name: str, peer: RelativePeerPoint) -> Decimal | None:
        if multiple_name == "EV/EBITDA":
            if peer.ebitda <= 0:
                return None
            return peer.enterprise_value / peer.ebitda
        if multiple_name == "P/E":
            if peer.earnings <= 0:
                return None
            return peer.equity_value / peer.earnings
        if multiple_name == "P/B":
            if peer.book_value <= 0:
                return None
            return peer.equity_value / peer.book_value
        if multiple_name == "EV/Sales":
            if peer.sales <= 0:
                return None
            return peer.enterprise_value / peer.sales
        raise ValueError(f"Unsupported multiple: {multiple_name}")

    @staticmethod
    def _implied_values(
        multiple_name: str,
        multiple: Decimal,
        target_metrics: RelativePeerPoint,
    ) -> tuple[Decimal, Decimal]:
        if target_metrics.share_count <= 0:
            raise ValueError("share_count must be positive")
        if multiple_name == "EV/EBITDA":
            enterprise_value = multiple * target_metrics.ebitda
            equity_value = enterprise_value - target_metrics.net_debt
        elif multiple_name == "P/E":
            equity_value = multiple * target_metrics.earnings
        elif multiple_name == "P/B":
            equity_value = multiple * target_metrics.book_value
        elif multiple_name == "EV/Sales":
            enterprise_value = multiple * target_metrics.sales
            equity_value = enterprise_value - target_metrics.net_debt
        else:
            raise ValueError(f"Unsupported multiple: {multiple_name}")
        target_price = equity_value / target_metrics.share_count
        return equity_value, target_price


def _quartiles(values: list[Decimal]) -> tuple[Decimal, Decimal, Decimal]:
    ordered = sorted(values)
    med = Decimal(str(median(ordered)))
    midpoint = len(ordered) // 2
    lower_half = ordered[:midpoint]
    upper_half = ordered[midpoint + (0 if len(ordered) % 2 == 0 else 1) :]
    q1 = Decimal(str(median(lower_half or ordered)))
    q3 = Decimal(str(median(upper_half or ordered)))
    return q1, med, q3
