"""Explicit monthly asking-rent scenarios; no tax or occupancy is inferred."""
from __future__ import annotations

from dataclasses import dataclass, asdict
import math


@dataclass(frozen=True)
class NetYieldParams:
    occupancy_share: float = .95
    opex_share: float = .10
    purchase_cost_multiplier: float = 1.02

    def __post_init__(self):
        if (any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
                for value in asdict(self).values())
                or not 0 <= self.occupancy_share <= 1 or not 0 <= self.opex_share <= 1
                or not 1 <= self.purchase_cost_multiplier <= 2):
            raise ValueError('Invalid rental scenario assumptions')

    @classmethod
    def from_settings(cls, getter):
        return cls(float(getter('NET_YIELD_VACANCY', '.95')),
                   float(getter('NET_YIELD_OPEX_SHARE', '.10')),
                   float(getter('NET_YIELD_PURCHASE_COST', '1.02')))


def monthly_yield_scenario(price, rent, params: NetYieldParams) -> dict | None:
    """Preserve the existing formula with visible, configurable assumptions.

    Opex is a user-configured share of scheduled annual rent, not a tax rate.
    The return describes an asking-price scenario, never an achieved yield.
    """
    if (any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0
            for value in (price, rent)) or not isinstance(params, NetYieldParams)):
        return None
    annual = rent * 12
    investment = price * params.purchase_cost_multiplier
    net_annual = annual * (params.occupancy_share - params.opex_share)
    return {'gross_yield_pct': round(100 * annual / price, 1),
            'net_yield_pct': round(100 * net_annual / investment, 1),
            'net_annual': round(net_annual), 'investment': round(investment),
            'gross_payback_years': round(price / annual, 1),
            'net_payback_years': round(investment / net_annual, 1) if net_annual > 0 else None,
            'params_used': asdict(params), 'asking_prices': True, 'scenario': True}
