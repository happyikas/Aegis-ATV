"""Tests for ``aegis.performance.eviction_advisor`` and the v4.3
``AttentionSummary`` fold-in into ``prompt_structure[9..13]``."""

from __future__ import annotations

import pytest

from aegis.atv.builder import build_atv
from aegis.performance.eviction_advisor import (
    EvictionAdvice,
    eviction_advisor,
    summarise_attention,
)
from aegis.schema import (
    SLICE_PROMPT_STRUCTURE,
    AttentionSummary,
    ATVHeader,
    ATVInput,
    CostEfficiencyMetrics,
)

# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────


def _mk_input(
    *,
    cache_hit: float = 0.0,
    ctx_util: float = 0.0,
    progress: float = 0.0,
    novelty: float = 0.0,
    attn_summary: AttentionSummary | None = None,
    attn_per_token: list[float] | None = None,
) -> ATVInput:
    return ATVInput(
        header=ATVHeader(
            trace_id="t" * 32, span_id="s" * 16,
            tenant_id="demo", aid="aid-test", timestamp_ns=0,
        ),
        tool_name="Bash",
        tool_args_json="{}",
        plan_text="placement-advisor verification",
        cost_estimate=CostEfficiencyMetrics(
            cache_hit_rate=cache_hit,
            context_utilization_ratio=ctx_util,
            task_progress_score=progress,
        ),
        novelty={"composite_novelty": novelty},
        attention_summary=attn_summary,
        attention_per_token=attn_per_token,
    )


# ──────────────────────────────────────────────────────────────────────
# AttentionSummary fold-in into prompt_structure
# ──────────────────────────────────────────────────────────────────────


class TestSummaryFoldIn:
    def test_atv_dim_unchanged_with_summary(self) -> None:
        inp = _mk_input(
            attn_summary=AttentionSummary(
                n_tokens=512, entropy_normalized=0.5,
                top_k_concentration=0.6, sink_presence=0.1,
                recency_bias=0.3, effective_rank=0.2,
            ),
        )
        atv = build_atv(inp)
        assert atv.shape == (2080,)

    def test_slots_zero_without_summary(self) -> None:
        inp = _mk_input()
        ps = build_atv(inp)[SLICE_PROMPT_STRUCTURE]
        assert ps[9:14].tolist() == [0.0, 0.0, 0.0, 0.0, 0.0]

    def test_slots_populated_from_summary(self) -> None:
        s = AttentionSummary(
            n_tokens=2048, entropy_normalized=0.42,
            top_k_concentration=0.78, sink_presence=0.15,
            recency_bias=0.55, effective_rank=0.18,
        )
        ps = build_atv(_mk_input(attn_summary=s))[SLICE_PROMPT_STRUCTURE]
        assert ps[9] == pytest.approx(0.42, abs=1e-5)
        assert ps[10] == pytest.approx(0.78, abs=1e-5)
        assert ps[11] == pytest.approx(0.15, abs=1e-5)
        assert ps[12] == pytest.approx(0.55, abs=1e-5)
        assert ps[13] == pytest.approx(0.18, abs=1e-5)

    def test_slots_clipped_to_unit_interval(self) -> None:
        s = AttentionSummary(
            n_tokens=256, entropy_normalized=2.0,         # over 1
            top_k_concentration=-0.5, sink_presence=10.0,  # under 0 / over 1
            recency_bias=0.5, effective_rank=0.3,
        )
        ps = build_atv(_mk_input(attn_summary=s))[SLICE_PROMPT_STRUCTURE]
        assert 0.0 <= ps[9] <= 1.0
        assert 0.0 <= ps[10] <= 1.0
        assert 0.0 <= ps[11] <= 1.0

    def test_slots_15_reserved_remain_zero(self) -> None:
        s = AttentionSummary(
            n_tokens=128, entropy_normalized=0.5,
            top_k_concentration=0.5, sink_presence=0.5,
            recency_bias=0.5, effective_rank=0.5,
        )
        ps = build_atv(_mk_input(attn_summary=s))[SLICE_PROMPT_STRUCTURE]
        assert ps[14] == 0.0
        assert ps[15] == 0.0


# ──────────────────────────────────────────────────────────────────────
# Policy decision tree
# ──────────────────────────────────────────────────────────────────────


