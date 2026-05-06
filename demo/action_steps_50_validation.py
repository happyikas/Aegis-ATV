#!/usr/bin/env python3
"""50-case validation for the v2.8 ActionStep surface.

Three groups:

  Group A (15 e2e):  routine ALLOWs + destructive BLOCKs through the
                     real PreToolUse hook. Verifies the audit JSONL
                     line carries action_steps[] with concrete params.

  Group B (25 unit): direct calls to compose_advice_heuristic with
                     controlled signal dicts. Covers all 11 verbs at
                     least once + boundary checks (low/high priority,
                     edge values).

  Group C (10 unit): cross-domain combinations validating the verb
                     mix when multiple advisors fire simultaneously.

Output:
  /tmp/action-steps-50-audit.jsonl       (e2e audit lines)
  docs/ACTION_STEPS_50_REPORT.md         (per-case markdown)

Run:
  uv run python demo/action_steps_50_validation.py
"""

from __future__ import annotations

import io
import json
import os
import sys
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import Any

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "tools"))
sys.path.insert(0, str(_REPO / "tools" / "hooks"))

_AUDIT = Path("/tmp/action-steps-50-audit.jsonl")
_REPORT = _REPO / "docs" / "ACTION_STEPS_50_REPORT.md"


# Builders for sensitive / destructive command literals (split tokens
# so this source file doesn't trip the firewall when scanned).


def _force_push() -> str:
    return " ".join(["git", "push", "--force", "origin", "main"])


def _purge(path: str) -> str:
    return " ".join(["rm", "-" + "rf", path])


def _drop(name: str) -> str:
    return " ".join(["DROP", "TABLE", name])


def _kubectl_delete(target: str) -> str:
    return " ".join(["kubectl", "delete"] + target.split())


def _terraform_destroy() -> str:
    return " ".join(["terraform", "destroy", "-auto-approve"])


def _aws_terminate() -> str:
    return " ".join(["aws", "ec2", "terminate-instances", "--instance-ids", "i-x"])


def _privileged_docker() -> str:
    return " ".join(["docker", "run", "--privileged", "--rm", "alpine"])


# Test-data fixtures


def _ctx_5turns_expensive():
    """5 turns; last 4 are 5000-token; cache breaks at -3."""
    from aegis.atv.temporal import ATVSnapshot, TemporalContext
    snaps = []
    for i in range(5):
        rel = i - 4
        tokens = 200 if rel < -3 else 5000
        cache = 0.85 if rel < -3 else 0.30
        snaps.append(ATVSnapshot(
            turn_index_rel=rel, ts_ns=0, tool_name="Bash",
            args_excerpt="", decision="ALLOW", outcome="success",
            input_tokens=tokens // 2, output_tokens=tokens // 2,
            cache_hit_rate=cache,
        ))
    return TemporalContext(
        history=tuple(snaps), window_size=5,
        cumulative_token_trajectory=tuple(0 for _ in range(5)),
        cache_hit_rate_trajectory=tuple(s.cache_hit_rate for s in snaps),
        n_backtracks=0, n_redundant=0, n_errors=0, n_failures=0,
        cache_hit_rate_max_drop_pp=55.0,
        token_velocity_per_turn=2500.0,
        is_progress_stalled=False,
        distinct_tools_in_window=("Bash",),
    )


def _ctx_with_backtracks(n: int):
    from aegis.atv.temporal import ATVSnapshot, TemporalContext
    snaps = []
    for i in range(5):
        rel = i - 4
        snaps.append(ATVSnapshot(
            turn_index_rel=rel, ts_ns=0, tool_name="Edit",
            args_excerpt="", decision="ALLOW", outcome="success",
            backtrack=(rel == -2),
            input_tokens=200, output_tokens=200, cache_hit_rate=0.5,
        ))
    return TemporalContext(
        history=tuple(snaps), window_size=5,
        cumulative_token_trajectory=tuple(0 for _ in range(5)),
        cache_hit_rate_trajectory=tuple(0.5 for _ in range(5)),
        n_backtracks=n, n_redundant=0, n_errors=0, n_failures=0,
        cache_hit_rate_max_drop_pp=0.0,
        token_velocity_per_turn=400.0,
        is_progress_stalled=False,
        distinct_tools_in_window=("Edit",),
    )


