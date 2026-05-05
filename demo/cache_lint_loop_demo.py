"""Aegis-ATV Closed-Loop Cache-Lint Demo
==========================================

Walks through the full feedback loop end-to-end:

::

    ┌─────────────┐    ┌──────────┐    ┌────────────┐    ┌──────────────┐
    │ before.jsonl│ →  │cache_lint│ →  │ user fixes │ →  │ after.jsonl  │
    │ (bad)       │    │  finds 2 │    │  CLAUDE.md │    │ (improved)   │
    └─────────────┘    │  breaks  │    └────────────┘    └──────┬───────┘
                       └──────────┘                              │
                                                                 ▼
                              ┌──────────────────────────────────────────┐
                              │ compare_transcripts(before, after)        │
                              │   • cache_hit_rate Δ                      │
                              │   • token savings realised                │
                              │   • realisation rate (realised ÷ proj)    │
                              │   • breaks resolved / persisting / new    │
                              └──────────────────────────────────────────┘

What this demo proves
---------------------

We synthesise two transcripts that model the same agent task:

* **before.jsonl** — system prompt has a date + UUID at the top,
  and a new tool gets registered mid-session at turn 3 → cache
  break + 2 static-lint findings.

* **after.jsonl** — date and UUID moved below cache_control,
  filesystem MCP tool registered at session start → cache stays
  high throughout, no breaks.

The demo runs cache_lint on both and prints the comparison
report with realised vs projected numbers. This is the same
shape as ``aegis cache-lint --transcript after.jsonl
--compare-with before.jsonl``.

Run
---

::

    uv run python demo/cache_lint_loop_demo.py
    uv run python demo/cache_lint_loop_demo.py --keep
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

# ruff: noqa: E402 -- imports follow sys.path bootstrap above.
from aegis.performance.cache_lint import analyze_transcript
from aegis.performance.cache_lint_loop import (
    ComparisonReport,
    compare_transcripts,
    project_fix,
)

SESSION_ID = "demo-cache-loop-0001"


# ──────────────────────────────────────────────────────────────────────
# Synthetic transcripts
# ──────────────────────────────────────────────────────────────────────


def _user_msg(text: str) -> dict[str, Any]:
    return {"type": "user", "message": {"role": "user", "content": text}}


def _assistant(
    *,
    input_tokens: int,
    cache_read: int = 0,
    cache_creation: int = 0,
    output_tokens: int = 200,
    tool_uses: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    content: list[dict[str, Any]] = []
    if tool_uses:
        content.extend(tool_uses)
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": content,
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_read_input_tokens": cache_read,
                "cache_creation_input_tokens": cache_creation,
            },
        },
    }


def _write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")


def build_before_transcript(path: Path) -> None:
    """Bad session — break at turn 3 (new tool registered)."""
    records = [
        _user_msg("Help me audit the auth module."),
        _assistant(
            input_tokens=4500, cache_creation=4500,
            tool_uses=[{"type": "tool_use", "name": "Read", "id": "t1"}],
        ),
        _user_msg("Continue."),
        _assistant(
            input_tokens=300, cache_read=4500,
            tool_uses=[{"type": "tool_use", "name": "Read", "id": "t2"}],
        ),
        _user_msg("Now check the imports."),
        _assistant(
            input_tokens=400, cache_read=5000,
            tool_uses=[{"type": "tool_use", "name": "Grep", "id": "t3"}],
        ),
        # ── BREAK: new MCP tool registered, prefix invalidated ──
        _user_msg("Use the filesystem MCP server."),
        _assistant(
            input_tokens=5800, cache_read=600, cache_creation=5500,
            tool_uses=[
                {"type": "tool_use", "name": "filesystem_mcp", "id": "t4"},
            ],
        ),
        _user_msg("Continue."),
        _assistant(
            input_tokens=300, cache_read=6300,
            tool_uses=[
                {"type": "tool_use", "name": "filesystem_mcp", "id": "t5"},
            ],
        ),
    ]
    _write_jsonl(path, records)


def build_after_transcript(path: Path) -> None:
    """Good session — same task, but the user heeded the advisor:
    filesystem MCP tool registered at session start, so its name
    is in the cached prefix from turn 0 → no break at turn 3."""
    records = [
        _user_msg("Help me audit the auth module."),
        _assistant(
            # Bigger initial cache_creation because the tool catalog
            # now includes filesystem_mcp from the start.
            input_tokens=5300, cache_creation=5300,
            tool_uses=[{"type": "tool_use", "name": "Read", "id": "t1"}],
        ),
        _user_msg("Continue."),
        _assistant(
            input_tokens=300, cache_read=5300,
            tool_uses=[{"type": "tool_use", "name": "Read", "id": "t2"}],
        ),
        _user_msg("Now check the imports."),
        _assistant(
            input_tokens=400, cache_read=5800,
            tool_uses=[{"type": "tool_use", "name": "Grep", "id": "t3"}],
        ),
        # ── No break — filesystem_mcp was already in the prefix ──
        _user_msg("Use the filesystem MCP server."),
        _assistant(
            input_tokens=400, cache_read=6300,
            tool_uses=[
                {"type": "tool_use", "name": "filesystem_mcp", "id": "t4"},
            ],
        ),
        _user_msg("Continue."),
        _assistant(
            input_tokens=300, cache_read=7000,
            tool_uses=[
                {"type": "tool_use", "name": "filesystem_mcp", "id": "t5"},
            ],
        ),
    ]
    _write_jsonl(path, records)


# Two system-prompt versions — same text, anti-patterns moved.
SYS_PROMPT_BEFORE = """\
You are a careful coding assistant.
Today is 2026-05-04. Session ID: a1b2c3d4-e5f6-7890-abcd-ef1234567890.

