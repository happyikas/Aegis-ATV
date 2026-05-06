"""30 Cost test cases.

Exercises ``cost-optimizer`` and ``kv-cache-optimizer`` advisors and
their cross-domain combinations. Coverage targets:

* All four cost-optimizer verbs: ``swap-model``, ``prune-turns``,
  ``end-session``, ``notify-operator``.
* Boundary tests on ``budget_used_ratio`` and
  ``hw_vs_sw_divergence_ratio`` (M12) thresholds.
* All cheaper-model swap pairs (Opus → Haiku, Sonnet → Haiku).
* Cache-driven cost: ``cache_hit_rate_max_drop_pp`` thresholds.
* Multi-domain combos (cost + cache, cost + velocity).
* Negative controls (clean session, idle session).
"""
from __future__ import annotations

from .case import TestCase
from .fixtures import (
    ctx_5turns_expensive,
    ctx_idle,
    ctx_long_window,
    ctx_with_backtracks,
)


def cases() -> list[TestCase]:
    out: list[TestCase] = []
    add = out.append

    add(TestCase(
        cid="COST-01",
        category="cost",
        title="Idle session — no advisor fires",
        scenario=(
            "3 cheap routine reads with 92% cache. Cost-optimizer "
            "must NOT fire on a healthy idle session."
        ),
        execution_summary=(
            "compose_advice_heuristic(temporal_ctx=ctx_idle())"
        ),
        test_type="unit",
        expected_no_fire=True,
        ctx_factory=ctx_idle,
    ))

    add(TestCase(
        cid="COST-02",
        category="cost",
        title="Budget warn flag → cost-optimizer fires",
        scenario=(
            "M16 cost ledger raises budget_warn_flag. Cost-optimizer "
            "should fire even without other anomalies."
        ),
        execution_summary=(
            "cost_signals={'budget_warn_flag': True}"
        ),
        test_type="unit",
        expected_advisor="cost-optimizer",
        cost_signals={"budget_warn_flag": True},
    ))

    add(TestCase(
        cid="COST-03",
        category="cost",
        title="Budget at 0.85x — below threshold, no fire",
        scenario=(
            "Budget consumed 85%, below the 0.9 trigger. "
            "Cost-optimizer must not yet recommend an action."
        ),
        execution_summary=(
            "cost_signals={'budget_used_ratio': 0.85}"
        ),
        test_type="unit",
        expected_no_fire_for="cost-optimizer",
        cost_signals={"budget_used_ratio": 0.85},
    ))

    add(TestCase(
        cid="COST-04",
        category="cost",
        title="Budget at 0.9x — exactly at threshold",
        scenario=(
            "Budget at 90% triggers cost-optimizer with prune-turns "
            "as the lowest-friction first action."
        ),
        execution_summary=(
            "cost_signals={'budget_used_ratio': 0.9}"
        ),
        test_type="unit",
        expected_advisor="cost-optimizer",
        expected_verb="prune-turns",
        ctx_factory=ctx_5turns_expensive,
        cost_signals={"budget_used_ratio": 0.9},
    ))

    add(TestCase(
        cid="COST-05",
        category="cost",
        title="Budget at 1.5x — Opus session swaps to Haiku",
        scenario=(
            "50% over budget on Opus 4.7. swap-model must target a "
            "cheaper model with a measurable cost ratio."
        ),
        execution_summary=(
            "current_model='claude-opus-4-7', "
            "budget_used_ratio=1.5"
        ),
        test_type="unit",
        expected_advisor="cost-optimizer",
        expected_verb="swap-model",
        ctx_factory=ctx_5turns_expensive,
        current_model="claude-opus-4-7",
        cost_signals={"budget_used_ratio": 1.5},
    ))

    add(TestCase(
        cid="COST-06",
        category="cost",
        title="Budget at 1.5x — prune-turns also present",
        scenario=(
            "At budget 1.5×, cost-optimizer enumerates multiple "
            "actions; prune-turns is one of them."
        ),
        execution_summary=(
            "current_model='claude-opus-4-7', "
            "budget_used_ratio=1.5"
        ),
        test_type="unit",
        expected_advisor="cost-optimizer",
        expected_verb="prune-turns",
        ctx_factory=ctx_5turns_expensive,
        current_model="claude-opus-4-7",
        cost_signals={"budget_used_ratio": 1.5},
    ))

    add(TestCase(
        cid="COST-07",
        category="cost",
        title="Budget at 2.0x — end-session escalation",
        scenario=(
            "Budget doubled. cost-optimizer must include end-session "
            "as a hard-stop option."
        ),
        execution_summary=(
            "current_model='claude-opus-4-7', budget_used_ratio=2.0"
        ),
        test_type="unit",
        expected_advisor="cost-optimizer",
        expected_verb="end-session",
        ctx_factory=ctx_5turns_expensive,
        current_model="claude-opus-4-7",
        cost_signals={"budget_used_ratio": 2.0},
    ))

    add(TestCase(
        cid="COST-08",
        category="cost",
        title="M12 ratio 1.99 — boundary, no fire",
        scenario=(
            "HW/SW divergence just below the 2× threshold. "
            "cost-optimizer must remain quiet."
        ),
        execution_summary=(
            "cost_signals={'hw_vs_sw_divergence_ratio': 1.99}"
        ),
        test_type="unit",
        expected_no_fire=True,
        cost_signals={"hw_vs_sw_divergence_ratio": 1.99},
    ))

    add(TestCase(
        cid="COST-09",
        category="cost",
        title="M12 ratio 2.0 — notify-operator at threshold",
        scenario=(
            "HW reports double the SW-estimated cost; the operator "
            "needs to be notified before the gap widens."
        ),
        execution_summary=(
            "cost_signals={'hw_vs_sw_divergence_ratio': 2.0}"
        ),
        test_type="unit",
        expected_advisor="cost-optimizer",
        expected_verb="notify-operator",
        cost_signals={"hw_vs_sw_divergence_ratio": 2.0},
    ))

    add(TestCase(
        cid="COST-10",
        category="cost",
        title="M12 ratio 3.15 — notify-operator high severity",
        scenario=(
            "HW/SW gap at 3.15×; severe drift between metering and "
            "ground truth."
        ),
        execution_summary=(
            "cost_signals={'hw_vs_sw_divergence_ratio': 3.15}"
        ),
        test_type="unit",
        expected_advisor="cost-optimizer",
        expected_verb="notify-operator",
        cost_signals={"hw_vs_sw_divergence_ratio": 3.15},
    ))

    add(TestCase(
        cid="COST-11",
        category="cost",
        title="M12 ratio 5.0 — notify-operator critical",
        scenario=(
            "HW/SW divergence at 5×; metering essentially "
            "uncalibrated."
        ),
        execution_summary=(
            "cost_signals={'hw_vs_sw_divergence_ratio': 5.0}"
        ),
        test_type="unit",
        expected_advisor="cost-optimizer",
        expected_verb="notify-operator",
        cost_signals={"hw_vs_sw_divergence_ratio": 5.0},
    ))

    add(TestCase(
        cid="COST-12",
        category="cost",
        title="Sonnet → Haiku swap path",
        scenario=(
            "Sonnet 4.6 over budget; swap-model should still emit a "
            "valid cheaper-model target rather than no-op."
        ),
        execution_summary=(
            "current_model='claude-sonnet-4-6', budget_used_ratio=1.5"
        ),
        test_type="unit",
        expected_advisor="cost-optimizer",
        expected_verb="swap-model",
        ctx_factory=ctx_5turns_expensive,
        current_model="claude-sonnet-4-6",
        cost_signals={"budget_used_ratio": 1.5},
    ))

    add(TestCase(
        cid="COST-13",
        category="cost",
        title="Cache drop 51pp — kv-cache-optimizer fires",
        scenario=(
            "Cache hit-rate fell 51pp in the recent window; "
            "kv-cache-optimizer should suggest prune-turns to "
            "stabilise the prefix."
        ),
        execution_summary=(
            "cache_hit_rate_max_drop_pp=51.0, "
            "prefix_re_keys_in_window=4"
        ),
        test_type="unit",
        expected_advisor="kv-cache-optimizer",
        expected_verb="prune-turns",
        ctx_factory=ctx_5turns_expensive,
        cache_signals={
            "cache_hit_rate_max_drop_pp": 51.0,
            "prefix_re_keys_in_window": 4,
        },
    ))

    add(TestCase(
        cid="COST-14",
        category="cost",
        title="Cache drop 25pp — boundary, kv-cache-optimizer silent",
        scenario=(
            "Cache drop at 25pp; below the 30pp action threshold. "
            "kv-cache-optimizer should not yet fire."
        ),
        execution_summary=(
            "cache_hit_rate_max_drop_pp=25.0"
        ),
        test_type="unit",
        expected_no_fire_for="kv-cache-optimizer",
        cache_signals={"cache_hit_rate_max_drop_pp": 25.0},
    ))

    add(TestCase(
        cid="COST-15",
        category="cost",
        title="Prefix unstable → summarize-window",
        scenario=(
            "Prompt prefix flagged unstable with 4 prefix re-keys; "
            "kv-cache-optimizer should recommend summarize-window."
        ),
        execution_summary=(
            "cache_signals={'prefix_stability': 'unstable', "
            "'prefix_re_keys_in_window': 4}"
        ),
        test_type="unit",
        expected_advisor="kv-cache-optimizer",
        expected_verb="summarize-window",
        ctx_factory=ctx_5turns_expensive,
        cache_signals={
            "prefix_stability": "unstable",
            "prefix_re_keys_in_window": 4,
        },
    ))

    add(TestCase(
        cid="COST-16",
        category="cost",
        title="Cost + cache combo (no security) — 2 advisors",
        scenario=(
            "Budget + cache drop. Both cost-optimizer and "
            "kv-cache-optimizer should fire in the same advice."
        ),
        execution_summary=(
            "budget_used_ratio=1.0, cache_hit_rate_max_drop_pp=50.0"
        ),
        test_type="unit",
        expected_multi=("cost-optimizer", "kv-cache-optimizer"),
        ctx_factory=ctx_5turns_expensive,
        cost_signals={"budget_used_ratio": 1.0},
        cache_signals={"cache_hit_rate_max_drop_pp": 50.0},
    ))

    add(TestCase(
        cid="COST-17",
        category="cost",
        title="Long window (50 turns) → prune-turns viable",
        scenario=(
            "50-turn window over budget. prune-turns should be "
            "feasible with a non-trivial k value."
        ),
        execution_summary=(
            "ctx=ctx_long_window(50), budget_used_ratio=1.2"
        ),
        test_type="unit",
        expected_advisor="cost-optimizer",
        expected_verb="prune-turns",
        ctx_factory=ctx_long_window,
        cost_signals={"budget_used_ratio": 1.2},
    ))

    add(TestCase(
        cid="COST-18",
        category="cost",
        title="Velocity anomaly → context-compactor (cost-adjacent)",
        scenario=(
            "Window-token-velocity flagged anomalous. "
            "context-compactor should fire to recommend "
            "summarize-window for token reduction."
        ),
        execution_summary=(
            "anomaly_metric='window_token_velocity_per_turn'"
        ),
        test_type="unit",
        expected_advisor="context-compactor",
        expected_verb="summarize-window",
        ctx_factory=ctx_5turns_expensive,
        anomaly_metric="window_token_velocity_per_turn",
    ))

    add(TestCase(
        cid="COST-19",
        category="cost",
        title="Cost + M12 + velocity — 3 actionable signals",
        scenario=(
            "Budget warn + HW/SW drift + velocity anomaly. "
            "cost-optimizer plus context-compactor expected."
        ),
        execution_summary=(
            "cost+M12+velocity stacked"
        ),
        test_type="unit",
        expected_multi=("cost-optimizer", "context-compactor"),
        ctx_factory=ctx_5turns_expensive,
        cost_signals={
            "hw_vs_sw_divergence_ratio": 3.0,
            "budget_used_ratio": 1.0,
        },
        anomaly_metric="window_token_velocity_per_turn",
    ))

    add(TestCase(
        cid="COST-20",
        category="cost",
        title="Cost + cache + security — 3 advisors fire",
        scenario=(
            "Canonical 3-domain combo: budget exceeded, cache "
            "broken, destructive op queued. All three advisors "
            "should be in the recommendation list."
        ),
        execution_summary=(
            "cost+cache+security cross-domain"
        ),
        test_type="unit",
        expected_multi=(
            "cost-optimizer", "kv-cache-optimizer", "security-reviewer",
        ),
        expected_verbs_any=("require-approval",),
        ctx_factory=ctx_5turns_expensive,
        current_model="claude-opus-4-7",
        cost_signals={"budget_used_ratio": 1.5},
        cache_signals={
            "cache_hit_rate_max_drop_pp": 55.0,
            "prefix_re_keys_in_window": 4,
        },
        security_signals={
            "verdict_decision": "REQUIRE_APPROVAL",
            "destructive_path_match": True,
            "policy_rule": "rule:fs_destructive",
            "blast_radius": "high",
        },
    ))

    add(TestCase(
        cid="COST-21",
        category="cost",
        title="Cache + backtrack — 2 advisors",
        scenario=(
            "Cache drop alongside a backtracking operator. "
            "kv-cache-optimizer + human-clarifier expected."
        ),
        execution_summary=(
            "cache_hit_rate_max_drop_pp=51.0 with n_backtracks=1"
        ),
        test_type="unit",
        expected_multi=("kv-cache-optimizer", "human-clarifier"),
        ctx_factory=lambda: ctx_with_backtracks(1),
        cache_signals={"cache_hit_rate_max_drop_pp": 51.0},
    ))

    add(TestCase(
        cid="COST-22",
        category="cost",
        title="Routine ALLOW (e2e) — no advisor fires",
        scenario=(
            "Read /tmp/foo.md through the local PreToolUse hook. "
            "Audit should record decision=ALLOW and no advisor."
        ),
        execution_summary=(
            "PreToolUse: Read /tmp/foo.md"
        ),
        test_type="e2e",
        expected_no_fire=True,
        expected_decision="ALLOW",
        pre_event={
            "hook_event_name": "PreToolUse",
            "session_id": "macmini-cost-22",
            "invocation_id": "macmini-cost-22-1",
            "tool_name": "Read",
            "tool_input": {"file_path": "/tmp/foo.md"},
        },
    ))

    add(TestCase(
        cid="COST-23",
        category="cost",
        title="Routine ALLOW (e2e) — Bash echo",
        scenario=(
            "Bash echo through the hook. Cost-optimizer must remain "
            "quiet on a 1-token shell command."
        ),
        execution_summary=(
            "PreToolUse: Bash 'echo hi'"
        ),
        test_type="e2e",
        expected_no_fire=True,
        expected_decision="ALLOW",
        pre_event={
            "hook_event_name": "PreToolUse",
            "session_id": "macmini-cost-23",
            "invocation_id": "macmini-cost-23-1",
            "tool_name": "Bash",
            "tool_input": {"command": "echo hi"},
        },
    ))

    add(TestCase(
        cid="COST-24",
        category="cost",
        title="Routine ALLOW (e2e) — Grep TODO",
        scenario=(
            "Grep through the hook. Read-only search should not "
            "trigger any cost advisor."
        ),
        execution_summary=(
            "PreToolUse: Grep TODO"
        ),
        test_type="e2e",
        expected_no_fire=True,
        expected_decision="ALLOW",
        pre_event={
            "hook_event_name": "PreToolUse",
            "session_id": "macmini-cost-24",
            "invocation_id": "macmini-cost-24-1",
            "tool_name": "Grep",
            "tool_input": {"pattern": "TODO", "path": "src/"},
        },
    ))

    add(TestCase(
        cid="COST-25",
        category="cost",
        title="Routine ALLOW (e2e) — small Edit",
        scenario=(
            "Tiny Edit through the hook. No cost advisor on a "
            "one-character replacement."
        ),
        execution_summary=(
            "PreToolUse: Edit /tmp/x.md a→b"
        ),
        test_type="e2e",
        expected_no_fire=True,
        expected_decision="ALLOW",
        pre_event={
            "hook_event_name": "PreToolUse",
            "session_id": "macmini-cost-25",
            "invocation_id": "macmini-cost-25-1",
            "tool_name": "Edit",
            "tool_input": {
                "file_path": "/tmp/x.md",
                "old_string": "a", "new_string": "b",
            },
        },
    ))

    add(TestCase(
        cid="COST-26",
        category="cost",
        title="Cache drop 75pp — high-severity prune-turns",
        scenario=(
            "Catastrophic cache failure (75pp drop). "
            "kv-cache-optimizer should fire with high confidence."
        ),
        execution_summary=(
            "cache_hit_rate_max_drop_pp=75.0"
        ),
        test_type="unit",
        expected_advisor="kv-cache-optimizer",
        expected_verb="prune-turns",
        ctx_factory=ctx_5turns_expensive,
        cache_signals={
            "cache_hit_rate_max_drop_pp": 75.0,
            "prefix_re_keys_in_window": 4,
        },
    ))

    add(TestCase(
        cid="COST-27",
        category="cost",
        title="Empty signals — no fire (control)",
        scenario=(
            "All signal dicts empty / None. No advisor should "
            "produce recommendations from a zero state."
        ),
        execution_summary=(
            "compose_advice_heuristic() with no inputs"
        ),
        test_type="unit",
        expected_no_fire=True,
    ))

    add(TestCase(
        cid="COST-28",
        category="cost",
        title="High-blast security alone — no cost-advisor fire",
        scenario=(
            "High blast radius security signal without any cost "
            "or cache pressure. Cost-optimizer must stay silent."
        ),
        execution_summary=(
            "security_signals only; no cost/cache"
        ),
        test_type="unit",
        expected_no_fire_for="cost-optimizer",
        security_signals={
            "verdict_decision": "REQUIRE_APPROVAL",
            "blast_radius": "high",
        },
    ))

    add(TestCase(
        cid="COST-29",
        category="cost",
        title="Budget warn + high blast — cost + security",
        scenario=(
            "Budget warn flag together with a high-blast security "
            "signal. Both advisors should fire concurrently."
        ),
        execution_summary=(
            "budget_warn_flag=True + blast_radius=high"
        ),
        test_type="unit",
        expected_multi=("cost-optimizer", "security-reviewer"),
        cost_signals={"budget_used_ratio": 1.0},
        security_signals={
            "verdict_decision": "REQUIRE_APPROVAL",
            "blast_radius": "high",
        },
    ))

    add(TestCase(
        cid="COST-30",
        category="cost",
        title="Mega 4-advisor combo with cost domain",
        scenario=(
            "Cost + cache + security + loop all firing at once. "
            "cost-optimizer must remain in the advisor set even "
            "when 3 other advisors compete for priority."
        ),
        execution_summary=(
            "cost+cache+security+loop full stack"
        ),
        test_type="unit",
        expected_multi=(
            "cost-optimizer", "kv-cache-optimizer",
            "security-reviewer", "loop-breaker",
        ),
        ctx_factory=ctx_5turns_expensive,
        current_tool="Bash",
        current_model="claude-opus-4-7",
        cost_signals={"budget_used_ratio": 1.5},
        cache_signals={"cache_hit_rate_max_drop_pp": 50.0},
        security_signals={
            "verdict_decision": "REQUIRE_APPROVAL",
            "destructive_path_match": True,
            "policy_rule": "rule:fs_destructive",
            "blast_radius": "high",
        },
        step_traces={
            "aegis.firewall.step336_loop.run":
                "step336: loop (3× seen) Bash",
        },
    ))

    assert len(out) == 30, f"want 30 cost cases, got {len(out)}"
    return out
