"""Anthropic billing pricing tables — cache-aware $ proxy.

The FLOPS-table proxy in :mod:`aegis.cost.model_flops` (FLOPs × $1.5e-15)
is what M12 cost-divergence (``dollar_cost_divergence``) uses — both
sides of the divergence equation are FLOP-derived, so the ratio is
meaningful for HW-vs-SW tampering detection (Claim 27).

But for **operator budget** purposes (step335, fleet alerts, the
``aegis cost summary`` headline number), the FLOP proxy massively
overstates cost because it can't see Anthropic's per-token pricing
nor the cache discounts. A long Claude Code session with 90 % of its
input under ``cache_read_input_tokens`` actually bills at ~10 % of
what the FLOP proxy implies.

This module provides the cache-aware billing proxy. Use cases:

* :class:`aegis.cost.replay.ReplayCall.cumulative_billed_dollars`
  — sits beside ``cumulative_dollars`` so reports can show both.
* ``aegis cost replay`` / ``aegis cost summary`` — render the
  realistic estimate as the headline number with the FLOP proxy
  as a caveat.

Sources / dates
---------------

Per-model rates as of 2026-05-04, from
https://docs.anthropic.com/en/docs/about-claude/pricing. **Update
the table when Anthropic changes the price book** — the dollar
amounts on every audit summary depend on it.

Cache-write 5-minute TTL is used as the default ``cache_creation``
rate (the 1-hour rate is a separate API surface that Claude Code
doesn't currently use in PreToolUse).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelRates:
    """Per-million-token prices for one model.

    All four rates are USD per 1 000 000 tokens. Cache-creation
    refers to the 5-minute TTL tier — this is what Claude Code
    populates as ``cache_creation_input_tokens`` in the transcript
    usage block.
    """

    input_per_mtok: float
    output_per_mtok: float
    cache_read_per_mtok: float       # ~10 % of input (90 % off)
    cache_creation_per_mtok: float   # ~125 % of input (25 % premium)


# Anthropic pricing as of 2026-05-04. Update when prices change.
PRICING_TABLE: dict[str, ModelRates] = {
    # ── Haiku 4.5 ────────────────────────────────────────────────
    "claude-haiku-4-5": ModelRates(
        input_per_mtok=0.80,
        output_per_mtok=4.00,
        cache_read_per_mtok=0.08,
        cache_creation_per_mtok=1.00,
    ),
    "claude-haiku-4-5-20251001": ModelRates(
        input_per_mtok=0.80,
        output_per_mtok=4.00,
        cache_read_per_mtok=0.08,
        cache_creation_per_mtok=1.00,
    ),
    # ── Sonnet 4.5 / 4.6 ─────────────────────────────────────────
    "claude-sonnet-4-5": ModelRates(
        input_per_mtok=3.00,
        output_per_mtok=15.00,
        cache_read_per_mtok=0.30,
        cache_creation_per_mtok=3.75,
    ),
    "claude-sonnet-4-6": ModelRates(
        input_per_mtok=3.00,
        output_per_mtok=15.00,
        cache_read_per_mtok=0.30,
        cache_creation_per_mtok=3.75,
    ),
    # ── Opus 4.7 ─────────────────────────────────────────────────
    "claude-opus-4-7": ModelRates(
        input_per_mtok=15.00,
        output_per_mtok=75.00,
        cache_read_per_mtok=1.50,
        cache_creation_per_mtok=18.75,
    ),
    # ── Generic fallback (Sonnet rates — middle of road) ─────────
    "default": ModelRates(
        input_per_mtok=3.00,
        output_per_mtok=15.00,
        cache_read_per_mtok=0.30,
        cache_creation_per_mtok=3.75,
    ),
}


def get_rates(model_name: str) -> ModelRates:
    r"""Look up rates by model name. Falls back to ``default`` for
    unknown models — operators usually want a non-zero estimate even
    on a misspelled model rather than \$0."""
    return PRICING_TABLE.get(model_name, PRICING_TABLE["default"])


def billed_dollars(
    *,
    model_name: str,
    input_tokens: float = 0.0,
    output_tokens: float = 0.0,
    cache_read_tokens: float = 0.0,
    cache_creation_tokens: float = 0.0,
) -> float:
    """Cache-aware billing estimate — what Anthropic actually charges
    for the given tokens.

    Treats every token at its appropriate rate per :data:`PRICING_TABLE`.
    Returns 0.0 if all token counts are zero (typical for sparse-adapter
    plugin-mode calls before a transcript backfill).

    Note: this is still a *proxy*. The actual invoice might differ by
    a few % (volume discounts, batch tier, region pricing). For
    cryptographic-grade billing, use the M12 Cost Attestation Ledger
    plus :func:`aegis.cost.usage_api.fetch` (PR #4 — Admin API).
    """
    rates = get_rates(model_name)
    return (
        (input_tokens / 1_000_000.0) * rates.input_per_mtok
        + (output_tokens / 1_000_000.0) * rates.output_per_mtok
        + (cache_read_tokens / 1_000_000.0) * rates.cache_read_per_mtok
        + (cache_creation_tokens / 1_000_000.0)
        * rates.cache_creation_per_mtok
    )
