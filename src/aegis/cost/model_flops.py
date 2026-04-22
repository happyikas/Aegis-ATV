"""Model-specific FLOPs/token mapping (patent ¶[0047] j-14 calibration table).

T2 MVP ships rough static estimates. Real product would calibrate per
model + tier from labelled telemetry collected during the Burn-in
Shadow phase, with periodic refresh.

The numbers approximate forward-pass FLOPs for one input or output token
on each named model. They are accurate to within ~2× — what matters for
the divergence math is consistency across the SW vs HW comparison, not
the absolute FLOP value.
"""

from __future__ import annotations

# Sources: published model cards + 2× param-count back-of-envelope.
# Format: model_name → (input_flops_per_token, output_flops_per_token)
FLOPS_PER_TOKEN: dict[str, tuple[float, float]] = {
    # Anthropic
    "claude-haiku-4-5":          (1.4e10, 2.0e10),    # ~7B params
    "claude-haiku-4-5-20251001": (1.4e10, 2.0e10),
    "claude-sonnet-4-6":         (1.4e11, 1.6e11),    # ~70B params (estimated)
    "claude-opus-4":             (3.5e11, 4.0e11),    # ~175B (estimated)
    # OpenAI
    "gpt-4o-mini":               (1.6e10, 2.0e10),    # ~8B (estimated)
    "gpt-4o":                    (2.0e11, 2.5e11),    # ~100B (estimated)
    "text-embedding-3-small":    (8.0e9,  0.0),        # encoder-only, no output tokens
    "text-embedding-3-large":    (3.0e10, 0.0),
    # Generic fallback
    "default":                   (3.0e10, 4.0e10),
}

# Hardware $/FLOP coefficient (model-tier-specific). T2 uses a single
# default; T3 will vary by accelerator (A100 vs H100 vs B200).
DEFAULT_DOLLAR_PER_FLOP: float = 1.0e-15  # ≈ $0.001 per teraFLOP


def expected_flops(model_name: str, input_tokens: float, output_tokens: float) -> float:
    """Return the SW-side expected FLOPs for one inference step."""
    in_flops, out_flops = FLOPS_PER_TOKEN.get(model_name, FLOPS_PER_TOKEN["default"])
    return input_tokens * in_flops + output_tokens * out_flops


def expected_dollars(model_name: str, input_tokens: float, output_tokens: float,
                     dollar_per_flop: float | None = None) -> float:
    """Hardware-derived $ proxy from FLOPs × $/FLOP."""
    f = expected_flops(model_name, input_tokens, output_tokens)
    return f * (dollar_per_flop or DEFAULT_DOLLAR_PER_FLOP)
