"""Tests for ``aegis.atv.serializer`` — ATV → sLLM prompt bridge (PR-β).

Verifies:

* strict mode produces a non-empty prompt from any valid ATV,
  refuses ATVInput, and explicitly surfaces gaps for the bands that
  cannot represent semantic content (plan_text, action_history,
  current tool name)
* enriched mode supplements strict with ATVInput.plan_text, recent_actions,
  and current tool call
* the strict→enriched delta is the diagnostic that scopes PR-α
* serialization is deterministic (same ATV → same text)
* privacy: raw attention scores never appear in the output, even
  when ATVInput carries them
"""

from __future__ import annotations

import re

import numpy as np
import pytest

from aegis.atv.builder import build_atv
from aegis.atv.serializer import (
    SerializedATV,
    atv_to_prompt,
    diagnose,
)
from aegis.schema import (
    ATV_DIM,
    ATV_VERSION,
    AttentionSummary,
    ATVHeader,
    ATVInput,
    CostEfficiencyMetrics,
)


def _mk_input(
    *,
    plan_text: str = "default plan",
    cumulative_tokens: float = 0.0,
    cumulative_dollars: float = 0.0,
    cache_hit: float = 0.0,
    ctx_util: float = 0.0,
    progress: float = 0.0,
    novelty: float = 0.0,
    attn_summary: AttentionSummary | None = None,
    attn_per_token: list[float] | None = None,
    recent_actions: list[dict] | None = None,
) -> ATVInput:
    return ATVInput(
        header=ATVHeader(
            trace_id="t" * 32, span_id="s" * 16,
            tenant_id="alice", aid="agent-007", timestamp_ns=1,
            model_hash="claude-haiku-4-5",
        ),
        tool_name="Bash",
        tool_args_json='{"command": "ls"}',
        plan_text=plan_text,
        cost_estimate=CostEfficiencyMetrics(
            cumulative_tokens=cumulative_tokens,
            cumulative_dollars=cumulative_dollars,
            cache_hit_rate=cache_hit,
            context_utilization_ratio=ctx_util,
            task_progress_score=progress,
        ),
        novelty={"composite_novelty": novelty},
        attention_summary=attn_summary,
        attention_per_token=attn_per_token,
        recent_actions=recent_actions or [],
    )


# ──────────────────────────────────────────────────────────────────────
# Shape + format invariants
# ──────────────────────────────────────────────────────────────────────


class TestFormat:
    def test_returns_serialized_atv(self) -> None:
        atv = build_atv(_mk_input())
        out = atv_to_prompt(atv)
        assert isinstance(out, SerializedATV)
        assert out.mode == "strict"

    def test_text_starts_and_ends_with_markers(self) -> None:
        atv = build_atv(_mk_input())
        out = atv_to_prompt(atv)
        assert out.text.startswith("[ATV-CONTEXT")
        assert out.text.rstrip().endswith("[/ATV-CONTEXT]")

    def test_schema_version_in_header(self) -> None:
        atv = build_atv(_mk_input())
        out = atv_to_prompt(atv)
        assert ATV_VERSION in out.text

    def test_rejects_wrong_dim(self) -> None:
        bad = np.zeros(100, dtype=np.float32)
        with pytest.raises(ValueError, match=str(ATV_DIM)):
            atv_to_prompt(bad)

    def test_enriched_requires_input(self) -> None:
        atv = build_atv(_mk_input())
        with pytest.raises(ValueError, match="enriched"):
            atv_to_prompt(atv, mode="enriched")

    def test_strict_does_not_consult_input(self) -> None:
        # Even when caller passes inp in strict mode, the output
        # MUST NOT contain ATVInput-derived text.
        inp = _mk_input(plan_text="DO NOT LEAK SECRET TOKEN abc-12345")
        atv = build_atv(inp)
        out = atv_to_prompt(atv, inp, mode="strict")
        assert "DO NOT LEAK" not in out.text
        assert "abc-12345" not in out.text


# ──────────────────────────────────────────────────────────────────────
# Strict-mode gap surfacing — the (b.pragmatic) diagnostic
# ──────────────────────────────────────────────────────────────────────