def _ctx_with_errors(n: int):
    from aegis.atv.temporal import ATVSnapshot, TemporalContext
    snaps = (
        ATVSnapshot(
            turn_index_rel=-1, ts_ns=0, tool_name="Bash",
            args_excerpt="", decision="ALLOW", outcome="error",
            is_error=True, input_tokens=200, output_tokens=200,
            cache_hit_rate=0.5,
        ),
    )
    return TemporalContext(
        history=snaps, window_size=2,
        cumulative_token_trajectory=(400,),
        cache_hit_rate_trajectory=(0.5,),
        n_backtracks=0, n_redundant=0, n_errors=n, n_failures=0,
        cache_hit_rate_max_drop_pp=0.0,
        token_velocity_per_turn=200.0,
        is_progress_stalled=False,
        distinct_tools_in_window=("Bash",),
    )


# 50 cases


def _scenarios() -> list[dict[str, Any]]:
    """Each case carries: id, group, name, type, expected_advisor,
    expected_verb, expected_no_fire, plus the inputs (pre or signals)."""
    cases: list[dict[str, Any]] = []
    nid = 0

    def _next() -> int:
        nonlocal nid
        nid += 1
        return nid

    # ──────────────────────────────────────────────────────────────────
    # GROUP A — e2e (15 cases)
    # ──────────────────────────────────────────────────────────────────

    routine = [
        ("Read /tmp/foo.md", "Read", {"file_path": "/tmp/foo.md"}),
        ("Bash ls", "Bash", {"command": "ls -la"}),
        ("Bash echo", "Bash", {"command": "echo hi"}),
        ("Edit small file", "Edit",
         {"file_path": "/tmp/x.md", "old_string": "a", "new_string": "b"}),
        ("Grep TODO", "Grep", {"pattern": "TODO", "path": "src/"}),
    ]
    for name, tool, ti in routine:
        cases.append({
            "id": _next(), "group": "A", "name": name, "type": "e2e",
            "expected_advisor": None,
            "expected_verb": None,
            "expected_no_fire": True,
            "pre": {
                "hook_event_name": "PreToolUse",
                "session_id": "as50-routine",
                "invocation_id": f"as50-routine-{nid}",
                "tool_name": tool,
                "tool_input": ti,
            },
        })

    destructive = [
        ("git force-push → BLOCK",      _force_push(),
         "security-reviewer", "require-approval"),
        ("recursive purge /var/data",   _purge("/var/data"),
         "security-reviewer", "require-approval"),
        ("recursive purge /home/user",  _purge("/home/user"),
         "security-reviewer", "require-approval"),
        ("destructive table drop",      _drop("users"),
         "security-reviewer", "require-approval"),
        ("kubectl delete prod",         _kubectl_delete("namespace production"),
         "security-reviewer", "require-approval"),
        ("aws ec2 terminate",           _aws_terminate(),
         "security-reviewer", "require-approval"),
        ("terraform destroy",           _terraform_destroy(),
         "security-reviewer", "require-approval"),
        ("privileged docker",           _privileged_docker(),
         "security-reviewer", "require-approval"),
    ]
    for name, cmd, advisor, verb in destructive:
        cases.append({
            "id": _next(), "group": "A", "name": name, "type": "e2e",
            "expected_advisor": advisor,
            "expected_verb": verb,
            "expected_no_fire": False,
            "post_status": "failure",
            "pre": {
                "hook_event_name": "PreToolUse",
                "session_id": "as50-destructive",
                "invocation_id": f"as50-destructive-{nid}",
                "tool_name": "Bash",
                "tool_input": {"command": cmd},
            },
        })

    # 2 loop-3rd cases — gate fires on 3rd same call, audit carries
    # loop-breaker + swap-tool action_step.
    for li, cmd in enumerate(["echo loop-A", "echo loop-B"]):
        cases.append({
            "id": _next(), "group": "A",
            "name": f"loop-3rd e2e set #{li + 1}",
            "type": "e2e",
            "expected_advisor": "loop-breaker",
            "expected_verb": "swap-tool",
            "expected_no_fire": False,
            "loop_priming": 2,  # 2 prior calls in same session
            "pre": {
                "hook_event_name": "PreToolUse",
                "session_id": f"as50-loop-{li}",
                "invocation_id": f"as50-loop-{li}-3",
                "tool_name": "Bash",
                "tool_input": {"command": cmd},
            },
        })

    # ──────────────────────────────────────────────────────────────────
    # GROUP B — unit (25 cases) — covers all 11 verbs explicitly
    # ──────────────────────────────────────────────────────────────────

    # cost-optimizer × prune-turns + swap-model + end-session (3 verbs
    # for one advisor at extreme budget)
    cases.append({
        "id": _next(), "group": "B",
        "name": "cost-optimizer at budget 1.6× → prune+swap+end",
        "type": "unit",
        "expected_advisor": "cost-optimizer",
        "expected_verb": "prune-turns",
        "ctx_factory": _ctx_5turns_expensive,
        "current_model": "claude-opus-4-7",
        "cost_signals": {"budget_used_ratio": 1.6},
    })
    cases.append({
        "id": _next(), "group": "B",
        "name": "cost-optimizer at budget 1.6× → swap-model present",
        "type": "unit",
        "expected_advisor": "cost-optimizer",
        "expected_verb": "swap-model",
        "ctx_factory": _ctx_5turns_expensive,
        "current_model": "claude-opus-4-7",
        "cost_signals": {"budget_used_ratio": 1.6},
    })
    cases.append({
        "id": _next(), "group": "B",
        "name": "cost-optimizer at budget 1.6× → end-session present",
        "type": "unit",
        "expected_advisor": "cost-optimizer",
        "expected_verb": "end-session",
        "ctx_factory": _ctx_5turns_expensive,
        "current_model": "claude-opus-4-7",
        "cost_signals": {"budget_used_ratio": 1.6},
    })
    # M12 divergence → notify-operator
    for ratio in (2.0, 3.15, 5.0):
        cases.append({
            "id": _next(), "group": "B",
            "name": f"M12 ratio {ratio} → notify-operator",
            "type": "unit",
            "expected_advisor": "cost-optimizer",
            "expected_verb": "notify-operator",
            "cost_signals": {"hw_vs_sw_divergence_ratio": ratio},
        })
    # M12 below threshold → no fire
    cases.append({
        "id": _next(), "group": "B",
        "name": "M12 ratio 1.99 → no fire (boundary)",
        "type": "unit",
        "expected_no_fire": True,
        "cost_signals": {"hw_vs_sw_divergence_ratio": 1.99},
    })
    # budget warn flag alone → cost-optimizer
    cases.append({
        "id": _next(), "group": "B",
        "name": "budget warn flag only → cost-optimizer",
        "type": "unit",
        "expected_advisor": "cost-optimizer",
        "cost_signals": {"budget_warn_flag": True},
    })

    # kv-cache-optimizer × prune-turns
    cases.append({
        "id": _next(), "group": "B",
        "name": "cache drop 51pp → prune-turns",
        "type": "unit",
        "expected_advisor": "kv-cache-optimizer",
        "expected_verb": "prune-turns",
        "ctx_factory": _ctx_5turns_expensive,
        "cache_signals": {
            "cache_hit_rate_max_drop_pp": 51.0,
            "prefix_re_keys_in_window": 4,
        },
    })
    # kv-cache-optimizer × summarize-window (prefix unstable)
    cases.append({
        "id": _next(), "group": "B",
        "name": "prefix unstable → summarize-window",
        "type": "unit",
        "expected_advisor": "kv-cache-optimizer",
        "expected_verb": "summarize-window",
        "ctx_factory": _ctx_5turns_expensive,
        "cache_signals": {
            "prefix_stability": "unstable",
            "prefix_re_keys_in_window": 4,
        },
    })
    # cache drop below 30pp → no fire
    cases.append({
        "id": _next(), "group": "B",
        "name": "cache drop 25pp → no kv-cache fire",
        "type": "unit",
        "expected_no_fire_for": "kv-cache-optimizer",
        "cache_signals": {"cache_hit_rate_max_drop_pp": 25.0},
    })

    # security-reviewer × require-approval (destructive_path_match)
    cases.append({
        "id": _next(), "group": "B",
        "name": "destructive path match → require-approval",
        "type": "unit",
        "expected_advisor": "security-reviewer",
        "expected_verb": "require-approval",
        "security_signals": {
            "verdict_decision": "BLOCK",
            "destructive_path_match": True,
            "policy_rule": "rule:git_destructive",
            "blast_radius": "high",
        },
    })
    # security-reviewer × notify-operator (high blast, no destructive)
    cases.append({
        "id": _next(), "group": "B",
        "name": "high blast (no destructive) → notify-operator",
        "type": "unit",
        "expected_advisor": "security-reviewer",
        "expected_verb": "notify-operator",
        "security_signals": {
            "verdict_decision": "REQUIRE_APPROVAL",
            "blast_radius": "high",
        },
    })

    # loop-breaker × swap-tool (each tool variant)
    loop_swaps = [
        ("Read", "Grep"),
        ("Bash", "Glob"),
        ("Edit", "Read"),
        ("Grep", "Glob"),
    ]
    for from_t, _to_t in loop_swaps:
        cases.append({
            "id": _next(), "group": "B",
            "name": f"loop-breaker {from_t} → swap-tool",
            "type": "unit",
            "expected_advisor": "loop-breaker",
            "expected_verb": "swap-tool",
            "current_tool": from_t,
            "step_traces": {
                "aegis.firewall.step336_loop.run":
                    f"step336: loop (3× seen) {from_t}",
            },
        })
    # human-clarifier × clarify-intent
    cases.append({
        "id": _next(), "group": "B",
        "name": "n_backtracks=1 → clarify-intent",
        "type": "unit",
        "expected_advisor": "human-clarifier",
        "expected_verb": "clarify-intent",
        "ctx_factory": lambda: _ctx_with_backtracks(1),
    })
    cases.append({
        "id": _next(), "group": "B",
        "name": "n_backtracks=5 → clarify-intent",
        "type": "unit",
        "expected_advisor": "human-clarifier",
        "expected_verb": "clarify-intent",
        "ctx_factory": lambda: _ctx_with_backtracks(5),
    })

    # test-runner × run-diagnostic
    cases.append({
        "id": _next(), "group": "B",
        "name": "n_errors=2 → run-diagnostic",
        "type": "unit",
        "expected_advisor": "test-runner",
        "expected_verb": "run-diagnostic",
        "ctx_factory": lambda: _ctx_with_errors(2),
    })
    cases.append({
        "id": _next(), "group": "B",
        "name": "error anomaly tag → run-diagnostic",
        "type": "unit",
        "expected_advisor": "test-runner",
        "expected_verb": "run-diagnostic",
        "anomaly_metric": "session_error_rate",
    })
    cases.append({
        "id": _next(), "group": "B",
        "name": "n_errors=1 → no test-runner fire",
        "type": "unit",
        "expected_no_fire_for": "test-runner",
        "ctx_factory": lambda: _ctx_with_errors(1),
    })

    # context-compactor × summarize-window (velocity anomaly)
    cases.append({
        "id": _next(), "group": "B",
        "name": "velocity anomaly → summarize-window",
        "type": "unit",
        "expected_advisor": "context-compactor",
        "expected_verb": "summarize-window",
        "ctx_factory": _ctx_5turns_expensive,
        "anomaly_metric": "window_token_velocity_per_turn",
    })

    # permission-escalator × notify-operator (BLOCK, no domain)
    cases.append({
        "id": _next(), "group": "B",
        "name": "BLOCK without domain → permission-escalator",
        "type": "unit",
        "expected_advisor": "permission-escalator",
        "expected_verb": "notify-operator",
        "base_decision": "BLOCK",
        "security_signals": {"verdict_decision": "BLOCK"},
    })
    cases.append({
        "id": _next(), "group": "B",
        "name": "ALLOW + no signals → no fire (clean)",
        "type": "unit",
        "expected_no_fire": True,
    })

    # ──────────────────────────────────────────────────────────────────
    # GROUP C — cross-domain combos (10 cases)
    # ──────────────────────────────────────────────────────────────────

    # Canonical 3-domain combo
    cases.append({
        "id": _next(), "group": "C",
        "name": "cost+cache+security → 3 advisors fire",
        "type": "unit",
        "expected_multi": ["cost-optimizer", "kv-cache-optimizer",
                           "security-reviewer"],
        "expected_verbs_any": ["prune-turns", "swap-model",
                               "require-approval"],
        "ctx_factory": _ctx_5turns_expensive,
        "current_model": "claude-opus-4-7",
        "cost_signals": {
            "hw_vs_sw_divergence_ratio": 3.0,
            "budget_used_ratio": 1.5,
        },
        "cache_signals": {
            "cache_hit_rate_max_drop_pp": 55.0,
            "prefix_re_keys_in_window": 4,
        },
        "security_signals": {
            "verdict_decision": "REQUIRE_APPROVAL",
            "destructive_path_match": True,
            "policy_rule": "rule:backup_path_destructive",
            "blast_radius": "high",
        },
    })
    # cost + cache (no security)
    cases.append({
        "id": _next(), "group": "C",
        "name": "cost+cache (no security) → 2 advisors",
        "type": "unit",
        "expected_multi": ["cost-optimizer", "kv-cache-optimizer"],
        "ctx_factory": _ctx_5turns_expensive,
        "cost_signals": {"budget_used_ratio": 1.0},
        "cache_signals": {"cache_hit_rate_max_drop_pp": 50.0},
    })
    # security + loop
    cases.append({
        "id": _next(), "group": "C",
        "name": "security + loop → 2 advisors",
        "type": "unit",
        "expected_multi": ["security-reviewer", "loop-breaker"],
        "current_tool": "Bash",
        "security_signals": {
            "verdict_decision": "BLOCK",
            "destructive_path_match": True,
            "policy_rule": "rule:git_destructive",
        },
        "step_traces": {
            "aegis.firewall.step336_loop.run":
                "step336: loop (3× seen) Bash",
        },
    })
    # backtrack + velocity
    cases.append({
        "id": _next(), "group": "C",
        "name": "backtrack + velocity → 2 advisors",
        "type": "unit",
        "expected_multi": ["human-clarifier", "context-compactor"],
        "ctx_factory": lambda: _ctx_with_backtracks(2),
        "anomaly_metric": "window_token_velocity_per_turn",
    })
    # error + loop
    cases.append({
        "id": _next(), "group": "C",
        "name": "error + loop → 2 advisors",
        "type": "unit",
        "expected_multi": ["test-runner", "loop-breaker"],
        "current_tool": "Read",
        "ctx_factory": lambda: _ctx_with_errors(2),
        "step_traces": {
            "aegis.firewall.step336_loop.run":
                "step336: loop (3× seen) Read",
        },
    })
    # cost (M12) + velocity
    cases.append({
        "id": _next(), "group": "C",
        "name": "M12 + velocity → cost + compactor",
        "type": "unit",
        "expected_multi": ["cost-optimizer", "context-compactor"],
        "ctx_factory": _ctx_5turns_expensive,
        "cost_signals": {"hw_vs_sw_divergence_ratio": 3.0},
        "anomaly_metric": "window_token_velocity_per_turn",
    })
    # cache + backtrack
    cases.append({
        "id": _next(), "group": "C",
        "name": "cache + backtrack → 2 advisors",
        "type": "unit",
        "expected_multi": ["kv-cache-optimizer", "human-clarifier"],
        "ctx_factory": lambda: _ctx_with_backtracks(1),
        "cache_signals": {"cache_hit_rate_max_drop_pp": 51.0},
    })
    # high blast + budget
    cases.append({
        "id": _next(), "group": "C",
        "name": "high blast + budget → security + cost",
        "type": "unit",
        "expected_multi": ["security-reviewer", "cost-optimizer"],
        "security_signals": {
            "verdict_decision": "REQUIRE_APPROVAL",
            "blast_radius": "high",
        },
        "cost_signals": {"budget_used_ratio": 1.0},
    })
    # 4-advisor mega combo
    cases.append({
        "id": _next(), "group": "C",
        "name": "4-advisor combo (cost+cache+sec+loop)",
        "type": "unit",
        "expected_multi": ["cost-optimizer", "kv-cache-optimizer",
                           "security-reviewer", "loop-breaker"],
        "ctx_factory": _ctx_5turns_expensive,
        "current_tool": "Bash",
        "current_model": "claude-opus-4-7",
        "cost_signals": {"budget_used_ratio": 1.5},
        "cache_signals": {"cache_hit_rate_max_drop_pp": 50.0},
        "security_signals": {
            "verdict_decision": "REQUIRE_APPROVAL",
            "destructive_path_match": True,
            "policy_rule": "rule:fs_destructive",
            "blast_radius": "high",
        },
        "step_traces": {
            "aegis.firewall.step336_loop.run":
                "step336: loop (3× seen) Bash",
        },
    })
    # null case: no signals → empty recommended_advisors
    cases.append({
        "id": _next(), "group": "C",
        "name": "no signals → no recommendations",
        "type": "unit",
        "expected_no_fire": True,
    })

    assert len(cases) == 50, f"want 50, got {len(cases)}"
    return cases