class TestPolicyDecision:
    def test_low_context_returns_none(self) -> None:
        inp = _mk_input(ctx_util=0.20)
        adv = eviction_advisor(build_atv(inp), inp)
        assert adv.policy == "none"
        assert adv.expected_memory_savings_pct == 0.0
        assert any("context_util" in r for r in adv.reasons)

    def test_uniform_attention_returns_none(self) -> None:
        inp = _mk_input(
            ctx_util=0.80,
            attn_summary=AttentionSummary(
                n_tokens=2048, entropy_normalized=0.95,
                top_k_concentration=0.12, sink_presence=0.05,
                recency_bias=0.05, effective_rank=0.90,
            ),
        )
        adv = eviction_advisor(build_atv(inp), inp)
        assert adv.policy == "none"
        assert any("near-uniform" in r for r in adv.reasons)

    def test_streaming_llm_signature(self) -> None:
        inp = _mk_input(
            ctx_util=0.85,
            attn_summary=AttentionSummary(
                n_tokens=4096, entropy_normalized=0.40,
                top_k_concentration=0.55, sink_presence=0.30,
                recency_bias=0.50, effective_rank=0.15,
            ),
        )
        adv = eviction_advisor(build_atv(inp), inp)
        assert adv.policy == "streaming_llm"
        assert adv.keep_attention_sink_tokens > 0
        assert adv.keep_recent_tokens > 0
        assert adv.expected_memory_savings_pct > 0.0

    def test_h2o_when_attention_concentrated(self) -> None:
        inp = _mk_input(
            ctx_util=0.80,
            attn_summary=AttentionSummary(
                n_tokens=2048, entropy_normalized=0.30,
                top_k_concentration=0.85, sink_presence=0.10,
                recency_bias=0.20, effective_rank=0.10,
            ),
        )
        adv = eviction_advisor(build_atv(inp), inp)
        assert adv.policy == "h2o"
        assert 0.10 <= adv.keep_heavy_hitter_pct <= 0.30

    def test_sliding_window_when_only_recency(self) -> None:
        inp = _mk_input(
            ctx_util=0.65,
            attn_summary=AttentionSummary(
                n_tokens=2048, entropy_normalized=0.50,
                top_k_concentration=0.40, sink_presence=0.05,
                recency_bias=0.65, effective_rank=0.40,
            ),
        )
        adv = eviction_advisor(build_atv(inp), inp)
        assert adv.policy == "sliding_window"
        assert adv.keep_recent_tokens >= 64

    def test_h2o_default_under_pressure_no_attention(self) -> None:
        # Context full but no attention signal at all → conservative h2o.
        inp = _mk_input(ctx_util=0.85)
        adv = eviction_advisor(build_atv(inp), inp)
        assert adv.policy == "h2o"
        assert adv.keep_heavy_hitter_pct > 0.0


# ──────────────────────────────────────────────────────────────────────
# evict_token_indices materialisation
# ──────────────────────────────────────────────────────────────────────


class TestEvictionIndices:
    def test_no_attention_per_token_no_indices(self) -> None:
        # Even if policy is non-none, indices list stays None.
        inp = _mk_input(
            ctx_util=0.80,
            attn_summary=AttentionSummary(
                n_tokens=2048, entropy_normalized=0.30,
                top_k_concentration=0.85,
            ),
        )
        adv = eviction_advisor(build_atv(inp), inp)
        assert adv.policy == "h2o"
        assert adv.evict_token_indices is None

    def test_h2o_keeps_heavy_hitters(self) -> None:
        n = 64
        attn = [0.001] * n
        heavy = [3, 7, 11, 19, 23, 31, 47]
        for i in heavy:
            attn[i] = 1.0
        s = summarise_attention(attn)
        inp = _mk_input(
            ctx_util=0.80, attn_summary=s, attn_per_token=attn,
        )
        adv = eviction_advisor(build_atv(inp), inp)
        assert adv.policy == "h2o"
        assert adv.evict_token_indices is not None
        kept = set(range(n)) - set(adv.evict_token_indices)
        # Every heavy hitter must be kept.
        for h in heavy:
            assert h in kept, f"heavy hitter {h} was evicted"

    def test_sliding_window_drops_prefix(self) -> None:
        n = 100
        # Force a sliding-window scenario.
        s = AttentionSummary(
            n_tokens=n, entropy_normalized=0.50,
            top_k_concentration=0.30, sink_presence=0.05,
            recency_bias=0.65, effective_rank=0.40,
        )
        attn = [0.01] * n
        inp = _mk_input(
            ctx_util=0.65, attn_summary=s, attn_per_token=attn,
        )
        adv = eviction_advisor(build_atv(inp), inp)
        assert adv.policy == "sliding_window"
        assert adv.evict_token_indices is not None
        if adv.evict_token_indices:
            # Dropped indices form a contiguous prefix.
            assert adv.evict_token_indices == list(
                range(0, len(adv.evict_token_indices))
            )

    def test_streaming_llm_keeps_sink_and_tail(self) -> None:
        n = 200
        s = AttentionSummary(
            n_tokens=n, entropy_normalized=0.40,
            top_k_concentration=0.55, sink_presence=0.30,
            recency_bias=0.50, effective_rank=0.20,
        )
        attn = [0.01] * n
        inp = _mk_input(
            ctx_util=0.85, attn_summary=s, attn_per_token=attn,
        )
        adv = eviction_advisor(build_atv(inp), inp)
        assert adv.policy == "streaming_llm"
        kept = sorted(set(range(n)) - set(adv.evict_token_indices or []))
        # Head 0..3 (sink) preserved.
        for i in range(adv.keep_attention_sink_tokens):
            assert i in kept
        # Tail (last keep_recent_tokens) preserved.
        for i in range(n - adv.keep_recent_tokens, n):
            if 0 <= i < n:
                assert i in kept

    def test_none_policy_yields_no_indices(self) -> None:
        # Low ctx_util → policy=none. Even with per-token data,
        # evict_token_indices must be empty list (the materialiser
        # returns []) — never None when per-token is present.
        attn = [0.5, 0.3, 0.2]
        inp = _mk_input(ctx_util=0.10, attn_per_token=attn)
        adv = eviction_advisor(build_atv(inp), inp)
        assert adv.policy == "none"
        assert adv.evict_token_indices == []


