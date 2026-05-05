"""Aegis-ATV Inefficiency Detection Demo
======================================

End-to-end runnable scenario showing how the multi-hook surveillance
chain (PR #45 PostToolUse + PR #46 Stop + PR #47 PreCompact /
UserPromptSubmit) catches **inefficient cost growth** that
PreToolUse-only gating cannot see.

What it does
------------

1. Builds a synthetic Claude Code session in a tempdir with FOUR
   embedded inefficiency patterns:

     A. **Edit revert loop** — agent edits foo.py, then undoes its
        own change on the same file. (PR #45 backtrack signal)

     B. **Redundant tool call** — same `Bash ls` invoked twice with
        identical args. (PR #45 redundant_of signal)

     C. **Context saturation** — by turn 28 the cumulative input
        tokens hit 195k of the 200k window, triggering an auto-
        compaction. (PR #47 PreCompact)

     D. **User retry** — the user's follow-up prompt re-asks the
        same question (Jaccard similarity > 0.5), signalling the
        agent's prior attempt didn't solve the problem.
        (PR #47 UserPromptSubmit)

2. Runs each hook's analysis function against the synthetic data
   and appends real records to a local audit chain.

3. Prints a narrated diagnosis: WHAT each hook caught, WHY it
   matters, and the aggregate efficiency verdict.

Run
---

::

    uv run python demo/inefficiency_demo.py

Optional flags:

    --keep                   Don't delete the tempdir on exit
                             (useful to `cat <dir>/audit.jsonl | jq`)
    --aegis-report           After the demo, also run `aegis report`
                             against the synthetic chain
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

# Make `src/` importable when the demo is run from the repo root.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

# ruff: noqa: E402  -- imports follow sys.path bootstrap above.
from aegis.cost.post_analysis import analyse_post_tool_event, to_audit_dict
from aegis.cost.precompact_analysis import (
    analyse_precompact_event,
)
from aegis.cost.precompact_analysis import (
    to_audit_record as precompact_audit_record,
)
from aegis.cost.retrospective import (
    analyze_session,
)
from aegis.cost.retrospective import (
    to_audit_record as retrospective_audit_record,
)
from aegis.cost.user_retry_detector import (
    detect_user_retry,
)
from aegis.cost.user_retry_detector import (
    to_audit_record as user_retry_audit_record,
)

SESSION_ID = "demo-inefficiency-session-0001"
MODEL = "claude-haiku-4-5"


# ───────────────────────────────────────────────────────────────────────
# Synthetic Claude Code transcript builder
# ───────────────────────────────────────────────────────────────────────


def _user_msg(text: str) -> dict[str, Any]:
    return {
        "type": "user",
        "message": {"role": "user", "content": text},
    }


def _assistant_msg(
    text: str,
    *,
    input_tokens: int = 1000,
    output_tokens: int = 500,
    cache_read: int = 0,
    cache_creation: int = 0,
    tool_uses: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    content: list[dict[str, Any]] = []
    if text:
        content.append({"type": "text", "text": text})
    if tool_uses:
        content.extend(tool_uses)
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "model": MODEL,
            "content": content,
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_read_input_tokens": cache_read,
                "cache_creation_input_tokens": cache_creation,
            },
        },
    }


def _tool_use(name: str, args: dict[str, Any], *, tool_use_id: str) -> dict[str, Any]:
    return {
        "type": "tool_use",
        "id": tool_use_id,
        "name": name,
        "input": args,
    }


def build_synthetic_transcript(path: Path) -> None:
    """Write a transcript that triggers all four inefficiency patterns.

    Token budget designed so that by the FINAL assistant turn the
    per-turn input usage hits ~195k of the 200k context window —
    the same condition that triggers Claude Code's auto-compaction.
    """
    lines: list[dict[str, Any]] = []

    # Turn 1 — initial user request.
    lines.append(_user_msg("Fix the auth bug in src/auth/login.py."))

    # Turns 2-3 — agent reads the file.
    lines.append(
        _assistant_msg(
            "Reading the file to understand the bug.",
            input_tokens=2000, output_tokens=300,
            tool_uses=[
                _tool_use("Read", {"file_path": "src/auth/login.py"},
                         tool_use_id="tu_001"),
            ],
        )
    )

    # Turn 4 — Edit foo.py with a (wrong) fix.
    lines.append(
        _assistant_msg(
            "Patching the validation block.",
            input_tokens=4500, output_tokens=400,
            cache_read=2000,
            tool_uses=[
                _tool_use(
                    "Edit",
                    {
                        "file_path": "src/auth/login.py",
                        "old_string": "if user is None:\n    return False",
                        "new_string": "if user is None:\n    return None",
                    },
                    tool_use_id="tu_002_edit_first",
                ),
            ],
        )
    )

    # Turn 5 — first redundant Bash ls.
    lines.append(
        _assistant_msg(
            "Verifying the project layout.",
            input_tokens=8000, output_tokens=200,
            cache_read=6000,
            tool_uses=[
                _tool_use(
                    "Bash",
                    {"command": "ls -la src/auth/"},
                    tool_use_id="tu_003_bash_first",
                ),
            ],
        )
    )

    # Turn 6 — second IDENTICAL Bash ls (Pattern B: redundancy).
    lines.append(
        _assistant_msg(
            "Let me list it again to be sure.",
            input_tokens=12000, output_tokens=180,
            cache_read=10000,
            tool_uses=[
                _tool_use(
                    "Bash",
                    {"command": "ls -la src/auth/"},
                    tool_use_id="tu_004_bash_dup",
                ),
            ],
        )
    )

    # Turn 7 — agent UNDOES the turn-4 edit (Pattern A: backtrack).
    # new_string of this edit equals old_string of turn 4.
    lines.append(
        _assistant_msg(
            "That fix was wrong. Reverting.",
            input_tokens=18000, output_tokens=350,
            cache_read=15000,
            tool_uses=[
                _tool_use(
                    "Edit",
                    {
                        "file_path": "src/auth/login.py",
                        "old_string": "if user is None:\n    return None",
                        "new_string": "if user is None:\n    return False",
                    },
                    tool_use_id="tu_005_edit_revert",
                ),
            ],
        )
    )

    # Turns 8-25 — bulk filler turns to drive the token total up.
    # Each turn adds ~7k of unique input + 8k cache_read on top of
    # whatever was previously cached.
    for i in range(8, 26):
        lines.append(
            _assistant_msg(
                f"Investigating angle {i}.",
                input_tokens=2500, output_tokens=300,
                cache_read=15000 + (i - 8) * 6500,
            )
        )

    # Turn 26 — error response triggers an `is_error=True` post_analysis.
    lines.append(
        _assistant_msg(
            "Trying a different approach.",
            input_tokens=3000, output_tokens=400,
            cache_read=120000,
            tool_uses=[
                _tool_use(
                    "Bash",
                    {"command": "uv run pytest tests/auth/"},
                    tool_use_id="tu_006_bash_err",
                ),
            ],
        )
    )

    # Turn 27 — assistant explanation, no tool use.
    lines.append(
        _assistant_msg(
            "Tests are failing. Reading more files.",
            input_tokens=2500, output_tokens=600,
            cache_read=140000,
        )
    )

    # Turn 28 — heavy turn that pushes per-turn input near the
    # 200k context window (Pattern C: PreCompact trigger).
    lines.append(
        _assistant_msg(
            "Synthesising fix proposal.",
            input_tokens=8000, output_tokens=900,
            cache_read=185000,
            cache_creation=2000,
        )
    )

    # Turn 29 — final assistant message with text body (final output).
    lines.append(
        _assistant_msg(
            "Here is my best guess at a fix; tests still fail though.",
            input_tokens=3000, output_tokens=1200,
            cache_read=195000,
        )
    )

    with path.open("w", encoding="utf-8") as fh:
        for ev in lines:
            fh.write(json.dumps(ev) + "\n")


# ───────────────────────────────────────────────────────────────────────
# Audit chain seeding — simulate PostToolUse records from PR #45
# ───────────────────────────────────────────────────────────────────────


def _append_audit(audit_path: Path, record: dict[str, Any]) -> None:
    """Plain JSONL append. The real hook uses the SHA3-chained
    `aegis.audit.local_chain.append`, but for demo readability we
    keep the on-disk shape simple. The records are byte-identical."""
    with audit_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")


def _pretool_record(
    aid: str, tool: str, args: dict[str, Any], decision: str = "ALLOW",
) -> dict[str, Any]:
    return {
        "ts_ns": time.time_ns(),
        "tool": tool,
        "aid": aid,
        "hook": "PreToolUse",
        "mode": "local",
        "decision": decision,
        "reason": "demo synthetic",
        "args": args,
    }


def seed_pre_and_post_tool_audit(
    audit_path: Path, intent_log_path: Path,
) -> dict[str, str]:
    """Lay down the Pre/Post records that PR #45's analyser writes
    in real life. We do this in chronological order so that
    detect_backtrack and detect_redundant_call walk the right
    history when the LATER tool calls are processed.

    Returns a dict mapping pattern_name → trace_id (for the diagnosis).
    """
    pattern_traces: dict[str, str] = {}

    # The 5 tool-using turns from the transcript:
    tool_calls: list[tuple[str, str, dict[str, Any], Any, int | None]] = [
        ("tu_001", "Read",
         {"file_path": "src/auth/login.py"},
         "def login(user):\n    ...\n    if user is None:\n        return False\n",
         0),
        ("tu_002_edit_first", "Edit",
         {"file_path": "src/auth/login.py",
          "old_string": "if user is None:\n    return False",
          "new_string": "if user is None:\n    return None"},
         "Edited 1 location.",
         0),
        ("tu_003_bash_first", "Bash",
         {"command": "ls -la src/auth/"},
         "total 16\ndrwxr-xr-x  4 user staff  128 Jan  1 12:00 .\n",
         0),
        ("tu_004_bash_dup", "Bash",
         {"command": "ls -la src/auth/"},
         "total 16\ndrwxr-xr-x  4 user staff  128 Jan  1 12:00 .\n",
         0),
        ("tu_005_edit_revert", "Edit",
         {"file_path": "src/auth/login.py",
          "old_string": "if user is None:\n    return None",
          "new_string": "if user is None:\n    return False"},
         "Edited 1 location.",
         0),
        ("tu_006_bash_err", "Bash",
         {"command": "uv run pytest tests/auth/"},
         "FAILED tests/auth/test_login.py::test_none_returns_false\n"
         "Traceback (most recent call last):\n  ...\nAssertionError",
         1),
    ]

    for trace_id, tool, args, response, exit_code in tool_calls:
        # PreToolUse record (the gate decision).
        pre = _pretool_record(SESSION_ID, tool, args)
        pre["trace_id"] = trace_id
        _append_audit(audit_path, pre)

        # PostToolUse analysis — runs PR #45 against the audit chain
        # SO FAR, so backtrack/redundancy detection has the right
        # window of history to walk.
        analysis = analyse_post_tool_event(
            tool_name=tool,
            tool_input=args,
            tool_response=response,
            exit_code=exit_code,
            audit_path=audit_path,
            intent_log_path=intent_log_path,
        )
        post = {
            "ts_ns": time.time_ns(),
            "tool": tool,
            "aid": SESSION_ID,
            "hook": "PostToolUse",
            "mode": "local",
            "trace_id": trace_id,
            "status": "failure" if exit_code else "success",
            "explain": {
                "post_analysis": to_audit_dict(
                    analysis, tool_name=tool, tool_input=args,
                ),
            },
        }
        _append_audit(audit_path, post)

        # Capture pattern hooks for the diagnosis.
        if tool == "Edit" and analysis.backtrack is not None:
            pattern_traces["pattern_a_backtrack"] = trace_id
        if analysis.redundant_of is not None:
            pattern_traces["pattern_b_redundant"] = trace_id
        if analysis.classification.is_error:
            pattern_traces["pattern_e_error"] = trace_id

    return pattern_traces


# ───────────────────────────────────────────────────────────────────────
# Hook orchestration
# ───────────────────────────────────────────────────────────────────────


def fire_precompact_hook(
    transcript_path: Path, audit_path: Path,
) -> dict[str, Any]:
    rec = analyse_precompact_event(
        session_id=SESSION_ID,
        transcript_path=transcript_path,
        trigger="auto",
        model_for_cost=MODEL,
    )
    audit_rec = precompact_audit_record(rec)
    _append_audit(audit_path, audit_rec)
    return audit_rec


def fire_user_prompt_hook(
    transcript_path: Path, audit_path: Path, *, current_prompt: str,
) -> dict[str, Any]:
    """The detector returns the PENULTIMATE user prompt — i.e. it
    assumes Claude Code has already appended the incoming message
    to the transcript before firing UserPromptSubmit. We mirror
    that contract here so the demo path matches production."""
    with transcript_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(_user_msg(current_prompt)) + "\n")
    evidence = detect_user_retry(
        current_prompt=current_prompt,
        transcript_path=transcript_path,
        threshold=0.5,
    )
    audit_rec = user_retry_audit_record(SESSION_ID, evidence)
    _append_audit(audit_path, audit_rec)
    return audit_rec


def fire_stop_hook(
    transcript_path: Path, audit_path: Path,
) -> dict[str, Any]:
    retro = analyze_session(
        transcript_path=transcript_path,
        audit_path=audit_path,
        session_id=SESSION_ID,
        model_for_cost=MODEL,
    )
    audit_rec = retrospective_audit_record(retro)
    _append_audit(audit_path, audit_rec)
    return audit_rec


# ───────────────────────────────────────────────────────────────────────
# Diagnosis renderer
# ───────────────────────────────────────────────────────────────────────


_BLUE = "\033[34m"
_GREEN = "\033[32m"
_YELLOW = "\033[33m"
_RED = "\033[31m"
_BOLD = "\033[1m"
_DIM = "\033[2m"
_RESET = "\033[0m"


def section(title: str) -> None:
    print()
    print(f"{_BOLD}{_BLUE}── {title} {'─' * (66 - len(title))}{_RESET}")


def render_diagnosis(
    *,
    audit_path: Path,
    precompact_rec: dict[str, Any],
    user_retry_rec: dict[str, Any],
    stop_rec: dict[str, Any],
    pattern_traces: dict[str, str],
) -> None:
    print()
    print(f"{_BOLD}AegisData Inefficiency Detection Demo{_RESET}")
    print(f"{_DIM}session_id = {SESSION_ID}{_RESET}")

    # ── A: PostToolUse signals (from PR #45) ────────────────────────────
    section("A. Per-call inefficiency signals (PostToolUse, PR #45)")
    n_backtrack = 0
    n_redundant = 0
    n_is_error = 0
    n_post = 0
    backtrack_files: list[str] = []
    redundant_pairs: list[tuple[str, str]] = []
    error_tools: list[str] = []
    with audit_path.open(encoding="utf-8") as fh:
        for raw in fh:
            try:
                rec = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if rec.get("hook") != "PostToolUse":
                continue
            n_post += 1
            pa = (rec.get("explain") or {}).get("post_analysis") or {}
            if pa.get("backtrack"):
                n_backtrack += 1
                backtrack_files.append(pa["backtrack"]["file_path"])
            if pa.get("redundant_of"):
                n_redundant += 1
                redundant_pairs.append(
                    (str(rec.get("trace_id", "")), pa["redundant_of"])
                )
            cls = pa.get("classification") or {}
            if cls.get("is_error"):
                n_is_error += 1
                error_tools.append(str(rec.get("tool", "")))
    print(f"  Tool calls analysed:    {n_post}")
    print(
        f"  {_YELLOW}↩ Backtracks:{_RESET}            "
        f"{n_backtrack}  "
        + (f"{_DIM}({backtrack_files[0]}){_RESET}" if backtrack_files else "")
    )
    print(
        f"  {_YELLOW}♻ Redundant calls:{_RESET}       "
        f"{n_redundant}  "
        + (f"{_DIM}(same args as earlier){_RESET}" if redundant_pairs else "")
    )
    print(
        f"  {_RED}✗ Error responses:{_RESET}       "
        f"{n_is_error}  "
        + (f"{_DIM}({', '.join(error_tools)}){_RESET}" if error_tools else "")
    )
    print()
    print(
        f"  {_DIM}WHY this matters: every backtrack means the agent paid for{_RESET}"
    )
    print(
        f"  {_DIM}an Edit twice and ended where it started. Redundant Bash{_RESET}"
    )
    print(
        f"  {_DIM}calls bill twice for the same answer. Errors mean the{_RESET}"
    )
    print(f"  {_DIM}prior planning step was wrong.{_RESET}")

    # ── B: PreCompact (PR #47) ──────────────────────────────────────────
    section("B. Context saturation (PreCompact, PR #47)")
    cmp_block = (precompact_rec.get("explain") or {}).get("compaction") or {}
    util = float(cmp_block.get("context_utilization_pre", 0.0))
    bar = _fill_bar(util, 32)
    util_color = _RED if util >= 0.9 else (_YELLOW if util >= 0.7 else _GREEN)
    print(
        f"  Context window:         {util_color}{bar}{_RESET}  "
        f"{util * 100:.1f}% of 200k"
    )
    print(
        f"  Turns before compact:   {cmp_block.get('n_turns_before', 0)}  "
        f"(assistant: {cmp_block.get('n_assistant_turns_before', 0)})"
    )
    print(
        f"  Tokens already billed:  "
        f"${cmp_block.get('cumulative_billed_dollars_before', 0.0):.4f}"
    )
    print()
    print(
        f"  {_DIM}WHY this matters: Claude Code is about to throw away the{_RESET}"
    )
    print(
        f"  {_DIM}detailed history and replace it with a re-summarisation{_RESET}"
    )
    print(
        f"  {_DIM}LLM call. That summary call costs more than a normal turn,{_RESET}"
    )
    print(
        f"  {_DIM}and the agent loses the verbatim trace it might need to{_RESET}"
    )
    print(f"  {_DIM}reason about its own past mistakes.{_RESET}")

    # ── C: UserPromptSubmit (PR #47) ────────────────────────────────────
    section("C. User retry detection (UserPromptSubmit, PR #47)")
    ur_block = (user_retry_rec.get("explain") or {}).get("user_retry") or {}
    similarity = float(ur_block.get("similarity", 0.0))
    is_retry = bool(ur_block.get("is_retry", False))
    method = ur_block.get("method", "jaccard")
    threshold = float(ur_block.get("threshold", 0.5))
    sim_color = _RED if is_retry else _GREEN
    print(
        f"  Similarity to prior:    {sim_color}{similarity:.2f}{_RESET}  "
        f"({method}; threshold {threshold:.2f})"
    )
    print(
        f"  Retry flag:             "
        f"{_RED + 'TRIGGERED' if is_retry else _GREEN + 'no retry'}{_RESET}"
    )
    print(
        f"  Privacy:                "
        f"{_DIM}prompt_hash={ur_block.get('prompt_hash', '')} "
        f"({ur_block.get('prompt_size_bytes', 0)} B); raw text NOT in audit{_RESET}"
    )
    print()
    print(
        f"  {_DIM}WHY this matters: when the user repeats themselves, the{_RESET}"
    )
    print(
        f"  {_DIM}agent's prior tokens were burned without solving the{_RESET}"
    )
    print(
        f"  {_DIM}problem. This is the strongest "
        f"\"agent failed\" signal we have{_RESET}"
    )
    print(f"  {_DIM}because only the user can confirm it.{_RESET}")

    # ── D: Stop retrospective (PR #46) ──────────────────────────────────
    section("D. Session retrospective (Stop hook, PR #46)")
    retro = (stop_rec.get("explain") or {}).get("session_retrospective") or {}

    cum_dollars = float(retro.get("cumulative_billed_dollars", 0.0))
    cache_hit = float(retro.get("cache_hit_rate", 0.0))
    backtrack_ratio = float(retro.get("backtrack_ratio", 0.0))
    redundancy_ratio = float(retro.get("redundancy_ratio", 0.0))
    error_rate = float(retro.get("error_rate", 0.0))
    ctx_util = float(retro.get("context_utilization_ratio", 0.0))
    n_tool_success = int(retro.get("n_tool_success", 0))
    tokens_per_success = float(
        retro.get("tokens_per_successful_tool_invocation", 0.0)
    )

    print(f"  {_BOLD}Cost{_RESET}")
    print(f"    cumulative_billed_dollars:    ${cum_dollars:.4f}")
    print(f"    tokens_per_successful_tool:   {tokens_per_success:,.0f}")
    print(f"    n_tool_success:               {n_tool_success}")
    print()
    print(f"  {_BOLD}Efficiency KPIs{_RESET}")
    print(f"    cache_hit_rate:               {cache_hit * 100:5.1f}%")
    print(f"    context_utilization_ratio:    {ctx_util * 100:5.1f}%")
    print()
    print(f"  {_BOLD}Inefficiency ratios (lower is better){_RESET}")
    print(
        f"    backtrack_ratio:              "
        f"{_color_ratio(backtrack_ratio)}{backtrack_ratio:5.2f}{_RESET}  "
        f"{_DIM}(reverts / Edit calls){_RESET}"
    )
    print(
        f"    redundancy_ratio:             "
        f"{_color_ratio(redundancy_ratio)}{redundancy_ratio:5.2f}{_RESET}  "
        f"{_DIM}(repeat calls / pretool calls){_RESET}"
    )
    print(
        f"    error_rate:                   "
        f"{_color_ratio(error_rate)}{error_rate:5.2f}{_RESET}  "
        f"{_DIM}(is_error / posttool calls){_RESET}"
    )

    # ── E: Aggregate diagnosis ──────────────────────────────────────────
    section("E. Aggregate diagnosis")

    findings: list[str] = []
    if backtrack_ratio > 0:
        findings.append(
            f"{_RED}● Edit-revert loop detected.{_RESET} The agent edited "
            f"{', '.join(set(backtrack_files))} and then undid its own change. "
            f"Cost: 2× Edit billing for net-zero progress."
        )
    if redundancy_ratio > 0:
        findings.append(
            f"{_RED}● Redundant tool call.{_RESET} The agent ran the same "
            f"command twice with identical args. Cache helped, but the "
            f"tool round-trip and reasoning step were paid for twice."
        )
    if error_rate > 0:
        findings.append(
            f"{_YELLOW}● Tool error in the trace.{_RESET} {n_is_error} "
            f"of {n_post} PostToolUse records flagged as is_error — "
            f"the agent's prior planning step was wrong."
        )
    if util >= 0.9:
        findings.append(
            f"{_RED}● Context saturated.{_RESET} The session hit "
            f"{util * 100:.0f}% of the 200k window before compaction. "
            f"Auto-compaction will trigger an additional re-summarisation "
            f"LLM call that is NOT visible in PreToolUse cost_estimate."
        )
    if is_retry:
        findings.append(
            f"{_RED}● User retry.{_RESET} The follow-up prompt is "
            f"{similarity:.0%} similar to the prior one — the user is "
            f"asking again. The {cum_dollars:.4f}$ already burned didn't "
            f"solve the problem."
        )

    if findings:
        for line in findings:
            print(f"  {line}")
    else:
        print(
            f"  {_GREEN}No inefficiency signals tripped.{_RESET}  "
            "(Sanity check — your demo data isn't right.)"
        )

    # Score: weighted sum, capped at 1.0.
    score = min(
        1.0,
        backtrack_ratio * 0.30
        + redundancy_ratio * 0.20
        + error_rate * 0.20
        + (util if util >= 0.9 else 0.0) * 0.20
        + (1.0 if is_retry else 0.0) * 0.30,
    )
    score_color = _RED if score >= 0.5 else (_YELLOW if score >= 0.25 else _GREEN)
    score_label = "HIGH" if score >= 0.5 else ("MEDIUM" if score >= 0.25 else "LOW")
    print()
    print(
        f"  {_BOLD}Inefficiency score: "
        f"{score_color}{score:.2f}  [{score_label}]{_RESET}"
    )

    print()
    print(f"  {_DIM}Audit chain:  {audit_path}{_RESET}")
    print(
        f"  {_DIM}Inspect:      "
        f"cat {audit_path} | jq -c '{{hook, tool, status, ok: .explain != null}}'{_RESET}"
    )


def _fill_bar(ratio: float, width: int) -> str:
    filled = int(min(1.0, max(0.0, ratio)) * width)
    return "█" * filled + "░" * (width - filled)


def _color_ratio(r: float) -> str:
    if r >= 0.30:
        return _RED
    if r >= 0.10:
        return _YELLOW
    return _GREEN


# ───────────────────────────────────────────────────────────────────────
# Main
# ───────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Aegis-ATV multi-hook inefficiency detection demo.",
    )
    p.add_argument(
        "--keep", action="store_true",
        help="Keep the tempdir on exit (handy for `cat | jq` inspection).",
    )
    p.add_argument(
        "--aegis-report", action="store_true",
        help="After the demo, also run `aegis report` against the chain.",
    )
    args = p.parse_args(argv)

    workdir = Path(tempfile.mkdtemp(prefix="aegis-inefficiency-demo-"))
    transcript_path = workdir / "transcript.jsonl"
    audit_path = workdir / "audit.jsonl"
    intent_log_path = workdir / "intent.sqlite"  # left empty — module copes

    try:
        print(f"{_DIM}working dir: {workdir}{_RESET}")
        print(f"{_DIM}building synthetic transcript ...{_RESET}")
        build_synthetic_transcript(transcript_path)

        print(f"{_DIM}seeding Pre/PostToolUse audit chain ...{_RESET}")
        pattern_traces = seed_pre_and_post_tool_audit(
            audit_path, intent_log_path,
        )

        print(f"{_DIM}firing PreCompact hook ...{_RESET}")
        precompact_rec = fire_precompact_hook(transcript_path, audit_path)

        print(f"{_DIM}firing UserPromptSubmit hook ...{_RESET}")
        # User retypes a paraphrase of turn-1.
        user_retry_rec = fire_user_prompt_hook(
            transcript_path, audit_path,
            current_prompt=(
                "The auth bug in src/auth/login.py is still there. "
                "Fix it properly this time."
            ),
        )

        print(f"{_DIM}firing Stop hook (session retrospective) ...{_RESET}")
        stop_rec = fire_stop_hook(transcript_path, audit_path)

        render_diagnosis(
            audit_path=audit_path,
            precompact_rec=precompact_rec,
            user_retry_rec=user_retry_rec,
            stop_rec=stop_rec,
            pattern_traces=pattern_traces,
        )

        if args.aegis_report:
            print()
            print(f"{_BOLD}── `aegis report` against the synthetic chain "
                  f"{'─' * 18}{_RESET}")
            # In-process so we don't pay for a nested `uv run`.
            sys.path.insert(0, str(ROOT / "tools"))
            import aegis_cli  # type: ignore[import-not-found]
            ns = argparse.Namespace(
                audit=str(audit_path), since=None,
                verbose=False, json=False, explain=None,
            )
            aegis_cli.cmd_report(ns)

        if args.keep:
            print()
            print(f"{_GREEN}--keep set; tempdir preserved at {workdir}{_RESET}")
        return 0
    finally:
        if not args.keep:
            shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