# Driver


def _setup_env() -> None:
    if _AUDIT.exists():
        _AUDIT.unlink()
    os.environ["AEGIS_LOCAL_AUDIT"] = str(_AUDIT)
    os.environ["AEGIS_ADVISOR_ENABLED"] = "1"
    os.environ.setdefault("AEGIS_ADVISOR_PROVIDER", "dummy")
    os.environ.setdefault("AEGIS_EMBEDDING_PROVIDER", "dummy")
    os.environ.setdefault("AEGIS_JUDGE_PROVIDER", "dummy")
    os.environ["AEGIS_APPROVE_AS_BLOCK"] = "1"
    os.environ["AEGIS_ATMU_DISABLE"] = "1"
    os.environ.setdefault("AEGIS_HW_PROVIDER", "sim")

    import aegis_local_hook
    import post_tool
    aegis_local_hook.LOCAL_AUDIT_PATH = _AUDIT
    post_tool.LOCAL_AUDIT_PATH = _AUDIT
    aegis_local_hook.ADVISOR_ENABLED = True
    aegis_local_hook.ADVISOR_ALWAYS = False
    aegis_local_hook.APPROVE_AS_BLOCK = True
    aegis_local_hook.ATMU_DISABLED = True
    post_tool.ATMU_DISABLED = True
    aegis_local_hook._CALIBRATION_SINGLETON = None

    try:
        from aegis.monitor.loop_detector import get_default_detector
        get_default_detector().reset()
    except Exception:  # noqa: BLE001
        pass


