"""KV-cache token-eviction advisor (v4.3) — pure function ATV → EvictionAdvice.

Token-eviction is the granularity-level companion to the placement advisor:

* ``placement_advisor`` (v3.4) decides whole-cache layout: which transformer
  layers go into HBM, what KV-cache quantisation tier to use, when to swap
  to CPU. Every decision applies to the ENTIRE cache.

* ``eviction_advisor`` (v4.3) decides which **individual token positions** to
  drop from the cache. The runtime keeps the policy-recommended subset and
  evicts the rest, freeing HBM at sub-layer granularity.

Two output paths
----------------

1. **Always available — policy + parameters.** Reads the ATV's
   ``prompt_structure`` band slots [9..13] (folded from
   :class:`AttentionSummary` by the builder) plus ``cost_efficiency``
   slots s-10 / s-11 / s-15. Emits one of four policies:

   * ``none`` — no eviction (fits in HBM, or attention is too uniform
     to evict safely)
   * ``sliding_window`` — keep the last K tokens
   * ``streaming_llm`` — keep the first M ("attention sinks") + last K
     tokens (Xiao et al., StreamingLLM 2023)
   * ``h2o`` — keep the top-P % of tokens by attention mass, evict the
     rest (Zhang et al., Heavy-Hitter Oracle 2023)

   With matching parameters: ``keep_recent_tokens``,
   ``keep_attention_sink_tokens``, ``keep_heavy_hitter_pct``.

2. **Available only when the runtime supplies per-token data —
   ``evict_token_indices``.** When ``ATVInput.attention_per_token`` is
   set (a list of float scores, one per token in the current
   sequence), the advisor materialises the policy as concrete
   indices to drop. Without per-token data, this list stays
   ``None`` — never fabricated, never guessed.

Why this surface is honest
--------------------------

Anthropic's API does not expose per-token attention scores, so for
Anthropic-backed sessions ``attention_per_token`` will always be
``None`` and the advisor will only ever produce policy-level advice.
That's correct: we never claim to know which Claude internal token
to drop.

Self-hosted runtimes (vLLM, llama-cpp custom builds with attention
hooks) can supply per-token data and unlock the index-level
output. The advisor is the same pure function in both cases — it
just degrades gracefully when the runtime can't see attention.

Patent linkage
--------------
Claim 33 sibling head — same ATV in, different output projection.
Same M13 attribution structure (frozen weights, sub-millisecond,
deterministic, advisor_hash version-pin).
"""

from __future__ import annotations

import hashlib
import math
import time
from dataclasses import dataclass, field
from typing import Literal

import numpy as np

from aegis.schema import (
    SLICE_COST_EFFICIENCY_METRICS,
    SLICE_NOVELTY_SCORE,
    SLICE_PROMPT_STRUCTURE,
    AttentionSummary,
    ATVInput,
)

EvictionPolicy = Literal["none", "sliding_window", "streaming_llm", "h2o"]


# Indices inside the 16-D cost_efficiency_metrics band.
_COST_S10_CACHE_HIT_RATE = 9
_COST_S11_CONTEXT_UTIL = 10
_COST_S15_TASK_PROGRESS = 14

# Indices inside the 16-D prompt_structure band — populated by the
# builder from AttentionSummary fields (v4.3 fold-in).
_PS_ATTN_ENTROPY = 9
_PS_TOP_K_CONCENTRATION = 10
_PS_SINK_PRESENCE = 11
_PS_RECENCY_BIAS = 12
_PS_EFFECTIVE_RANK = 13

# Defaults for policy parameters.
_DEFAULT_RECENT_TOKENS = 256
_DEFAULT_SINK_TOKENS = 4
_DEFAULT_HEAVY_HITTER_PCT = 0.20

# Confidence weights — rough proportional contribution of each
# signal to advisor confidence (sum-to-1 not enforced, just
# normalisation guides).
_W_ATTENTION_PRESENT = 0.50
_W_CONTEXT_UTIL = 0.30
_W_TASK_PROGRESS = 0.20

_VERSION = "eviction_advisor_v1"
_HASH = hashlib.sha3_256(_VERSION.encode()).hexdigest()


