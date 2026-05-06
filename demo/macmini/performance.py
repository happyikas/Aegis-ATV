"""30 Performance test cases.

Exercises the latency-, loop-, and progress-focused advisors:
``loop-breaker``, ``context-compactor``, ``test-runner``,
``human-clarifier``. Coverage targets:

* step336 loop detection across all four loopable tools (Read, Bash,
  Edit, Grep) — both unit (heuristic step_traces) and e2e (3rd-call
  through the hook).
* Boundary tests on backtrack count, error count, cache drop.
* Progress-stalled and long-window scenarios.
* Cross-domain: error+loop, backtrack+velocity, cache+backtrack.
* Negative controls (different params → no loop, idle session).
"""
from __future__ import annotations

from .case import TestCase
from .fixtures import (
    ctx_5turns_expensive,
    ctx_idle,
    ctx_long_window,
    ctx_progress_stalled,
    ctx_with_backtracks,
    ctx_with_errors,
)


def _loop_trace(tool: str) -> dict[str, str]:
    return {
        "aegis.firewall.step336_loop.run":
            f"step336: loop (3× seen) {tool}",
    }


def cases() -> list[TestCase]:
    out: list[TestCase] = []
    add = out.append

    # ── loop-breaker × swap-tool unit sweep (4) ─────────────────────
    for tool, target in (
        ("Read", "Grep"),
        ("Bash", "Glob"),
        ("Edit", "Read"),
        ("Grep", "Glob"),
    ):
        add(TestCase(
            cid=f"PERF-{len(out) + 1:02d}",
            category="performance",
            title=f"loop-breaker {tool} → swap-tool",
            scenario=(
                f"step336 reports {tool} repeated 3x. loop-breaker "
                f"should recommend swap-tool with target {target}."
            ),
            execution_summary=(
                f"step_traces=loop {tool}"
            ),
            test_type="unit",
            expected_advisor="loop-breaker",
            expected_verb="swap-tool",
            current_tool=tool,
            step_traces=_loop_trace(tool),
        ))

    add(TestCase(
        cid="PERF-05",
        category="performance",
        title="loop-breaker priority — narrow-scope also present",
        scenario=(
            "Even when swap-tool fires, loop-breaker should also "
            "include narrow-scope as an alternative recovery."
        ),
        execution_summary=(
            "step_traces=loop Bash; expect narrow-scope verb"
        ),
        test_type="unit",
        expected_advisor="loop-breaker",
        expected_verb="narrow-scope",
        current_tool="Bash",
        step_traces=_loop_trace("Bash"),
    ))

    # ── error/diagnostic (3) ────────────────────────────────────────
    add(TestCase(
        cid="PERF-06",
        category="performance",
        title="n_errors=2 → run-diagnostic",
        scenario=(
            "Two error turns recorded; test-runner should fire to "
            "recommend running diagnostics before proceeding."
        ),
        execution_summary=(
            "ctx_with_errors(2)"
        ),
        test_type="unit",
        expected_advisor="test-runner",
        expected_verb="run-diagnostic",
        ctx_factory=lambda: ctx_with_errors(2),
    ))

    add(TestCase(
        cid="PERF-07",
        category="performance",
        title="error anomaly tag → run-diagnostic",
        scenario=(
            "Burn-in flagged session_error_rate as anomalous. "
            "test-runner should fire from the anomaly path."
        ),
        execution_summary=(
            "anomaly_metric='session_error_rate'"
        ),
        test_type="unit",
        expected_advisor="test-runner",
        expected_verb="run-diagnostic",
        anomaly_metric="session_error_rate",
    ))

    add(TestCase(
        cid="PERF-08",
        category="performance",
        title="n_errors=1 — boundary, test-runner silent",
        scenario=(
            "Single error doesn't yet warrant an action; "
            "test-runner must not fire."
        ),
        execution_summary=(
            "ctx_with_errors(1)"
        ),
        test_type="unit",
        expected_no_fire_for="test-runner",
        ctx_factory=lambda: ctx_with_errors(1),
    ))

    # ── backtrack/clarifier (3) ─────────────────────────────────────
    add(TestCase(
        cid="PERF-09",
        category="performance",
        title="n_backtracks=1 → clarify-intent",
        scenario=(
            "Operator backed out one turn; human-clarifier should "
            "recommend clarify-intent before continuing."
        ),
        execution_summary=(
            "ctx_with_backtracks(1)"
        ),
        test_type="unit",
        expected_advisor="human-clarifier",
        expected_verb="clarify-intent",
        ctx_factory=lambda: ctx_with_backtracks(1),
    ))

    add(TestCase(
        cid="PERF-10",
        category="performance",
        title="n_backtracks=5 → clarify-intent high prio",
        scenario=(
            "Repeated backtracks (5 in window). human-clarifier "
            "should fire with elevated priority."
        ),
        execution_summary=(
            "ctx_with_backtracks(5)"
        ),
        test_type="unit",
        expected_advisor="human-clarifier",
        expected_verb="clarify-intent",
        ctx_factory=lambda: ctx_with_backtracks(5),
    ))

    add(TestCase(
        cid="PERF-11",
        category="performance",
        title="n_backtracks=0 — clarifier silent",
        scenario=(
            "No backtracks; human-clarifier must not fire from a "
            "clean operator trail."
        ),
        execution_summary=(
            "ctx_idle()"
        ),
        test_type="unit",
        expected_no_fire_for="human-clarifier",
        ctx_factory=ctx_idle,
    ))

    # ── velocity/compactor (3) ──────────────────────────────────────
    add(TestCase(
        cid="PERF-12",
        category="performance",
        title="window velocity anomaly → summarize-window",
        scenario=(
            "window_token_velocity_per_turn flagged. "
            "context-compactor should fire with summarize-window."
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
        cid="PERF-13",
        category="performance",
        title="long window 50 turns → context-compactor fires",
        scenario=(
            "50-turn window with velocity anomaly; "
            "context-compactor must produce non-empty steps "
            "(more than 2 turns to compact)."
        ),
        execution_summary=(
            "ctx_long_window(50) + velocity anomaly"
        ),
        test_type="unit",
        expected_advisor="context-compactor",
        ctx_factory=ctx_long_window,
        anomaly_metric="window_token_velocity_per_turn",
    ))

    add(TestCase(
        cid="PERF-14",
        category="performance",
        title="progress stalled → context-compactor",
        scenario=(
            "is_progress_stalled=True with backtracks; "
            "context-compactor + human-clarifier expected."
        ),
        execution_summary=(
            "ctx_progress_stalled() + velocity anomaly"
        ),
        test_type="unit",
        expected_multi=("context-compactor", "human-clarifier"),
        ctx_factory=ctx_progress_stalled,
        anomaly_metric="window_token_velocity_per_turn",
    ))

    # ── cross-domain combos (4) ─────────────────────────────────────
    add(TestCase(
        cid="PERF-15",
        category="performance",
        title="error + loop → 2 advisors",
        scenario=(
            "Error trail and tool-loop simultaneously; test-runner "
            "and loop-breaker both fire."
        ),
        execution_summary=(
            "ctx_with_errors(2) + step_traces=loop Read"
        ),
        test_type="unit",
        expected_multi=("test-runner", "loop-breaker"),
        ctx_factory=lambda: ctx_with_errors(2),
        current_tool="Read",
        step_traces=_loop_trace("Read"),
    ))

    add(TestCase(
        cid="PERF-16",
        category="performance",
        title="backtrack + velocity → 2 advisors",
        scenario=(
            "Backtracks plus velocity anomaly; human-clarifier "
            "and context-compactor both fire."
        ),
        execution_summary=(
            "ctx_with_backtracks(2) + velocity anomaly"
        ),
        test_type="unit",
        expected_multi=("human-clarifier", "context-compactor"),
        ctx_factory=lambda: ctx_with_backtracks(2),
        anomaly_metric="window_token_velocity_per_turn",
    ))

    add(TestCase(
        cid="PERF-17",
        category="performance",
        title="cache + backtrack — kv-cache + clarifier",
        scenario=(
            "Cache drop with backtracking operator; both surfaces "
            "should fire concurrently."
        ),
        execution_summary=(
            "cache drop 51pp + n_backtracks=1"
        ),
        test_type="unit",
        expected_multi=("kv-cache-optimizer", "human-clarifier"),
        ctx_factory=lambda: ctx_with_backtracks(1),
        cache_signals={"cache_hit_rate_max_drop_pp": 51.0},
    ))

    add(TestCase(
        cid="PERF-18",
        category="performance",
        title="loop + cost — narrow-scope + swap-model",
        scenario=(
            "Loop and budget pressure stacked; loop-breaker plus "
            "cost-optimizer expected."
        ),
        execution_summary=(
            "step_traces=loop Bash + budget 1.5x"
        ),
        test_type="unit",
        expected_multi=("loop-breaker", "cost-optimizer"),
        ctx_factory=ctx_5turns_expensive,
        current_tool="Bash",
        current_model="claude-opus-4-7",
        cost_signals={"budget_used_ratio": 1.5},
        step_traces=_loop_trace("Bash"),
    ))

    # ── e2e loop tests through hook (4) ─────────────────────────────
    for li, (tool, cmd) in enumerate([
        ("Bash", "echo perf-loop-A"),
        ("Bash", "echo perf-loop-B"),
        ("Bash", "echo perf-loop-C"),
        ("Bash", "echo perf-loop-D"),
    ]):
        add(TestCase(
            cid=f"PERF-{19 + li:02d}",
            category="performance",
            title=f"loop-3rd e2e #{li + 1} — loop-breaker + swap-tool",
            scenario=(
                "PreToolUse hook called 3 times with the same "
                "command. step336 detects on the 3rd call; audit "
                "should carry loop-breaker with swap-tool."
            ),
            execution_summary=(
                f"3x PreToolUse {tool} {cmd!r}"
            ),
            test_type="e2e",
            expected_advisor="loop-breaker",
            expected_verb="swap-tool",
            loop_priming=2,
            pre_event={
                "hook_event_name": "PreToolUse",
                "session_id": f"macmini-perf-loop-{li}",
                "invocation_id": f"macmini-perf-loop-{li}-3",
                "tool_name": tool,
                "tool_input": {"command": cmd},
            },
        ))

    # ── e2e routine ALLOWs that should NOT trigger anything (3) ─────
    add(TestCase(
        cid="PERF-23",
        category="performance",
        title="single-call Bash (e2e) — no loop fire",
        scenario=(
            "First call to Bash; step336 must not flag a loop on a "
            "single invocation."
        ),
        execution_summary=(
            "PreToolUse Bash 'echo single' (1x)"
        ),
        test_type="e2e",
        expected_no_fire=True,
        expected_decision="ALLOW",
        pre_event={
            "hook_event_name": "PreToolUse",
            "session_id": "macmini-perf-single",
            "invocation_id": "macmini-perf-single-1",
            "tool_name": "Bash",
            "tool_input": {"command": "echo single"},
        },
    ))

    add(TestCase(
        cid="PERF-24",
        category="performance",
        title="2 calls only (e2e) — boundary, no loop",
        scenario=(
            "step336 fires on the 3rd repeat. With only 2 calls "
            "(1 priming + 1 actual), no loop should be detected."
        ),
        execution_summary=(
            "PreToolUse Bash 'echo two-only' (2x total)"
        ),
        test_type="e2e",
        expected_no_fire_for="loop-breaker",
        expected_decision="ALLOW",
        loop_priming=1,
        pre_event={
            "hook_event_name": "PreToolUse",
            "session_id": "macmini-perf-two",
            "invocation_id": "macmini-perf-two-2",
            "tool_name": "Bash",
            "tool_input": {"command": "echo two-only"},
        },
    ))

    add(TestCase(
        cid="PERF-25",
        category="performance",
        title="different params (e2e) — no loop",
        scenario=(
            "step336 keys on (tool, params); calls with different "
            "params don't form a loop. No loop-breaker should fire."
        ),
        execution_summary=(
            "PreToolUse Bash 'echo a' / 'echo b' / 'echo c'"
        ),
        test_type="e2e",
        expected_no_fire_for="loop-breaker",
        expected_decision="ALLOW",
        pre_event={
            "hook_event_name": "PreToolUse",
            "session_id": "macmini-perf-distinct",
            "invocation_id": "macmini-perf-distinct-3",
            "tool_name": "Bash",
            "tool_input": {"command": "echo c"},
        },
    ))

    # ── boundary + control (5) ──────────────────────────────────────
    add(TestCase(
        cid="PERF-26",
        category="performance",
        title="ALLOW + clean ctx (unit) — fully silent",
        scenario=(
            "Idle context, ALLOW base, no signals. Heuristic must "
            "produce zero recommendations."
        ),
        execution_summary=(
            "ctx_idle(), no signals"
        ),
        test_type="unit",
        expected_no_fire=True,
        ctx_factory=ctx_idle,
    ))

    add(TestCase(
        cid="PERF-27",
        category="performance",
        title="velocity anomaly only → context-compactor",
        scenario=(
            "Velocity anomaly without ctx still produces a "
            "compactor recommendation from the anomaly path."
        ),
        execution_summary=(
            "anomaly_metric='window_token_velocity_per_turn' alone"
        ),
        test_type="unit",
        expected_advisor="context-compactor",
        ctx_factory=ctx_5turns_expensive,
        anomaly_metric="window_token_velocity_per_turn",
    ))

    add(TestCase(
        cid="PERF-28",
        category="performance",
        title="loop + error + cache — 3 advisors",
        scenario=(
            "Triple performance signal: loop, error, cache drop. "
            "loop-breaker + test-runner + kv-cache-optimizer."
        ),
        execution_summary=(
            "loop+error+cache stacked"
        ),
        test_type="unit",
        expected_multi=(
            "loop-breaker", "test-runner", "kv-cache-optimizer",
        ),
        ctx_factory=lambda: ctx_with_errors(2),
        current_tool="Edit",
        cache_signals={"cache_hit_rate_max_drop_pp": 51.0},
        step_traces=_loop_trace("Edit"),
    ))

    add(TestCase(
        cid="PERF-29",
        category="performance",
        title="step336 trace without loop substring — no fire",
        scenario=(
            "Step traces present but no 'loop' keyword. "
            "loop-breaker must not fire from spurious traces."
        ),
        execution_summary=(
            "step_traces={'step336': 'no-op'}"
        ),
        test_type="unit",
        expected_no_fire_for="loop-breaker",
        current_tool="Bash",
        step_traces={"aegis.firewall.step336_loop.run": "no-op"},
    ))

    add(TestCase(
        cid="PERF-30",
        category="performance",
        title="long window + budget — compactor + cost",
        scenario=(
            "50-turn window over budget; both context-compactor "
            "and cost-optimizer should fire."
        ),
        execution_summary=(
            "ctx_long_window(50), budget=1.2x"
        ),
        test_type="unit",
        expected_multi=("context-compactor", "cost-optimizer"),
        ctx_factory=ctx_long_window,
        cost_signals={"budget_used_ratio": 1.2},
        anomaly_metric="window_token_velocity_per_turn",
    ))

    assert len(out) == 30, f"want 30 perf cases, got {len(out)}"
    return out