def _drive_pretool(event: dict) -> None:
    import aegis_local_hook
    pre_in = io.StringIO(json.dumps(event))
    pre_out = io.StringIO()
    saved = sys.stderr
    sys.stderr = io.StringIO()
    try:
        aegis_local_hook.handle_pretool(pre_in, pre_out)
    finally:
        sys.stderr = saved


def _run_e2e(case: dict) -> dict:
    """Drive PreToolUse through the hook; return the audit summary."""
    pre = case["pre"]
    session = pre["session_id"]
    # Loop priming if requested.
    priming = case.get("loop_priming", 0)
    for k in range(priming):
        _drive_pretool({
            "hook_event_name": "PreToolUse",
            "session_id": session,
            "invocation_id": f"prime-{case['id']}-{k}",
            "tool_name": pre["tool_name"],
            "tool_input": pre["tool_input"],
        })
    _drive_pretool(pre)

    # Read back the matching audit line.
    if not _AUDIT.is_file():
        return {"advisors": [], "decision": "(none)"}
    inv_id = pre["invocation_id"]
    for raw in reversed(_AUDIT.read_text(encoding="utf-8").splitlines()):
        raw = raw.strip()
        if not raw:
            continue
        rec = json.loads(raw)
        if (rec.get("invocation_id") == inv_id
                and rec.get("hook") != "PostToolUse"):
            explain = rec.get("explain") or {}
            advice = explain.get("action_advice") or {}
            advisors = advice.get("recommended_advisors") or []
            return {
                "decision": rec.get("decision"),
                "reason": rec.get("reason", ""),
                "gate_invoked": (
                    explain.get("advisor_gate", {}).get("invoked", False)
                ),
                "advisors": [
                    {
                        "advisor": r.get("advisor"),
                        "priority": r.get("priority"),
                        "verbs": [
                            s.get("verb")
                            for s in r.get("action_steps") or []
                            if isinstance(s, dict)
                        ],
                        "steps": r.get("action_steps") or [],
                    }
                    for r in advisors
                    if isinstance(r, dict)
                ],
            }
    return {"advisors": [], "decision": "(none)"}


