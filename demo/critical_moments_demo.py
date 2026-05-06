#!/usr/bin/env python3
"""Drive 10 critical-moment scenarios through the LIVE PreToolUse +
PostToolUse hooks and print a verification table.

Each scenario targets a specific gate signal or retrospective category
so the operator can confirm end-to-end that the advisor fires when it
should. Audit goes to ``/tmp/critical-moments-audit.jsonl`` to keep the
real ``~/.aegis/audit.jsonl`` clean.

Run::

    uv run python demo/critical_moments_demo.py
"""

from __future__ import annotations

import io
import json
import os
import sys
import textwrap
from collections.abc import Sequence
from pathlib import Path
from typing import Any

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "tools"))
sys.path.insert(0, str(_REPO / "tools" / "hooks"))

_AUDIT = Path("/tmp/critical-moments-audit.jsonl")


# ──────────────────────────────────────────────────────────────────────
# Destructive literals are constructed at runtime so the demo source
# itself doesn't trigger the firewall when the file is edited under a
# hook (this is the same trick advisor_demo.py uses).
# ──────────────────────────────────────────────────────────────────────


def _git_force_push() -> str:
    return " ".join(["git", "push", "--force", "origin", "main"])


def _sql_drop_table() -> str:
    return " ".join(["DROP", "TABLE", "users", "WHERE", "1=1"])


def _rm_rf_path() -> str:
    return " ".join(["rm", "-rf", "/tmp/imaginary-target"])


def _kubectl_delete() -> str:
    return " ".join(["kubectl", "delete", "namespace", "production"])


def _privileged_docker() -> str:
    return " ".join(["docker", "run", "--privileged", "alpine"])


# ──────────────────────────────────────────────────────────────────────
# 10 scenarios
# ──────────────────────────────────────────────────────────────────────


def _scenarios() -> list[dict[str, Any]]:
    """Each scenario: id, name, expected gate signal / advisor names,
    and (pre, post) event pair."""
    s: list[dict[str, Any]] = []

    s.append({
        "id": 1,
        "name": "baseline routine Read",
        "expect_gate": "no critical signals",
        "expect_advisors": [],
        "pre": {
            "hook_event_name": "PreToolUse",
            "session_id": "cm-demo",
            "invocation_id": "cm-001",
            "tool_name": "Read",
            "tool_input": {"file_path": "/tmp/foo.md"},
        },
        "post_status": "success",
    })

    s.append({
        "id": 2,
        "name": "git force-push → BLOCK",
        "expect_gate": "verdict=BLOCK",
        "expect_advisors": ["security-reviewer"],
        "pre": {
            "hook_event_name": "PreToolUse",
            "session_id": "cm-demo",
            "invocation_id": "cm-002",
            "tool_name": "Bash",
            "tool_input": {"command": _git_force_push()},
        },
        "post_status": "failure",
    })

    s.append({
        "id": 3,
        "name": "rm -rf path → BLOCK",
        "expect_gate": "verdict=BLOCK",
        "expect_advisors": ["security-reviewer"],
        "pre": {
            "hook_event_name": "PreToolUse",
            "session_id": "cm-demo",
            "invocation_id": "cm-003",
            "tool_name": "Bash",
            "tool_input": {"command": _rm_rf_path()},
        },
        "post_status": "failure",
    })

    s.append({
        "id": 4,
        "name": "SQL destructive → BLOCK",
        "expect_gate": "verdict=BLOCK",
        "expect_advisors": ["security-reviewer"],
        "pre": {
            "hook_event_name": "PreToolUse",
            "session_id": "cm-demo",
            "invocation_id": "cm-004",
            "tool_name": "Bash",
            "tool_input": {"command": _sql_drop_table()},
        },
        "post_status": "failure",
    })

    s.append({
        "id": 5,
        "name": "kubectl delete → BLOCK or REQ_APPROVAL",
        # Firewall is conservative — kubectl delete on a production
        # namespace is treated as destructive enough to BLOCK.
        "expect_gate": "verdict=*",
        "expect_advisors": ["permission-escalator"],
        "pre": {
            "hook_event_name": "PreToolUse",
            "session_id": "cm-demo",
            "invocation_id": "cm-005",
            "tool_name": "Bash",
            "tool_input": {"command": _kubectl_delete()},
        },
        "post_status": "failure",
    })

    s.append({
        "id": 6,
        "name": "privileged docker → BLOCK / REQ_APPROVAL",
        "expect_gate": "verdict=*",
        "expect_advisors": ["security-reviewer", "permission-escalator"],
        "pre": {
            "hook_event_name": "PreToolUse",
            "session_id": "cm-demo",
            "invocation_id": "cm-006",
            "tool_name": "Bash",
            "tool_input": {"command": _privileged_docker()},
        },
        "post_status": "failure",
    })

    # Loop pattern: same call 3× — third call fires step336 →
    # REQUIRE_APPROVAL → loop-breaker.
    for i in range(7, 10):
        s.append({
            "id": i,
            "name": (
                f"loop call #{i - 6} (3rd triggers step336)"
                if i == 9 else f"loop call #{i - 6}"
            ),
            # On the 3rd call the firewall escalates to
            # REQUIRE_APPROVAL via step336 → gate fires under signal #1
            # (verdict=*) before the dedicated loop signal can match.
            # Either gate trigger is correct; both produce the same
            # downstream advisor (loop-breaker).
            "expect_gate": (
                "verdict=*" if i == 9 else "no critical signals"
            ),
            "expect_advisors": (
                ["loop-breaker"] if i == 9 else []
            ),
            "pre": {
                "hook_event_name": "PreToolUse",
                "session_id": "cm-demo",
                "invocation_id": f"cm-00{i}",
                "tool_name": "Bash",
                "tool_input": {"command": "echo loop-target"},
            },
            "post_status": "success",
        })

    s.append({
        "id": 10,
        "name": "ALWAYS=1 + actual failure → missed_signal",
        "expect_gate": "AEGIS_ADVISOR_ALWAYS=1",
        "expect_advisors": [],
        "expect_retrospective": "missed_signal",
        "force_always": True,  # bypass the gate so the advisor predicts ALLOW
        "pre": {
            "hook_event_name": "PreToolUse",
            "session_id": "cm-demo",
            "invocation_id": "cm-010",
            "tool_name": "Bash",
            "tool_input": {"command": "echo build-script"},
        },
        "post_status": "failure",  # exit_code != 0 → retrospective fires
    })

    return s