class TestStrictGaps:
    def test_plan_text_gap_listed(self) -> None:
        atv = build_atv(_mk_input(plan_text="x"))
        out = atv_to_prompt(atv, mode="strict")
        assert any("plan_text" in g for g in out.gaps)

    def test_agent_embedding_gap_listed(self) -> None:
        atv = build_atv(_mk_input(plan_text="x"))
        out = atv_to_prompt(atv, mode="strict")
        # Embedding is present but no decoder → that's the gap.
        assert any(
            "agent_state_embedding" in g and "decoder" in g
            for g in out.gaps
        )

    def test_action_history_gap_listed_when_nonzero(self) -> None:
        # action_history is hash-expanded and non-zero unless
        # recent_actions is empty AND the encoder produces zero.
        # Force non-zero by giving recent_actions.
        inp = _mk_input(
            recent_actions=[{"tool": "Read", "result": "ok"}],
        )
        atv = build_atv(inp)
        out = atv_to_prompt(atv, mode="strict")
        assert any(
            "action_history" in g and "hash" in g.lower()
            for g in out.gaps
        )

    def test_current_tool_call_gap_listed(self) -> None:
        atv = build_atv(_mk_input())
        out = atv_to_prompt(atv, mode="strict")
        assert any(
            "tool_name" in g or "current tool" in g.lower()
            for g in out.gaps
        )

    def test_hw_band_gap_in_t2(self) -> None:
        # Default tier=T2 → HW band zero → 10% empty surface listed.
        atv = build_atv(_mk_input())
        out = atv_to_prompt(atv, mode="strict")
        assert any("hw_band" in g for g in out.gaps)


# ──────────────────────────────────────────────────────────────────────
# Enriched mode supplementation
# ──────────────────────────────────────────────────────────────────────


class TestEnrichedSupplements:
    def test_enriched_includes_plan_text(self) -> None:
        plan = (
            "Audit the auth module for the validation bug we discussed."
        )
        inp = _mk_input(plan_text=plan)
        atv = build_atv(inp)
        out = atv_to_prompt(atv, inp, mode="enriched")
        # First 400 chars surfaced — plan is shorter, so full text.
        assert plan in out.text

    def test_enriched_includes_tool_call(self) -> None:
        inp = _mk_input()
        atv = build_atv(inp)
        out = atv_to_prompt(atv, inp, mode="enriched")
        assert "Bash" in out.text
        assert "ls" in out.text

    def test_enriched_includes_recent_actions(self) -> None:
        inp = _mk_input(recent_actions=[
            {"tool": "Read", "result": "ok"},
            {"tool": "Edit", "result": "ok"},
        ])
        atv = build_atv(inp)
        out = atv_to_prompt(atv, inp, mode="enriched")
        # At least one of the recent actions surfaced.
        assert "Read" in out.text or "Edit" in out.text

    def test_enriched_caps_long_plan(self) -> None:
        # Use 'Z' as sentinel — uppercase Z appears in no section
        # header or boilerplate, so any Z in the output came from
        # plan_text supplementation.
        long_plan = "Z" * 2000
        inp = _mk_input(plan_text=long_plan)
        atv = build_atv(inp)
        out = atv_to_prompt(atv, inp, mode="enriched")
        # 400-char excerpt + ellipsis, not the full 2000.
        assert out.text.count("Z") <= 401


# ──────────────────────────────────────────────────────────────────────
# Determinism
# ──────────────────────────────────────────────────────────────────────


class TestDeterminism:
    def test_same_atv_same_text(self) -> None:
        atv = build_atv(_mk_input(plan_text="hello"))
        a = atv_to_prompt(atv, mode="strict")
        b = atv_to_prompt(atv, mode="strict")
        assert a.text == b.text

    def test_strict_text_independent_of_input(self) -> None:
        # Two different ATVInputs that produce IDENTICAL ATV vectors
        # should yield identical strict-mode text.
        inp1 = _mk_input(plan_text="x")
        inp2 = _mk_input(plan_text="x")
        atv1 = build_atv(inp1)
        atv2 = build_atv(inp2)
        if np.array_equal(atv1, atv2):
            assert atv_to_prompt(atv1, mode="strict").text == \
                atv_to_prompt(atv2, mode="strict").text


# ──────────────────────────────────────────────────────────────────────
# Privacy
# ──────────────────────────────────────────────────────────────────────


class TestPrivacy:
    def test_attention_per_token_never_appears(self) -> None:
        # Per-token attention list MUST never end up in the text —
        # those scores localise secrets to positions.
        sentinel = 0.987654   # unmistakable value
        attn = [0.001] * 50 + [sentinel] * 10
        inp = _mk_input(
            plan_text="hello",
            attn_per_token=attn,
            attn_summary=AttentionSummary(
                n_tokens=60, top_k_concentration=0.7,
            ),
        )
        atv = build_atv(inp)
        # Both modes — neither should leak the raw float.
        for mode in ("strict", "enriched"):
            out = atv_to_prompt(atv, inp, mode=mode)
            assert "0.987654" not in out.text
            assert "0.987" not in out.text

    def test_attention_summary_aggregates_only(self) -> None:
        # AttentionSummary fold-in surfaces aggregates (entropy,
        # top_k, sink, recency), never per-position data.
        inp = _mk_input(
            plan_text="hello",
            attn_summary=AttentionSummary(
                n_tokens=128,
                entropy_normalized=0.42,
                top_k_concentration=0.78,
                sink_presence=0.15,
                recency_bias=0.55,
                effective_rank=0.18,
            ),
        )
        atv = build_atv(inp)
        out = atv_to_prompt(atv, mode="strict")
        # Summary slots fold into prompt_structure[9..13]; their
        # values should appear (they're aggregate, safe to surface).
        assert "attn_top_k_concentration" in out.text
        assert "attn_sink_presence" in out.text


