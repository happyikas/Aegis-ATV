"""Tests for ``aegis.judge.advisor_signals`` — cost / cache / security
signal extraction + section rendering (PR-ψ-multi-domain).

The extractors are duck-typed (read attributes / dict keys defensively),
so these tests use lightweight fakes instead of building full ATVInput
/ Verdict instances.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from aegis.atv.temporal import ATVSnapshot, TemporalContext
from aegis.judge.advisor_signals import (
    extract_cache_signals,
    extract_cost_signals,
    extract_security_signals,
    render_cache_signals,
    render_cost_signals,
    render_security_signals,
)

# ──────────────────────────────────────────────────────────────────────
# Fakes
# ──────────────────────────────────────────────────────────────────────


@dataclass
class _FakeCostEstimate:
    cumulative_dollars: float = 0.0


@dataclass
class _FakeHeader:
    aid: str = "sess-test"


@dataclass
class _FakeInp:
    cost_estimate: _FakeCostEstimate
    tool_args_json: str = ""
    tool_name: str = "Bash"
    header: _FakeHeader = field(default_factory=_FakeHeader)


@dataclass
class _FakeVerdict:
    decision: str = "ALLOW"
    reason: str = ""
    step_traces: dict[str, str] = field(default_factory=dict)


def _mk_temporal(
    *,
    rates: list[float] | None = None,
    cache_creation_per_turn: list[int] | None = None,
    cache_read_per_turn: list[int] | None = None,
    max_drop_pp: float = 0.0,
) -> TemporalContext:
    """Synthesise a TemporalContext with controllable cache trajectories."""
    rates = rates or [0.5, 0.5, 0.5]
    creation = cache_creation_per_turn or [0] * len(rates)
    read = cache_read_per_turn or [0] * len(rates)
    snaps = [
        ATVSnapshot(
            turn_index_rel=i - (len(rates) - 1),
            ts_ns=0,
            tool_name="Read",
            args_excerpt="",
            decision="ALLOW",
            outcome="success",
            cache_creation_tokens=creation[i],
            cache_read_tokens=read[i],
            cache_hit_rate=rates[i],
        )
        for i in range(len(rates))
    ]
    return TemporalContext(
        history=tuple(snaps),
        window_size=len(rates),
        cumulative_token_trajectory=tuple(0 for _ in rates),
        cache_hit_rate_trajectory=tuple(rates),
        n_backtracks=0, n_redundant=0, n_errors=0, n_failures=0,
        cache_hit_rate_max_drop_pp=float(max_drop_pp),
        token_velocity_per_turn=0.0,
        is_progress_stalled=False,
        distinct_tools_in_window=("Read",),
    )


# ──────────────────────────────────────────────────────────────────────
# Cost
# ──────────────────────────────────────────────────────────────────────


class TestCostExtract:
    def test_empty_inp_empty_verdict_yields_empty(self) -> None:
        d = extract_cost_signals(
            inp=_FakeInp(cost_estimate=_FakeCostEstimate()),
            verdict=_FakeVerdict(),
        )
        assert d == {}

    def test_step335_trace_extracts_cum_proj_limit(self) -> None:
        v = _FakeVerdict(
            step_traces={
                "aegis.firewall.step335_cost.run":
                    "ok cum=0.4200 proj=1.3000 limit=2.00",
            }
        )
        d = extract_cost_signals(
            inp=_FakeInp(cost_estimate=_FakeCostEstimate()), verdict=v,
        )
        assert d["cumulative_dollars"] == 0.42
        assert d["projected_session_cost"] == 1.30
        assert d["budget_limit"] == 2.00
        assert abs(d["budget_used_ratio"] - 0.65) < 1e-6

    def test_step335_warn_flag(self) -> None:
        v = _FakeVerdict(
            step_traces={
                "aegis.firewall.step335_cost.run":
                    "warn cum=1.8 proj=2.1 limit=2.0",
            }
        )
        d = extract_cost_signals(
            inp=_FakeInp(cost_estimate=_FakeCostEstimate()), verdict=v,
        )
        assert d["budget_warn_flag"] is True

    def test_m12_escalation_trace_extracts_ratio(self) -> None:
        v = _FakeVerdict(
            step_traces={
                "aegis.cost.escalation":
                    "M12: hw_vs_sw_flops_ratio=3.150 > threshold 2.000",
            }
        )
        d = extract_cost_signals(
            inp=_FakeInp(cost_estimate=_FakeCostEstimate()), verdict=v,
        )
        assert d["hw_vs_sw_divergence_ratio"] == 3.150
        assert "m12_escalation_trace" in d

    def test_falls_back_to_inp_cost_estimate(self) -> None:
        d = extract_cost_signals(
            inp=_FakeInp(cost_estimate=_FakeCostEstimate(cumulative_dollars=1.5)),
            verdict=_FakeVerdict(),
        )
        assert d["cumulative_dollars"] == 1.5


class TestCostRender:
    def test_empty_returns_empty(self) -> None:
        assert render_cost_signals({}) == ""

    def test_renders_header_and_fields(self) -> None:
        out = render_cost_signals({
            "cumulative_dollars": 0.42,
            "projected_session_cost": 1.30,
            "budget_used_ratio": 0.65,
            "hw_vs_sw_divergence_ratio": 3.15,
        })
        assert out.startswith("COST METRICS")
        assert "$0.4200" in out
        assert "65.0% of budget" in out
        assert "3.15×" in out
        assert "ESCALATED" in out


# ──────────────────────────────────────────────────────────────────────
# KV Cache
# ──────────────────────────────────────────────────────────────────────


class TestCacheExtract:
    def test_none_temporal_yields_empty(self) -> None:
        assert extract_cache_signals(temporal_ctx=None) == {}

    def test_basic_trajectory_metrics(self) -> None:
        ctx = _mk_temporal(rates=[0.8, 0.5, 0.3], max_drop_pp=30.0)
        d = extract_cache_signals(temporal_ctx=ctx)
        assert d["cache_hit_rate_recent"] == 0.3
        assert abs(d["cache_hit_rate_window_mean"] - 0.5333) < 1e-3
        assert d["cache_hit_rate_max_drop_pp"] == 30.0
        assert d["prefix_stability"] == "stable"

    def test_unstable_prefix_when_creation_dominates(self) -> None:
        # 4 turns where creation > read on each → 4 re-keys → unstable
        ctx = _mk_temporal(
            rates=[0.5] * 4,
            cache_creation_per_turn=[200, 200, 200, 200],
            cache_read_per_turn=[10, 10, 10, 10],
        )
        d = extract_cache_signals(temporal_ctx=ctx)
        assert d["prefix_stability"] == "unstable"
        assert d["prefix_re_keys_in_window"] == 4
        assert d["cache_creation_tokens_window"] == 800


class TestCacheRender:
    def test_renders_drop_marker_above_30pp(self) -> None:
        out = render_cache_signals({
            "cache_hit_rate_recent": 0.3,
            "cache_hit_rate_window_mean": 0.5,
            "cache_hit_rate_max_drop_pp": 51.0,
            "prefix_stability": "unstable",
            "prefix_re_keys_in_window": 4,
        })
        assert out.startswith("KV CACHE METRICS")
        assert "significant drop" in out
        assert "unstable" in out


# ──────────────────────────────────────────────────────────────────────
# Security
# ──────────────────────────────────────────────────────────────────────


class TestSecurityExtract:
    def test_dangerous_pattern_in_reason_flags_match(self) -> None:
        """v2.7.3 — step310 emits ``dangerous pattern: <regex>`` for
        the destructive shell-pattern matcher (rm -rf, sql destructive,
        etc.). These are destructive even though they don't carry a
        ``rule:`` prefix."""
        v = _FakeVerdict(
            decision="BLOCK",
            reason=r"dangerous pattern: \brm\s+-rf\s+/",
        )
        d = extract_security_signals(
            inp=_FakeInp(cost_estimate=_FakeCostEstimate()), verdict=v,
        )
        assert d["destructive_path_match"] is True
        assert d["policy_rule"].startswith("dangerous_pattern:")

    def test_destructive_rule_in_reason_flags_match(self) -> None:
        v = _FakeVerdict(
            decision="BLOCK",
            reason="rule:git_destructive — push --force detected",
            step_traces={"aegis.firewall.step320_blast.run": "blast=high"},
        )
        d = extract_security_signals(
            inp=_FakeInp(cost_estimate=_FakeCostEstimate()), verdict=v,
        )
        assert d["destructive_path_match"] is True
        assert d["policy_rule"] == "rule:git_destructive"
        assert d["blast_radius"] == "high"
        assert d["verdict_decision"] == "BLOCK"

    def test_sensitive_path_flag(self) -> None:
        v = _FakeVerdict(decision="ALLOW")
        inp = _FakeInp(
            cost_estimate=_FakeCostEstimate(),
            tool_args_json='{"file_path": "/backup/db_dump.sql"}',
        )
        d = extract_security_signals(inp=inp, verdict=v)
        assert d.get("sensitive_path_in_args") is True

    def test_m13_top_security_subfields_pulled_through(self) -> None:
        v = _FakeVerdict(decision="ALLOW")
        explain = {
            "m13_top": [
                {"subfield": "tool_arg_inspection", "score": 0.9},
                {"subfield": "agent_state_embedding", "score": 0.4},
                {"subfield": "action_blast_radius", "score": 0.7},
            ]
        }
        d = extract_security_signals(
            inp=_FakeInp(cost_estimate=_FakeCostEstimate()),
            verdict=v,
            explain_block=explain,
        )
        names = {e["subfield"] for e in d["m13_security_top"]}
        assert "tool_arg_inspection" in names
        assert "action_blast_radius" in names
        assert "agent_state_embedding" not in names  # not security


class TestSecurityRender:
    def test_renders_destructive_match(self) -> None:
        out = render_security_signals({
            "verdict_decision": "BLOCK",
            "destructive_path_match": True,
            "policy_rule": "rule:git_destructive",
            "blast_radius": "high",
        })
        assert out.startswith("SECURITY SIGNALS")
        assert "verdict_decision:" in out
        assert "rule:git_destructive" in out
        assert "blast_radius:" in out