@dataclass(frozen=True)
class EvictionAdvice:
    """Out-of-band advisory payload — runtime decides whether to honour.

    Attributes
    ----------
    policy:
        One of ``none`` / ``sliding_window`` / ``streaming_llm`` /
        ``h2o``. The runtime is free to map this to whatever its own
        eviction machinery supports.
    keep_recent_tokens:
        Window size for ``sliding_window`` and the ``+K recent`` tail
        of ``streaming_llm``.
    keep_attention_sink_tokens:
        First-M token count to retain in ``streaming_llm``. The
        StreamingLLM paper finds 4 is enough.
    keep_heavy_hitter_pct:
        Fraction of tokens to retain under ``h2o``. ``0.20`` means
        keep the top 20 % by attention mass, evict the rest.
    expected_memory_savings_pct:
        Best-effort estimate of KV-cache memory reduction the runtime
        would see if it applied this policy fully.
    evict_token_indices:
        Concrete positions to drop, materialised from
        ``attention_per_token`` when that input is present. ``None``
        signals "policy-only" mode (Anthropic-backed sessions, etc.).
    confidence:
        Self-reported in [0, 1]. The runtime can fall back to its
        own policy below a threshold.
    reasons:
        Human-readable trace of which signals drove the advice.
    latency_ms:
        Wall-clock the advisor took.
    advisor_hash:
        SHA3-256 of the advisor implementation version (audit pin).
    """

    policy: EvictionPolicy = "none"
    keep_recent_tokens: int = 0
    keep_attention_sink_tokens: int = 0
    keep_heavy_hitter_pct: float = 0.0
    expected_memory_savings_pct: float = 0.0
    evict_token_indices: list[int] | None = None
    confidence: float = 0.0
    reasons: list[str] = field(default_factory=list)
    latency_ms: float = 0.0
    advisor_hash: str = ""


# ─────────────────────────────────────────────────────────────────────
# Policy decision tree
# ─────────────────────────────────────────────────────────────────────


def _decide_policy(
    *,
    context_util: float,
    cache_hit_rate: float,
    task_progress: float,
    attn_entropy_norm: float,
    top_k_concentration: float,
    sink_presence: float,
    recency_bias: float,
    has_attention_signal: bool,
) -> tuple[EvictionPolicy, list[str]]:
    """Decide the policy class given the ATV signals.

    Order matters — earlier branches take precedence.
    """
    reasons: list[str] = []

    # 1. Cheap exit: plenty of HBM headroom → don't evict.
    if context_util < 0.50:
        reasons.append(
            f"none: context_util={context_util:.2f} < 0.50 → HBM has headroom"
        )
        return "none", reasons

    # 2. Attention is too uniform to safely evict — every token
    # carries comparable mass. Better to swap whole layers out
    # (placement advisor's job) than to drop tokens.
    if has_attention_signal and attn_entropy_norm > 0.85:
        reasons.append(
            f"none: attention is near-uniform "
            f"(entropy_norm={attn_entropy_norm:.2f} > 0.85) — "
            "no clear heavy hitters to keep"
        )
        return "none", reasons

    # 3. Strong attention sinks + strong recency tail → classic
    # StreamingLLM regime.
    if has_attention_signal and sink_presence > 0.20 and recency_bias > 0.40:
        reasons.append(
            f"streaming_llm: sink_presence={sink_presence:.2f}, "
            f"recency_bias={recency_bias:.2f} → keep first-K + last-N"
        )
        return "streaming_llm", reasons

    # 4. Heavy-tailed attention → H2O.
    if has_attention_signal and top_k_concentration > 0.65:
        reasons.append(
            f"h2o: top_k_concentration={top_k_concentration:.2f} > 0.65 — "
            "attention concentrates on a small subset; keep heavy hitters"
        )
        return "h2o", reasons

    # 5. High recency-only bias → sliding window.
    if has_attention_signal and recency_bias > 0.50:
        reasons.append(
            f"sliding_window: recency_bias={recency_bias:.2f} > 0.50 — "
            "recent context dominates"
        )
        return "sliding_window", reasons

    # 6. No attention signal but context is full — conservative H2O default.
    if context_util >= 0.70:
        if has_attention_signal:
            reasons.append(
                f"h2o (default under context pressure): context_util="
                f"{context_util:.2f} ≥ 0.70 with weak attention signal"
            )
        else:
            reasons.append(
                f"h2o (default under context pressure): context_util="
                f"{context_util:.2f} ≥ 0.70 — no attention signal, "
                "runtime should apply default H2O top-K=20 %"
            )
        return "h2o", reasons

    # 7. Final fallback — context fits, mild eviction is safe.
    reasons.append(
        "sliding_window (default): mid-pressure session, "
        f"context_util={context_util:.2f}, cache_hit={cache_hit_rate:.2f}, "
        f"task_progress={task_progress:.2f}"
    )
    return "sliding_window", reasons


def _expected_savings_pct(
    policy: EvictionPolicy,
    *,
    keep_recent_tokens: int,
    keep_attention_sink_tokens: int,
    keep_heavy_hitter_pct: float,
    n_tokens: int,
) -> float:
    """Best-effort estimate of KV-cache memory reduction.

    Conservative: assumes the runtime applies the policy with
    ``n_tokens`` total in the cache. Returns a fraction in [0, 1].
    """
    if policy == "none" or n_tokens <= 0:
        return 0.0
    if policy == "sliding_window":
        kept = min(keep_recent_tokens, n_tokens)
        return max(0.0, 1.0 - kept / n_tokens)
    if policy == "streaming_llm":
        kept = min(
            keep_recent_tokens + keep_attention_sink_tokens, n_tokens,
        )
        return max(0.0, 1.0 - kept / n_tokens)
    if policy == "h2o":
        return max(0.0, 1.0 - keep_heavy_hitter_pct)
    return 0.0