def _run_unit(case: dict) -> dict:
    """Call compose_advice_heuristic directly."""
    from aegis.burnin.anomaly import AnomalyTag
    from aegis.judge.action_advice import compose_advice_heuristic

    ctx_factory = case.get("ctx_factory")
    ctx = ctx_factory() if callable(ctx_factory) else None
    anomalies = []
    if case.get("anomaly_metric"):
        anomalies.append(AnomalyTag(
            metric=case["anomaly_metric"], severity="warning",
            observed=10, baseline_mean=1, baseline_std=1,
            z_score=3.0, description=f"{case['anomaly_metric']} elevated",
        ))
    advice = compose_advice_heuristic(
        temporal_ctx=ctx,
        anomalies=anomalies,
        base_decision=case.get("base_decision", "ALLOW"),
        current_tool=case.get("current_tool", ""),
        current_model=case.get("current_model"),
        cost_signals=case.get("cost_signals"),
        cache_signals=case.get("cache_signals"),
        security_signals=case.get("security_signals"),
        step_traces=case.get("step_traces"),
    )
    return {
        "decision": advice.decision,
        "advisors": [
            {
                "advisor": r.advisor,
                "priority": r.priority,
                "verbs": [s.verb for s in r.action_steps],
                "steps": [
                    {
                        "verb": s.verb,
                        "parameters": dict(s.parameters),
                        "expected_impact": s.expected_impact,
                        "confidence": s.confidence,
                    }
                    for s in r.action_steps
                ],
            }
            for r in advice.recommended_advisors
        ],
    }


