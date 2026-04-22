"""Three cost-linkage divergence metrics (patent ¶[0047] j-14..j-16, Claim 26).

Each metric compares the agent's SW-reported cost against a hardware-
derived proxy. T2 has no real hardware telemetry; the HW values default
to 0 and divergences therefore evaluate to 0. The math is identical to
T3's, so the same code path is exercised by tests with synthetic HW
inputs and will activate immediately when real HW telemetry arrives.

In T3 the HW inputs come from per-AID hardware resource-accounting
registers attributed to a specific Agent Identifier through memory-
controller tag enforcement (Claim 3 hardware-side cost pathway).
"""

from __future__ import annotations

from dataclasses import dataclass

from aegis.cost.model_flops import (
    DEFAULT_DOLLAR_PER_FLOP,
    expected_flops,
)
from aegis.schema import CostEfficiencyMetrics


@dataclass(frozen=True)
class DivergenceMetrics:
    """Patent ¶[0047] j-14..j-16, Claim 26 — three divergence metrics."""

    token_to_flops: float    # |SW expected FLOPs − HW measured FLOPs| / SW expected
    memory_cost: float       # |SW context bytes − HW HBM bytes|       / SW context bytes
    dollar_cost: float       # |SW reported $   − HW derived $|       / SW reported $

    def to_array(self) -> list[float]:
        """Map to the last 3 slots (j-14..j-16) of hw_cost_attestation."""
        return [self.token_to_flops, self.memory_cost, self.dollar_cost]


# Default HBM bytes-per-token estimate for the KV cache (4 bytes per
# FP16 weight × 2 for K,V × 32 layers ≈ 256 bytes; conservative).
HBM_BYTES_PER_TOKEN: float = 256.0


def _safe_relative(observed: float, expected: float) -> float:
    """``|observed − expected| / max(expected, ε)``, clamped to [0, 1.0]."""
    eps = 1e-9
    if expected <= eps:
        return 0.0
    rel = abs(observed - expected) / expected
    return min(1.0, rel)


def token_to_flops_divergence(
    metrics: CostEfficiencyMetrics,
    *,
    model_name: str,
    hw_flops_observed: float,
) -> float:
    """¶[0047] j-14: SW token count → expected FLOPs vs HW measured FLOPs.

    T2: ``hw_flops_observed = 0`` → returns 0 (no divergence to report).
    """
    if hw_flops_observed <= 0:
        return 0.0
    expected = expected_flops(
        model_name, metrics.input_token_count, metrics.output_token_count
    )
    return _safe_relative(hw_flops_observed, expected)


def memory_cost_divergence(
    metrics: CostEfficiencyMetrics,
    *,
    hw_hbm_bytes_observed: float,
    bytes_per_token: float = HBM_BYTES_PER_TOKEN,
) -> float:
    """¶[0047] j-15: SW context size → expected HBM bytes vs HW measured."""
    if hw_hbm_bytes_observed <= 0:
        return 0.0
    expected = metrics.cumulative_tokens * bytes_per_token
    return _safe_relative(hw_hbm_bytes_observed, expected)


def dollar_cost_divergence(
    metrics: CostEfficiencyMetrics,
    *,
    model_name: str,
    hw_flops_observed: float,
    dollar_per_flop: float = DEFAULT_DOLLAR_PER_FLOP,
) -> float:
    """¶[0047] j-16: SW $ vs HW-derived $ proxy (FLOPs × $/FLOP).

    Normalized by the SW-reported value so the metric is comparable
    with ``token_to_flops_divergence`` (also SW-normalized).
    """
    if hw_flops_observed <= 0 or metrics.cumulative_dollars <= 0:
        return 0.0
    hw_dollars = hw_flops_observed * dollar_per_flop
    return _safe_relative(hw_dollars, metrics.cumulative_dollars)


def compute_divergence(
    metrics: CostEfficiencyMetrics,
    *,
    model_name: str,
    hw_flops_observed: float = 0.0,
    hw_hbm_bytes_observed: float = 0.0,
    dollar_per_flop: float = DEFAULT_DOLLAR_PER_FLOP,
) -> DivergenceMetrics:
    """Convenience — compute all three at once. T2 callers pass HW=0."""
    return DivergenceMetrics(
        token_to_flops=token_to_flops_divergence(
            metrics, model_name=model_name, hw_flops_observed=hw_flops_observed,
        ),
        memory_cost=memory_cost_divergence(
            metrics, hw_hbm_bytes_observed=hw_hbm_bytes_observed,
        ),
        dollar_cost=dollar_cost_divergence(
            metrics, model_name=model_name,
            hw_flops_observed=hw_flops_observed, dollar_per_flop=dollar_per_flop,
        ),
    )


# Sanity helper for the dashboard's hw_cost_attestation column. Returns
# how the tier_profile / cost_attestation_profile combination should be
# interpreted at render-time.
def divergence_active(tier_profile: str, cost_attestation_profile: str) -> bool:
    return tier_profile == "T3" or cost_attestation_profile in ("hardware", "both")