Tools: Read, Grep, Bash, Edit, Write, filesystem_mcp.
Always use the tools when appropriate. Never commit secrets.
"""

SYS_PROMPT_AFTER = """\
You are a careful coding assistant.

Tools: Read, Grep, Bash, Edit, Write, filesystem_mcp.
Always use the tools when appropriate. Never commit secrets.

(In production: per-request date and session_id live ABOVE the user
message, not in this stable system prompt — so they no longer match
the static-lint anti-pattern catalogue.)
"""


# ──────────────────────────────────────────────────────────────────────
# Renderer
# ──────────────────────────────────────────────────────────────────────


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


def render_loop(cmp: ComparisonReport) -> None:
    print()
    print(f"{_BOLD}AegisData Cache-Lint Closed-Loop Verification{_RESET}")
    print(f"{_DIM}session = {SESSION_ID}{_RESET}")

    # Stage 1 — baseline
    section("1. Baseline (before fix)")
    print(
        f"  observed cache_hit_rate:    "
        f"{_RED}{cmp.before.observed_cache_hit_rate * 100:5.1f}%{_RESET}"
    )
    print(f"  cache breaks:               {len(cmp.before.breaks)}")
    print(f"  static-lint findings:       {len(cmp.before.static_findings)}")

    # Stage 2 — projection from baseline
    section("2. Advisor projection (if all fixes applied)")
    p = cmp.projected
    print(
        f"  projected cache_hit_rate:   "
        f"{_GREEN}{p.projected_cache_hit_rate * 100:5.1f}%{_RESET}"
    )
    print(f"  projected token savings:    "
          f"{p.projected_token_savings:,} tokens / session")
    print(f"  breaks to address:          {p.breaks_to_address}")
    print(f"  static findings to address: {p.static_findings_to_address}  "
          f"({_RED}{p.error_severity_findings} error{_RESET}, "
          f"{_YELLOW}{p.warning_severity_findings} warning{_RESET})")

    # Stage 3 — user applies fixes & runs new session (simulated)
    section("3. After fix — measured outcome")
    sign = "+" if cmp.cache_hit_rate_delta >= 0 else ""
    delta_color = _GREEN if cmp.cache_hit_rate_delta >= 0 else _RED
    print(
        f"  observed cache_hit_rate:    "
        f"{_GREEN}{cmp.after.observed_cache_hit_rate * 100:5.1f}%{_RESET}  "
        f"({delta_color}{sign}{cmp.cache_hit_rate_delta * 100:.1f} pp{_RESET})"
    )
    print(
        f"  tokens recovered:           "
        f"{_GREEN}{cmp.token_savings_realised:+,} tokens / session"
        f"{_RESET}"
    )
    realisation_color = (
        _GREEN if cmp.realisation_rate >= 0.80
        else (_YELLOW if cmp.realisation_rate >= 0.50 else _RED)
    )
    print(
        f"  realisation rate:           "
        f"{realisation_color}{cmp.realisation_rate * 100:.0f}%{_RESET}  "
        f"({_DIM}realised ÷ projected{_RESET})"
    )

    # Stage 4 — break-level breakdown
    section("4. Per-break verdict")
    if cmp.breaks_resolved:
        print(f"  {_GREEN}✓ Resolved {len(cmp.breaks_resolved)} break(s):{_RESET}")
        for b in cmp.breaks_resolved:
            print(
                f"    {_GREEN}↳{_RESET} turn {b.turn_idx}  "
                f"({b.attribution[:60]}…)" if len(b.attribution) > 60
                else
                f"    {_GREEN}↳{_RESET} turn {b.turn_idx}  ({b.attribution})"
            )
    if cmp.breaks_persisting:
        print(
            f"  {_YELLOW}~ Persisting {len(cmp.breaks_persisting)} "
            f"break(s){_RESET} (recommendation not applied):"
        )
        for b in cmp.breaks_persisting:
            print(f"    turn {b.turn_idx}  ({b.attribution[:60]})")
    if cmp.new_breaks:
        print(f"  {_RED}⚠ NEW {len(cmp.new_breaks)} break(s) (regression):{_RESET}")
        for b in cmp.new_breaks:
            print(f"    turn {b.turn_idx}  ({b.attribution[:60]})")
    if not (cmp.breaks_resolved or cmp.breaks_persisting or cmp.new_breaks):
        print(f"  {_GREEN}no breaks in either session{_RESET}")

    # Stage 5 — static findings breakdown
    section("5. Static-lint diff")
    if cmp.static_findings_resolved:
        print(f"  {_GREEN}✓ Removed{_RESET} "
              f"{len(cmp.static_findings_resolved)} anti-pattern(s):")
        for f in cmp.static_findings_resolved:
            print(
                f"    {_GREEN}↳{_RESET} {f.pattern_name}  "
                f"{f.matched_excerpt!r}"
            )
    if cmp.static_findings_persisting:
        print(
            f"  {_YELLOW}~ Persisting{_RESET} "
            f"{len(cmp.static_findings_persisting)} anti-pattern(s):"
        )
        for f in cmp.static_findings_persisting:
            print(f"    {f.pattern_name}  {f.matched_excerpt!r}")
    if cmp.new_static_findings:
        print(
            f"  {_RED}⚠ NEW{_RESET} "
            f"{len(cmp.new_static_findings)} anti-pattern(s) (regression):"
        )
        for f in cmp.new_static_findings:
            print(f"    {f.pattern_name}  {f.matched_excerpt!r}")
    if not (cmp.static_findings_resolved
            or cmp.static_findings_persisting
            or cmp.new_static_findings):
        print(f"  {_GREEN}no static-lint findings in either prompt{_RESET}")

    # Stage 6 — verdict
    section("6. Closed-loop verdict")
    if cmp.realisation_rate >= 0.80:
        print(
            f"  {_GREEN}✓ Closed loop confirmed.{_RESET}  Advisor's "
            "projection materialised — the recommendations addressed the "
            "root cause."
        )
    elif cmp.realisation_rate >= 0.50:
        print(
            f"  {_YELLOW}~ Partial recovery.{_RESET}  Some "
            "recommendations took effect but breaks persist; review the "
            "persisting list above."
        )
    elif cmp.token_savings_realised < 0:
        print(
            f"  {_RED}⚠ Regression.{_RESET}  The follow-up session is "
            "WORSE than the baseline. Inspect the new breaks list."
        )
    else:
        print(
            f"  {_RED}× No realisation.{_RESET}  Recommendations were "
            "not applied or did not address the root cause."
        )
    print()
    print(
        f"  {_DIM}For real sessions, run:{_RESET}"
    )
    print(
        f"  {_DIM}  aegis cache-lint --transcript after.jsonl "
        f"\\\\{_RESET}"
    )
    print(
        f"  {_DIM}    --compare-with before.jsonl{_RESET}"
    )


# ──────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Aegis-ATV closed-loop cache-lint demo.",
    )
    p.add_argument(
        "--keep", action="store_true",
        help="Keep the tempdir on exit (handy for jq inspection).",
    )
    args = p.parse_args(argv)

    workdir = Path(tempfile.mkdtemp(prefix="aegis-cache-loop-demo-"))
    before_path = workdir / "before.jsonl"
    after_path = workdir / "after.jsonl"

    try:
        print(f"{_DIM}working dir: {workdir}{_RESET}")
        print(f"{_DIM}building synthetic before / after transcripts...{_RESET}")
        build_before_transcript(before_path)
        build_after_transcript(after_path)

        print(f"{_DIM}running cache_lint on both + projecting + diffing...{_RESET}")

        # Show projection for the BEFORE report on its own first.
        before_only = analyze_transcript(
            before_path, system_prompt=SYS_PROMPT_BEFORE,
        )
        proj = project_fix(before_only)
        print()
        print(
            f"{_DIM}baseline projection (from BEFORE alone): "
            f"+{proj.projected_token_savings:,} tokens recoverable, "
            f"hit_rate target {proj.projected_cache_hit_rate * 100:.1f}%{_RESET}"
        )

        cmp = compare_transcripts(
            before_path=before_path,
            after_path=after_path,
            before_system_prompt=SYS_PROMPT_BEFORE,
            after_system_prompt=SYS_PROMPT_AFTER,
        )
        render_loop(cmp)

        if args.keep:
            print()
            print(f"{_GREEN}--keep set; tempdir preserved at {workdir}{_RESET}")
            print(
                f"{_DIM}try:  uv run python tools/aegis_cli.py cache-lint "
                f"\\\\{_RESET}"
            )
            print(
                f"{_DIM}        --transcript {after_path} "
                f"--compare-with {before_path} --json | jq .{_RESET}"
            )
        return 0
    finally:
        if not args.keep:
            shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