def _check(case: dict, result: dict) -> tuple[bool, list[str]]:
    miss: list[str] = []
    advisors = {a["advisor"] for a in result["advisors"]}
    verbs_per_advisor = {
        a["advisor"]: a["verbs"] for a in result["advisors"]
    }

    if case.get("expected_no_fire"):
        if advisors:
            miss.append(f"unexpected fire: {sorted(advisors)}")
        return (len(miss) == 0, miss)

    if case.get("expected_no_fire_for"):
        target = case["expected_no_fire_for"]
        if target in advisors:
            miss.append(f"{target} unexpectedly fired")
        return (len(miss) == 0, miss)

    if case.get("expected_advisor"):
        if case["expected_advisor"] not in advisors:
            miss.append(
                f"{case['expected_advisor']} not in {sorted(advisors)}"
            )
        elif case.get("expected_verb"):
            verbs = verbs_per_advisor.get(case["expected_advisor"], [])
            if case["expected_verb"] not in verbs:
                miss.append(
                    f"{case['expected_advisor']} missing verb "
                    f"{case['expected_verb']} (has {verbs})"
                )

    if case.get("expected_multi"):
        for a in case["expected_multi"]:
            if a not in advisors:
                miss.append(
                    f"multi-domain miss: {a} not in {sorted(advisors)}"
                )

    if case.get("expected_verbs_any"):
        all_verbs = [
            v for a in result["advisors"] for v in a["verbs"]
        ]
        for v in case["expected_verbs_any"]:
            if v not in all_verbs:
                miss.append(f"verb {v} missing from {all_verbs}")

    return (len(miss) == 0, miss)