# ──────────────────────────────────────────────────────────────────────
# Hook drivers
# ──────────────────────────────────────────────────────────────────────


def _drive_pretool(event: dict[str, Any], *, force_always: bool = False) -> None:
    import aegis_local_hook
    pre_in = io.StringIO(json.dumps(event))
    pre_out = io.StringIO()
    saved = sys.stderr
    sys.stderr = io.StringIO()
    saved_always = aegis_local_hook.ADVISOR_ALWAYS
    if force_always:
        aegis_local_hook.ADVISOR_ALWAYS = True
    try:
        aegis_local_hook.handle_pretool(pre_in, pre_out)
    finally:
        sys.stderr = saved
        aegis_local_hook.ADVISOR_ALWAYS = saved_always


def _drive_posttool(scenario: dict[str, Any]) -> None:
    import post_tool

    pre = scenario["pre"]
    post_event = {
        "hook_event_name": "PostToolUse",
        "session_id": pre["session_id"],
        "invocation_id": pre["invocation_id"],
        "tool_name": pre["tool_name"],
        "tool_input": pre["tool_input"],
        "tool_response": {"output": "ok"},
        "exit_code": 0 if scenario["post_status"] == "success" else 1,
    }
    pi = io.StringIO(json.dumps(post_event))
    po = io.StringIO()
    saved = sys.stderr
    sys.stderr = io.StringIO()
    try:
        post_tool.handle_posttool(pi, po)
    finally:
        sys.stderr = saved


def setup_environment() -> None:
    if _AUDIT.exists():
        _AUDIT.unlink()
    os.environ["AEGIS_LOCAL_AUDIT"] = str(_AUDIT)
    os.environ["AEGIS_ADVISOR_ENABLED"] = "1"
    os.environ["AEGIS_ADVISOR_PROVIDER"] = "dummy"
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


# ──────────────────────────────────────────────────────────────────────
# Output
# ──────────────────────────────────────────────────────────────────────


def _read_records() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not _AUDIT.is_file():
        return out
    for line in _AUDIT.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(r, dict):
            out.append(r)
    return out


def _summarise(records: list[dict[str, Any]],
               scenario: dict[str, Any]) -> dict[str, Any]:
    inv = scenario["pre"]["invocation_id"]
    pre = next(
        (r for r in records
         if r.get("invocation_id") == inv
         and r.get("hook") != "PostToolUse"),
        None,
    )
    post = next(
        (r for r in records
         if r.get("invocation_id") == inv
         and r.get("hook") == "PostToolUse"),
        None,
    )
    explain = (pre or {}).get("explain") or {}
    gate = explain.get("advisor_gate") or {}
    advice = explain.get("action_advice") or {}
    advisors = [
        f'{r.get("advisor", "?")}'
        for r in (advice.get("recommended_advisors") or [])
        if isinstance(r, dict)
    ]
    retro = ((post or {}).get("explain") or {}).get(
        "retrospective_advice"
    ) or {}
    return {
        "decision": (pre or {}).get("decision", "(no pre)"),
        "gate_invoked": bool(gate.get("invoked")),
        "gate_reason": gate.get("reason", ""),
        "advisors": advisors,
        "retrospective": retro.get("accuracy", "(none)"),
    }


_GREEN = "\033[32m"
_RED = "\033[31m"
_YELLOW = "\033[33m"
_DIM = "\033[2m"
_BOLD = "\033[1m"
_RESET = "\033[0m"