# ─────────────────────────────────────────────────────────────────────
# Per-token index materialisation
# ─────────────────────────────────────────────────────────────────────


def _materialise_indices(
    policy: EvictionPolicy,
    attention: list[float],
    *,
    keep_recent_tokens: int,
    keep_attention_sink_tokens: int,
    keep_heavy_hitter_pct: float,
) -> list[int]:
    """Translate the policy + parameters into concrete token positions
    to drop, using the runtime-supplied per-token attention scores.

    Returns sorted ascending indices. Empty list ⇒ keep everything.
    """
    n = len(attention)
    if n == 0 or policy == "none":
        return []

    if policy == "sliding_window":
        keep = min(keep_recent_tokens, n)
        if keep >= n:
            return []
        return list(range(0, n - keep))

    if policy == "streaming_llm":
        keep_tail = min(keep_recent_tokens, n)
        keep_head = min(keep_attention_sink_tokens, n - keep_tail)
        if keep_head + keep_tail >= n:
            return []
        return list(range(keep_head, n - keep_tail))

    if policy == "h2o":
        # Keep the top-(keep_heavy_hitter_pct * n) by attention mass.
        n_keep = max(1, int(round(keep_heavy_hitter_pct * n)))
        if n_keep >= n:
            return []
        # argsort ascending → smallest first; drop those.
        order = np.argsort(np.asarray(attention, dtype=np.float64))
        n_drop = n - n_keep
        drop = sorted(int(i) for i in order[:n_drop])
        return drop

    return []


# ─────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────


def eviction_advisor(
    atv: np.ndarray,
    inp: ATVInput | None = None,
) -> EvictionAdvice:
    """Pure function: 2080-D ATV (+ optional ``ATVInput``) → EvictionAdvice.

    Reads the policy-relevant signals from the ATV's
    ``prompt_structure`` (slots 9..13, folded from
    :class:`AttentionSummary`) and ``cost_efficiency`` (s-10, s-11,
    s-15) bands. When ``inp.attention_per_token`` is supplied,
    materialises ``evict_token_indices`` directly; otherwise leaves
    that field ``None`` and emits policy-only advice.
    """
    t0 = time.perf_counter_ns()

    cost = atv[SLICE_COST_EFFICIENCY_METRICS]
    cache_hit_rate = float(cost[_COST_S10_CACHE_HIT_RATE])
    context_util = float(cost[_COST_S11_CONTEXT_UTIL])
    task_progress = float(cost[_COST_S15_TASK_PROGRESS])

    ps = atv[SLICE_PROMPT_STRUCTURE]
    if ps.size >= 14:
        attn_entropy_norm = float(ps[_PS_ATTN_ENTROPY])
        top_k_concentration = float(ps[_PS_TOP_K_CONCENTRATION])
        sink_presence = float(ps[_PS_SINK_PRESENCE])
        recency_bias = float(ps[_PS_RECENCY_BIAS])
    else:
        attn_entropy_norm = top_k_concentration = 0.0
        sink_presence = recency_bias = 0.0

    nov = atv[SLICE_NOVELTY_SCORE]
    composite_novelty = float(nov[3]) if nov.size >= 4 else 0.0

    has_attention_signal = (
        (attn_entropy_norm + top_k_concentration + sink_presence + recency_bias)
        > 0.0
    )

    policy, reasons = _decide_policy(
        context_util=context_util,
        cache_hit_rate=cache_hit_rate,
        task_progress=task_progress,
        attn_entropy_norm=attn_entropy_norm,
        top_k_concentration=top_k_concentration,
        sink_presence=sink_presence,
        recency_bias=recency_bias,
        has_attention_signal=has_attention_signal,
    )

    # Parameter tuning per policy.
    keep_recent_tokens = 0
    keep_attention_sink_tokens = 0
    keep_heavy_hitter_pct = 0.0
    if policy == "sliding_window":
        # Tighter window when context util is higher.
        scale = 1.0 - max(0.0, min(1.0, context_util - 0.5)) * 0.8
        keep_recent_tokens = max(64, int(_DEFAULT_RECENT_TOKENS * scale))
    elif policy == "streaming_llm":
        keep_recent_tokens = _DEFAULT_RECENT_TOKENS
        keep_attention_sink_tokens = _DEFAULT_SINK_TOKENS
    elif policy == "h2o":
        # When attention is more concentrated, we can keep fewer tokens.
        # Map top_k_concentration ∈ [0, 1] → keep_pct ∈ [0.10, 0.30].
        if has_attention_signal and top_k_concentration > 0:
            keep_heavy_hitter_pct = float(
                np.clip(0.30 - 0.20 * top_k_concentration, 0.10, 0.30)
            )
        else:
            keep_heavy_hitter_pct = _DEFAULT_HEAVY_HITTER_PCT

    # Index materialisation when raw per-token attention is supplied.
    evict_indices: list[int] | None = None
    n_tokens_for_savings = 0
    if inp is not None and inp.attention_per_token:
        evict_indices = _materialise_indices(
            policy,
            inp.attention_per_token,
            keep_recent_tokens=keep_recent_tokens,
            keep_attention_sink_tokens=keep_attention_sink_tokens,
            keep_heavy_hitter_pct=keep_heavy_hitter_pct,
        )
        n_tokens_for_savings = len(inp.attention_per_token)
        reasons.append(
            f"materialised {len(evict_indices)} eviction index(es) "
            f"from {n_tokens_for_savings} per-token scores"
        )
    elif inp is not None and inp.attention_summary is not None:
        n_tokens_for_savings = inp.attention_summary.n_tokens
    else:
        # Fall back to a nominal 2 048-token estimate purely for the
        # savings-pct headline; runtime overrides with reality.
        n_tokens_for_savings = 2048

    expected = _expected_savings_pct(
        policy,
        keep_recent_tokens=keep_recent_tokens,
        keep_attention_sink_tokens=keep_attention_sink_tokens,
        keep_heavy_hitter_pct=keep_heavy_hitter_pct,
        n_tokens=n_tokens_for_savings,
    )

    # Confidence — has_attention contributes the most; context_util
    # and task_progress add a bit. All capped at 1.0.
    conf = 0.0
    if has_attention_signal:
        conf += _W_ATTENTION_PRESENT
    if context_util > 0.05:
        conf += _W_CONTEXT_UTIL * min(1.0, context_util)
    if task_progress > 0.05:
        conf += _W_TASK_PROGRESS * min(1.0, task_progress)
    conf = float(min(1.0, conf))

    # Decorate reasons with composite signals for the audit trail.
    reasons.append(
        f"signals: cache_hit={cache_hit_rate:.2f}, "
        f"novelty={composite_novelty:.2f}, has_attn={has_attention_signal}"
    )

    latency_ms = (time.perf_counter_ns() - t0) / 1e6

    return EvictionAdvice(
        policy=policy,
        keep_recent_tokens=keep_recent_tokens,
        keep_attention_sink_tokens=keep_attention_sink_tokens,
        keep_heavy_hitter_pct=keep_heavy_hitter_pct,
        expected_memory_savings_pct=float(expected),
        evict_token_indices=evict_indices,
        confidence=conf,
        reasons=reasons,
        latency_ms=latency_ms,
        advisor_hash=_HASH,
    )