# ──────────────────────────────────────────────────────────────────────
# Diagnose helper
# ──────────────────────────────────────────────────────────────────────


class TestDiagnose:
    def test_returns_both_modes(self) -> None:
        inp = _mk_input(plan_text="audit my code please")
        atv = build_atv(inp)
        d = diagnose(atv, inp)
        assert "strict_text" in d
        assert "enriched_text" in d
        assert d["strict_lines"] > 0
        assert d["enriched_lines"] >= d["strict_lines"]

    def test_delta_positive_when_input_present(self) -> None:
        # Use a substantial plan + recent_actions so the enriched
        # supplement clearly exceeds the strict "ABSENT" placeholders
        # they replace.
        inp = _mk_input(
            plan_text=(
                "Audit src/auth/login.py for the validation bug. "
                "First Read the file, then Grep imports, then Edit "
                "the offending block, then run pytest tests/auth/."
            ),
            recent_actions=[
                {"tool": "Read", "result": "ok"},
                {"tool": "Grep", "result": "ok"},
                {"tool": "Edit", "result": "ok"},
            ],
        )
        atv = build_atv(inp)
        d = diagnose(atv, inp)
        # Enriched > strict by at least the plan_text + recent_actions
        # additions.
        assert int(d["delta_bytes"]) > 0

    def test_strict_gaps_at_least_three(self) -> None:
        # strict mode should surface ≥ 3 distinct gaps:
        # plan_text, agent_state_embedding decoder, current tool.
        inp = _mk_input(plan_text="x")
        atv = build_atv(inp)
        d = diagnose(atv, inp)
        assert len(d["strict_gaps"]) >= 3


# ──────────────────────────────────────────────────────────────────────
# bands_present diagnostic dict
# ──────────────────────────────────────────────────────────────────────


class TestBandsPresent:
    def test_all_critical_bands_listed(self) -> None:
        inp = _mk_input(
            cumulative_tokens=1000.0, cache_hit=0.5,
        )
        atv = build_atv(inp)
        out = atv_to_prompt(atv, mode="strict")
        # All major bands have an entry.
        for key in (
            "agent_state_embedding",
            "novelty_score",
            "prompt_structure",
            "cost_efficiency_metrics",
            "action_history",
            "action_blast_radius",
            "grounding_metrics",
        ):
            assert key in out.bands_present

    def test_zero_bands_marked_zero(self) -> None:
        inp = _mk_input()  # no cost signal
        atv = build_atv(inp)
        out = atv_to_prompt(atv, mode="strict")
        assert (
            "zero" in out.bands_present.get("cost_efficiency_metrics", "")
            or "0" in out.bands_present.get("cost_efficiency_metrics", "")
        )


# ──────────────────────────────────────────────────────────────────────
# Token budget reasonableness
# ──────────────────────────────────────────────────────────────────────


class TestSize:
    def test_strict_under_token_budget(self) -> None:
        # Rough heuristic: 1 token ≈ 4 chars. Strict should be
        # under ~3000 tokens (≈ 12k chars) in worst case.
        inp = _mk_input(
            plan_text="x" * 5000, cumulative_tokens=5e6,
        )
        atv = build_atv(inp)
        out = atv_to_prompt(atv, mode="strict")
        assert len(out.text) < 12_000

    def test_enriched_includes_excerpt_not_full_plan(self) -> None:
        # Z sentinel — appears in no section header or boilerplate.
        inp = _mk_input(plan_text="Z" * 1000)
        atv = build_atv(inp)
        out = atv_to_prompt(atv, inp, mode="enriched")
        # At most 401 'Z's surfaced (400 + ellipsis).
        assert out.text.count("Z") <= 401
        # Not the full thousand.
        assert "Z" * 500 not in out.text


# ──────────────────────────────────────────────────────────────────────
# Header semantics — schema version present
# ──────────────────────────────────────────────────────────────────────


class TestHeader:
    def test_mode_in_header(self) -> None:
        atv = build_atv(_mk_input())
        s = atv_to_prompt(atv, mode="strict")
        assert "mode=strict" in s.text
        e = atv_to_prompt(atv, _mk_input(), mode="enriched")
        assert "mode=enriched" in e.text

    def test_bracket_delimiters_paired(self) -> None:
        atv = build_atv(_mk_input())
        out = atv_to_prompt(atv, mode="strict")
        assert (
            re.match(r"^\[ATV-CONTEXT[^\]]+\]", out.text) is not None
        )
        assert "[/ATV-CONTEXT]" in out.text