# ──────────────────────────────────────────────────────────────────────
# summarise_attention
# ──────────────────────────────────────────────────────────────────────


class TestSummariseAttention:
    def test_empty_returns_zero(self) -> None:
        s = summarise_attention([])
        assert s.n_tokens == 0
        assert s.entropy_normalized == 0.0

    def test_uniform_high_entropy(self) -> None:
        s = summarise_attention([1.0] * 100)
        assert s.entropy_normalized > 0.99
        # Top-10 % of uniform = 10 % of mass.
        assert 0.05 <= s.top_k_concentration <= 0.15

    def test_concentrated_low_entropy(self) -> None:
        attn = [0.001] * 100
        attn[0] = 100.0  # all mass on one token
        s = summarise_attention(attn)
        assert s.entropy_normalized < 0.1
        assert s.top_k_concentration > 0.95

    def test_sink_recency_localised(self) -> None:
        n = 100
        attn = [0.001] * n
        # 50 % mass on first 4 tokens
        for i in range(4):
            attn[i] = 12.5
        # 30 % mass on last 32 tokens
        for i in range(n - 32, n):
            attn[i] = (0.30 / 32) * 100  # crude but localised
        s = summarise_attention(attn)
        assert s.sink_presence > 0.30
        assert s.recency_bias > 0.20


# ──────────────────────────────────────────────────────────────────────
# Determinism, latency, advisor_hash
# ──────────────────────────────────────────────────────────────────────


class TestPureFunction:
    def test_deterministic_same_atv_same_advice(self) -> None:
        inp = _mk_input(
            ctx_util=0.85,
            attn_summary=AttentionSummary(
                n_tokens=2048, top_k_concentration=0.80,
            ),
        )
        atv = build_atv(inp)
        a = eviction_advisor(atv, inp)
        b = eviction_advisor(atv, inp)
        assert a.policy == b.policy
        assert a.keep_recent_tokens == b.keep_recent_tokens
        assert a.keep_heavy_hitter_pct == b.keep_heavy_hitter_pct
        assert a.advisor_hash == b.advisor_hash

    def test_advisor_hash_is_stable(self) -> None:
        adv = eviction_advisor(
            build_atv(_mk_input()), _mk_input(),
        )
        # 64 hex chars from sha3-256.
        assert len(adv.advisor_hash) == 64
        assert all(c in "0123456789abcdef" for c in adv.advisor_hash)

    def test_sub_millisecond_latency(self) -> None:
        # Should be in the tens of microseconds on any reasonable box.
        atv = build_atv(_mk_input(ctx_util=0.50))
        adv = eviction_advisor(atv)
        assert adv.latency_ms < 5.0

    def test_runs_without_inp(self) -> None:
        atv = build_atv(_mk_input(ctx_util=0.80))
        adv = eviction_advisor(atv, None)
        assert isinstance(adv, EvictionAdvice)
        assert adv.evict_token_indices is None

    def test_confidence_lower_without_attention(self) -> None:
        with_attn = eviction_advisor(
            build_atv(_mk_input(
                ctx_util=0.80,
                attn_summary=AttentionSummary(
                    n_tokens=512, top_k_concentration=0.7,
                ),
            )),
            _mk_input(
                ctx_util=0.80,
                attn_summary=AttentionSummary(
                    n_tokens=512, top_k_concentration=0.7,
                ),
            ),
        )
        without_attn = eviction_advisor(
            build_atv(_mk_input(ctx_util=0.80)),
            _mk_input(ctx_util=0.80),
        )
        assert with_attn.confidence > without_attn.confidence


# ──────────────────────────────────────────────────────────────────────
# Expected memory savings
# ──────────────────────────────────────────────────────────────────────


class TestSavingsEstimate:
    def test_savings_zero_when_policy_none(self) -> None:
        inp = _mk_input(ctx_util=0.20)
        adv = eviction_advisor(build_atv(inp), inp)
        assert adv.expected_memory_savings_pct == 0.0

    def test_savings_grows_with_h2o(self) -> None:
        inp = _mk_input(
            ctx_util=0.80,
            attn_summary=AttentionSummary(
                n_tokens=2048, top_k_concentration=0.90,
            ),
        )
        adv = eviction_advisor(build_atv(inp), inp)
        assert adv.policy == "h2o"
        # H2O keeping ~12 % means savings ~88 %.
        assert adv.expected_memory_savings_pct > 0.70

    def test_savings_bounded_in_unit_interval(self) -> None:
        inp = _mk_input(
            ctx_util=0.95,
            attn_summary=AttentionSummary(
                n_tokens=4096, top_k_concentration=0.95,
            ),
        )
        adv = eviction_advisor(build_atv(inp), inp)
        assert 0.0 <= adv.expected_memory_savings_pct <= 1.0