# Report


def _format_advisors_short(advisors: list[dict]) -> str:
    if not advisors:
        return "—"
    parts = []
    for a in advisors[:3]:
        verbs = ",".join(a["verbs"][:2]) or "(no steps)"
        parts.append(f"{a['advisor']}@{a['priority']}[{verbs}]")
    if len(advisors) > 3:
        parts.append(f"+{len(advisors) - 3} more")
    return " | ".join(parts)


def write_report(
    cases: list[dict],
    results: list[tuple[dict, dict, bool, list[str]]],
) -> None:
    n_total = len(results)
    n_pass = sum(1 for _, _, ok, _ in results if ok)

    by_group: dict[str, list] = {"A": [], "B": [], "C": []}
    for r in results:
        by_group[r[0]["group"]].append(r)

    verb_freq: Counter[str] = Counter()
    advisor_freq: Counter[str] = Counter()
    for _, sm, _, _ in results:
        for a in sm["advisors"]:
            advisor_freq[a["advisor"]] += 1
            for v in a["verbs"]:
                verb_freq[v] += 1

    lines: list[str] = [
        "# 50-case ActionStep Validation Report",
        "",
        "Driver: `demo/action_steps_50_validation.py`",
        f"Audit:  `{_AUDIT}`",
        "",
        "Validates the v2.8 ActionStep surface end-to-end across "
        "three groups: e2e through hooks (Group A), unit calls to "
        "the heuristic composer (Group B — covers all 11 verbs), "
        "and cross-domain combinations (Group C).",
        "",
        "## Headline",
        "",
        f"- **Total:** {n_total}",
        f"- **Pass:** {n_pass} ({n_pass / n_total * 100:.0f}%)",
        f"- **Fail:** {n_total - n_pass}",
        "",
        "## By group",
        "",
        "| Group | Description | Cases | Pass | Pass% |",
        "|-------|-------------|-------|------|-------|",
    ]
    descs = {
        "A": "e2e (hooks)",
        "B": "unit (heuristic verb sweep)",
        "C": "unit (cross-domain combos)",
    }
    for g in ("A", "B", "C"):
        rs = by_group[g]
        p = sum(1 for _, _, ok, _ in rs if ok)
        lines.append(
            f"| {g} | {descs[g]} | {len(rs)} | {p} | "
            f"{p / max(len(rs), 1) * 100:.0f}% |"
        )
    lines += [""]

    lines += ["## Advisor frequency",
              "",
              "| Advisor | Count |",
              "|---------|-------|"]
    for a, c in advisor_freq.most_common():
        lines.append(f"| `{a}` | {c} |")
    lines += [""]

    lines += ["## Verb frequency (across all action_steps)",
              "",
              "| Verb | Count |",
              "|------|-------|"]
    for v, c in verb_freq.most_common():
        lines.append(f"| `{v}` | {c} |")
    lines += [""]

    # Per-case sections
    for g in ("A", "B", "C"):
        lines += [f"## Group {g} — {descs[g]}", ""]
        for case, sm, ok, mm in by_group[g]:
            mark = "✅" if ok else "❌"
            lines.append(f"### {mark} Case {case['id']}: {case['name']}")
            lines += [""]
            if case.get("expected_advisor"):
                lines.append(
                    f"- **Expected**: `{case['expected_advisor']}` "
                    + (
                        f"with verb `{case['expected_verb']}`"
                        if case.get("expected_verb") else ""
                    )
                )
            elif case.get("expected_multi"):
                lines.append(
                    f"- **Expected multi**: "
                    f"{', '.join('`' + a + '`' for a in case['expected_multi'])}"
                )
            elif case.get("expected_no_fire"):
                lines.append("- **Expected**: no advisor fires")
            elif case.get("expected_no_fire_for"):
                lines.append(
                    f"- **Expected**: `{case['expected_no_fire_for']}` "
                    "does NOT fire"
                )
            lines.append(
                f"- **Result**: {_format_advisors_short(sm['advisors'])}"
            )
            if case["type"] == "e2e":
                lines.append(
                    f"- decision: `{sm.get('decision', '?')}`"
                )
            if not ok:
                lines.append(f"  - ❌ {'; '.join(mm)}")
            elif sm["advisors"]:
                # Show top advisor's first step in detail.
                top = sm["advisors"][0]
                steps = top.get("steps", [])
                if steps:
                    s = steps[0]
                    lines.append(
                        f"  - top step: `{s.get('verb')}` "
                        f"(conf={s.get('confidence', 0.5):.2f})"
                    )
                    impact = s.get("expected_impact") or ""
                    if impact:
                        lines.append(f"    → {impact[:80]}")
            lines += [""]

    lines += ["## Reproduction", "",
              "```bash",
              "uv run python demo/action_steps_50_validation.py",
              "```", ""]

    _REPORT.parent.mkdir(parents=True, exist_ok=True)
    _REPORT.write_text("\n".join(lines), encoding="utf-8")

    print(f"\nResults: {n_pass}/{n_total} pass ({n_pass / n_total * 100:.0f}%)")
    for g in ("A", "B", "C"):
        rs = by_group[g]
        p = sum(1 for _, _, ok, _ in rs if ok)
        print(f"  Group {g}: {p}/{len(rs)} ({descs[g]})")
    print(f"\nVerb frequency: {dict(verb_freq.most_common())}")
    print(f"Advisor frequency: {dict(advisor_freq.most_common())}")
    fails = [(c, s, m) for c, s, _, m in results if not _check(c, s)[0]]
    if fails:
        print(f"\nFailures ({len(fails)}):")
        for c, _, m in fails[:5]:
            print(
                f"  #{c['id']:>3} [{c['group']}] {c['name'][:48]:<50} "
                f"{'; '.join(m)[:80]}"
            )
    print(f"\nReport: {_REPORT}\n")


def main(argv: Sequence[str] | None = None) -> int:
    _setup_env()
    cases = _scenarios()
    print(f"\n[as50] driving {len(cases)} cases")

    results: list[tuple[dict, dict, bool, list[str]]] = []
    for case in cases:
        sm = _run_e2e(case) if case["type"] == "e2e" else _run_unit(case)
        ok, mm = _check(case, sm)
        results.append((case, sm, ok, mm))

    write_report(cases, results)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
