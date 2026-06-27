"""Deterministic DCF valuation engine for AVIS v1."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal

from avis.core.valuation_engine.assumptions import DcfAssumptions


@dataclass(frozen=True, slots=True)
class DcfProjectionInput:
    """DCF input bundle with explicit non-EBIT schedules."""

    revenue_base: Decimal
    tax_rate: Decimal
    share_count: Decimal
    net_debt: Decimal
    depreciation_and_amortization: tuple[Decimal, ...]
    capex: tuple[Decimal, ...]
    delta_nwc: tuple[Decimal, ...]


@dataclass(frozen=True, slots=True)
class DcfProjectionLine:
    """One projected year of the DCF model."""

    year_index: int
    revenue: Decimal
    ebit: Decimal
    nopat: Decimal
    depreciation_and_amortization: Decimal
    capex: Decimal
    delta_nwc: Decimal
    free_cash_flow: Decimal
    discount_factor: Decimal
    present_value: Decimal


@dataclass(frozen=True, slots=True)
class DcfResult:
    """Top-level DCF output plus the explainability schedule."""

    intrinsic_value_per_share: Decimal
    equity_value: Decimal
    enterprise_value: Decimal
    terminal_value: Decimal
    terminal_present_value: Decimal
    projection_lines: tuple[DcfProjectionLine, ...]

    def as_payload(self) -> dict[str, object]:
        return {
            "intrinsic_value_per_share": str(self.intrinsic_value_per_share),
            "equity_value": str(self.equity_value),
            "enterprise_value": str(self.enterprise_value),
            "terminal_value": str(self.terminal_value),
            "terminal_present_value": str(self.terminal_present_value),
            "projection_lines": [
                {key: str(value) for key, value in asdict(line).items()}
                for line in self.projection_lines
            ],
        }


class DcfEngine:
    """Computes DCF values using only approved v1 methodology."""

    def run(self, *, assumptions: DcfAssumptions, projection_input: DcfProjectionInput) -> DcfResult:
        forecast_years = assumptions.forecast_years
        if assumptions.wacc <= assumptions.terminal_growth:
            raise ValueError("WACC must be greater than terminal growth for Gordon Growth Model")
        if len(projection_input.depreciation_and_amortization) != forecast_years:
            raise ValueError("D&A schedule length must match forecast_years")
        if len(projection_input.capex) != forecast_years:
            raise ValueError("Capex schedule length must match forecast_years")
        if len(projection_input.delta_nwc) != forecast_years:
            raise ValueError("Delta NWC schedule length must match forecast_years")
        if projection_input.share_count <= 0:
            raise ValueError("share_count must be positive")

        lines: list[DcfProjectionLine] = []
        total_present_value = Decimal("0")
        for year_index in range(1, forecast_years + 1):
            revenue = projection_input.revenue_base * (Decimal("1") + assumptions.revenue_cagr) ** year_index
            ebit = revenue * assumptions.ebit_margin
            nopat = ebit * (Decimal("1") - projection_input.tax_rate)
            da = projection_input.depreciation_and_amortization[year_index - 1]
            capex = projection_input.capex[year_index - 1]
            delta_nwc = projection_input.delta_nwc[year_index - 1]
            free_cash_flow = nopat + da - capex - delta_nwc
            discount_factor = (Decimal("1") + assumptions.wacc) ** year_index
            present_value = free_cash_flow / discount_factor
            line = DcfProjectionLine(
                year_index=year_index,
                revenue=revenue,
                ebit=ebit,
                nopat=nopat,
                depreciation_and_amortization=da,
                capex=capex,
                delta_nwc=delta_nwc,
                free_cash_flow=free_cash_flow,
                discount_factor=discount_factor,
                present_value=present_value,
            )
            lines.append(line)
            total_present_value += present_value

        final_fcf = lines[-1].free_cash_flow
        terminal_value = self.terminal_value(
            final_year_fcf=final_fcf,
            wacc=assumptions.wacc,
            terminal_growth=assumptions.terminal_growth,
        )
        terminal_present_value = terminal_value / ((Decimal("1") + assumptions.wacc) ** forecast_years)
        enterprise_value = total_present_value + terminal_present_value
        equity_value = enterprise_value - projection_input.net_debt
        intrinsic_value_per_share = equity_value / projection_input.share_count
        return DcfResult(
            intrinsic_value_per_share=intrinsic_value_per_share,
            equity_value=equity_value,
            enterprise_value=enterprise_value,
            terminal_value=terminal_value,
            terminal_present_value=terminal_present_value,
            projection_lines=tuple(lines),
        )

    @staticmethod
    def terminal_value(
        *,
        final_year_fcf: Decimal,
        wacc: Decimal,
        terminal_growth: Decimal,
    ) -> Decimal:
        if wacc <= terminal_growth:
            raise ValueError("WACC must be greater than terminal growth")
        return (final_year_fcf * (Decimal("1") + terminal_growth)) / (wacc - terminal_growth)