# ─────────────────────────────────────────────────────────────────────
# Convenience for callers that want a quick AttentionSummary from a
# raw per-token list.
# ─────────────────────────────────────────────────────────────────────


def summarise_attention(
    attention_per_token: list[float],
    *,
    sink_size: int = 4,
    recent_size: int = 32,
    top_k_pct: float = 0.10,
) -> AttentionSummary:
    """Compute an :class:`AttentionSummary` from raw per-token scores.

    Useful for callers that have the raw vector but not the
    aggregate. The advisor itself only needs the summary, but a
    runtime that wants both fields populated can call this once
    per decode and pass the result through ATVInput.
    """
    n = len(attention_per_token)
    if n == 0:
        return AttentionSummary()
    arr = np.asarray(attention_per_token, dtype=np.float64)
    total = float(arr.sum())
    if total <= 0:
        return AttentionSummary(n_tokens=n)
    p = arr / total

    # Entropy
    nz = p[p > 0]
    entropy = float(-(nz * np.log(nz)).sum())
    entropy_max = math.log(n) if n > 1 else 1.0
    entropy_norm = entropy / entropy_max if entropy_max > 0 else 0.0

    # Top-K concentration
    k = max(1, int(round(top_k_pct * n)))
    sorted_p = np.sort(p)[::-1]
    top_k_concentration = float(sorted_p[:k].sum())

    # Sink + recency
    sink = float(p[: min(sink_size, n)].sum())
    recent = float(p[-min(recent_size, n):].sum())

    # Effective rank
    eff_rank = float(min(1.0, math.exp(entropy) / n))

    return AttentionSummary(
        n_tokens=n,
        entropy_normalized=float(min(1.0, max(0.0, entropy_norm))),
        top_k_concentration=float(min(1.0, max(0.0, top_k_concentration))),
        sink_presence=float(min(1.0, max(0.0, sink))),
        recency_bias=float(min(1.0, max(0.0, recent))),
        effective_rank=eff_rank,
    )