def _ok(actual: str, expected: str) -> str:
    if expected == "verdict=*":  # accept any verdict-driven fire
        return _GREEN + "✓" + _RESET if actual.startswith("verdict=") else _RED + "✗" + _RESET
    return _GREEN + "✓" + _RESET if expected in actual else _RED + "✗" + _RESET


def _advisor_match(actual: list[str], expected: list[str]) -> str:
    if not expected:
        return _GREEN + "✓" + _RESET if not actual else _YELLOW + "+" + _RESET
    matched = [a for a in expected if a in actual]
    if matched == expected:
        return _GREEN + "✓" + _RESET
    if matched:
        return _YELLOW + "~" + _RESET
    return _RED + "✗" + _RESET


def render_table(scenarios: list[dict[str, Any]],
                 records: list[dict[str, Any]]) -> str:
    lines: list[str] = [
        _BOLD + "10 Critical-Moment Verification" + _RESET,
        f"  audit: {_AUDIT}",
        f"  records: {len(records)}",
        "",
    ]
    header = (
        f'  {"#":<3} {"scenario":<40} {"verdict":<18} '
        f'{"gate?":<5} {"signal":<26} {"advisors":<28} {"retro":<14}'
    )
    lines.append(_BOLD + header + _RESET)
    lines.append(_DIM + "  " + "─" * 144 + _RESET)

    for sc in scenarios:
        sm = _summarise(records, sc)

        gate_match = _ok(sm["gate_reason"] or "no critical signals",
                         sc["expect_gate"])
        adv_match = _advisor_match(sm["advisors"],
                                   sc.get("expect_advisors", []))

        retro = sm["retrospective"]
        if "expect_retrospective" in sc:
            retro_match = (
                _GREEN + retro + _RESET
                if retro == sc["expect_retrospective"]
                else _RED + retro + _RESET
            )
        else:
            retro_match = _DIM + retro + _RESET

        gate_str = (
            _GREEN + "fire" + _RESET if sm["gate_invoked"]
            else _DIM + "skip" + _RESET
        )

        adv_str = ", ".join(sm["advisors"][:3]) or "—"
        if len(sm["advisors"]) > 3:
            adv_str += f" (+{len(sm['advisors']) - 3})"
        adv_str = adv_str + " " + adv_match

        signal_str = (sm["gate_reason"] or "—")[:25] + " " + gate_match
        scname_str = sc["name"][:39]

        lines.append(
            f'  {sc["id"]:<3} {scname_str:<40} '
            f'{sm["decision"]:<18} {gate_str:<14} {signal_str:<35} '
            f'{adv_str:<37} {retro_match}'
        )

    return "\n".join(lines)


def render_full_audit_section(scenarios: list[dict[str, Any]],
                              records: list[dict[str, Any]]) -> str:
    """For each scenario, dump the full action_advice JSON so the user
    can verify the heuristic / sLLM produced sensible content."""
    out = ["", _BOLD + "Per-scenario advice details" + _RESET, ""]
    for sc in scenarios:
        inv = sc["pre"]["invocation_id"]
        pre = next(
            (r for r in records
             if r.get("invocation_id") == inv
             and r.get("hook") != "PostToolUse"),
            None,
        )
        if pre is None:
            continue
        explain = pre.get("explain") or {}
        advice = explain.get("action_advice")
        if not advice:
            continue
        out.append(
            _BOLD + f'  [{sc["id"]}] {sc["name"]}' + _RESET
        )
        for r in (advice.get("recommended_advisors") or []):
            out.append(
                f'    [{r.get("priority"):<6}] '
                f'{r.get("advisor"):<22} '
                f'{r.get("action", "")[:80]}'
            )
            if r.get("reasoning"):
                out.append(
                    _DIM + textwrap.fill(
                        f'      reason: {r["reasoning"]}',
                        width=120, subsequent_indent="              ",
                    ) + _RESET
                )
        out.append("")
    return "\n".join(out)


def _legend() -> str:
    return (
        "\n  Legend: "
        + _GREEN + "✓" + _RESET + " expected match | "
        + _YELLOW + "~" + _RESET + " partial match | "
        + _YELLOW + "+" + _RESET + " unexpected fire | "
        + _RED + "✗" + _RESET + " mismatch\n"
    )


# ──────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────


def main(argv: Sequence[str] | None = None) -> int:
    setup_environment()
    scenarios = _scenarios()

    print(f"\n[critical-moments] driving {len(scenarios)} scenarios → {_AUDIT}")
    for sc in scenarios:
        _drive_pretool(sc["pre"], force_always=sc.get("force_always", False))
        _drive_posttool(sc)

    records = _read_records()
    print(_legend())
    print(render_table(scenarios, records))
    print(render_full_audit_section(scenarios, records))

    n_fired = sum(
        1 for r in records
        if r.get("hook") != "PostToolUse"
        and (r.get("explain") or {}).get("advisor_gate", {}).get("invoked")
    )
    n_pre = sum(1 for r in records if r.get("hook") != "PostToolUse")
    print(
        f"\nGate fired on {n_fired}/{n_pre} scenarios. "
        f"Audit at {_AUDIT}.\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
