"""Aegis-ATV Prompt-Cache Lint Demo
====================================

Build a synthetic Claude Code session that embeds three classic
prompt-cache-breakage anti-patterns, run it through the merged
``aegis.performance.cache_lint`` analyser, and print a narrated
diagnosis showing what broke and how to fix it.

Patterns embedded
-----------------

1. **Tool catalog change mid-session** — a new MCP tool first appears
   at turn 4, invalidating the prefix that included the tool list.
2. **Dynamic content injection** — turn 7 has a dramatically larger
   ``input_tokens`` than its neighbours, simulating a date / UUID
   being added above the cache_control marker.
3. **System-prompt anti-patterns** (static lint) — the system prompt
   itself contains a date, a time, and a UUID in its stable region.

Run
---

    uv run python demo/cache_lint_demo.py
    uv run python demo/cache_lint_demo.py --keep
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
from aegis.performance.cache_lint import (
    CacheLintReport,
    analyze_transcript,
)

SESSION_ID = "demo-cache-lint-session-0001"


# ──────────────────────────────────────────────────────────────────────
# Synthetic transcript
# ──────────────────────────────────────────────────────────────────────


def _user_msg(text: str) -> dict[str, Any]:
    return {"type": "user", "message": {"role": "user", "content": text}}


def _assistant_turn(
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


def build_synthetic_transcript(path: Path) -> None:
    """Produce a 9-turn transcript with two distinct cache breaks."""
    records: list[dict[str, Any]] = []

    # Turn 0 — first call, no cache yet (fresh prefix).
    records.append(_user_msg("Help me audit a Python module."))
    records.append(_assistant_turn(
        input_tokens=4500, cache_creation=4500,
        tool_uses=[{"type": "tool_use", "name": "Read", "id": "tu_01"}],
    ))

    # Turn 1 — high cache hit (~94 %).
    records.append(_user_msg("Continue."))
    records.append(_assistant_turn(
        input_tokens=300, cache_read=4500,
        tool_uses=[{"type": "tool_use", "name": "Read", "id": "tu_02"}],
    ))

    # Turn 2 — still high cache (~94 %).
    records.append(_user_msg("Now check the imports."))
    records.append(_assistant_turn(
        input_tokens=400, cache_read=5000,
        tool_uses=[{"type": "tool_use", "name": "Grep", "id": "tu_03"}],
    ))

    # ── BREAK 1 ──
    # Turn 3 — a NEW tool name (filesystem_mcp) appears for the first
    # time. Tool catalog hash changes → entire prefix invalidated.
    records.append(_user_msg(
        "Use the filesystem MCP server to list everything."
    ))
    records.append(_assistant_turn(
        input_tokens=5800, cache_read=600,
        cache_creation=5500,
        tool_uses=[
            {"type": "tool_use", "name": "filesystem_mcp", "id": "tu_04"},
        ],
    ))

    # Turn 4 — cache rebuilds.
    records.append(_user_msg("Good. Continue."))
    records.append(_assistant_turn(
        input_tokens=300, cache_read=6300,
        tool_uses=[
            {"type": "tool_use", "name": "filesystem_mcp", "id": "tu_05"},
        ],
    ))

    # Turn 5 — high cache.
    records.append(_user_msg("Run the tests now."))
    records.append(_assistant_turn(
        input_tokens=500, cache_read=6700,
        tool_uses=[{"type": "tool_use", "name": "Bash", "id": "tu_06"}],
    ))

    # ── BREAK 2 ──
    # Turn 6 — input_tokens jumps 4 + ×, simulating a large dynamic
    # block (e.g., long date-stamped header) being injected above the
    # cache_control marker.
    records.append(_user_msg("Compare with last week's output."))
    records.append(_assistant_turn(
        input_tokens=3500, cache_read=2200,
        cache_creation=2000,
        tool_uses=[{"type": "tool_use", "name": "Read", "id": "tu_07"}],
    ))

    # Turn 7 — recovers.
    records.append(_user_msg("Summarise."))
    records.append(_assistant_turn(
        input_tokens=400, cache_read=8500,
    ))

    with path.open("w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")


SYS_PROMPT_WITH_ANTIPATTERNS = """\
You are a careful coding assistant.
Today is 2026-05-04 and the local time is 14:32:11.
Your session identifier is a1b2c3d4-e5f6-7890-abcd-ef1234567890.
Generated at 1714834331000 ms epoch.

---

Tools available: Read, Grep, Bash, Edit, Write.

Always use the tools when appropriate. Never commit secrets.
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


def _bar(eff: float, width: int = 20) -> str:
    filled = int(min(1.0, max(0.0, eff)) * width)
    return "█" * filled + "░" * (width - filled)


def _eff_color(eff: float) -> str:
    if eff >= 0.70:
        return _GREEN
    if eff >= 0.30:
        return _YELLOW
    return _RED


def section(title: str) -> None:
    print()
    print(f"{_BOLD}{_BLUE}── {title} {'─' * (66 - len(title))}{_RESET}")


def render(report: CacheLintReport) -> None:
    print()
    print(f"{_BOLD}AegisData Prompt-Cache Lint Demo{_RESET}")
    print(f"{_DIM}session = {SESSION_ID}{_RESET}")
    print(
        f"{_DIM}analysed {report.n_turns} assistant turns, "
        f"{len(report.static_findings)} static-lint findings{_RESET}"
    )

    # Per-turn efficiency strip
    section("A. Per-turn cache efficiency (transcript walk)")
    for t in report.turns:
        bar = _bar(t.efficiency)
        col = _eff_color(t.efficiency)
        print(
            f"  turn {t.turn_idx}:  {col}{bar}{_RESET}  "
            f"{t.efficiency * 100:5.1f}%   "
            f"{_DIM}cache_read={t.cache_read:>6,} / "
            f"total_input={t.total_input:,}{_RESET}"
        )

    # Cache breaks
    section("B. Cache breaks detected")
    if not report.breaks:
        print(f"  {_GREEN}✓ no breaks{_RESET}")
    else:
        for b in report.breaks:
            print(
                f"  {_RED}⚠ turn {b.turn_idx}{_RESET}  "
                f"efficiency {b.before_efficiency*100:.0f}% → "
                f"{b.after_efficiency*100:.0f}%  "
                f"({_RED}-{b.drop_pp:.1f} pp{_RESET}, "
                f"~{b.tokens_lost_estimate:,} tokens lost)"
            )
            print(f"    {_BOLD}cause:{_RESET}      {b.attribution}")
            print(f"    {_BOLD}suggestion:{_RESET} {b.suggestion}")
            print()

    # Static findings
    section("C. Static lint (system prompt anti-patterns)")
    if not report.static_findings:
        print(f"  {_GREEN}✓ no anti-patterns{_RESET}")
    else:
        for f in report.static_findings:
            sev_marker = (
                f"{_RED}✗{_RESET}" if f.severity == "error"
                else (f"{_YELLOW}⚠{_RESET}"
                      if f.severity == "warning"
                      else f"{_DIM}·{_RESET}")
            )
            print(
                f"  {sev_marker} char {f.position:>4}  "
                f"[{_BOLD}{f.pattern_name}{_RESET}]  "
                f"{f.matched_excerpt!r}"
            )
            print(f"    {_DIM}→ {f.suggestion}{_RESET}")

    # Aggregate diagnosis
    section("D. Aggregate diagnosis")
    obs = report.observed_cache_hit_rate * 100
    th = report.theoretical_max_cache_hit_rate * 100
    obs_color = _eff_color(report.observed_cache_hit_rate)
    th_color = _eff_color(report.theoretical_max_cache_hit_rate)
    print(
        f"  observed cache_hit_rate:        "
        f"{obs_color}{obs:5.1f}%{_RESET}"
    )
    print(
        f"  theoretical max (no breaks):    "
        f"{th_color}{th:5.1f}%{_RESET}"
    )
    if report.potential_token_savings > 0:
        print(
            f"  {_BOLD}potential token savings:        "
            f"{_GREEN}~{report.potential_token_savings:,} tokens"
            f"{_RESET}{_BOLD} per session{_RESET}"
        )

    section("E. What to do")
    if report.breaks:
        print(
            f"  1. Move all dynamic content (dates, IDs, etc.) {_BOLD}below{_RESET}"
        )
        print("     a `cache_control` marker so it sits in the un-cached tail.")
        print(
            "  2. Register every MCP server / tool at session start; "
            "do not"
        )
        print("     introduce new tools mid-session.")
    if report.static_findings:
        n_err = sum(1 for f in report.static_findings if f.severity == "error")
        if n_err:
            print(
                f"  3. {_RED}{n_err} ERROR-level finding(s){_RESET}: random "
                f"per-request"
            )
            print("     identifiers in the stable prefix. Fix immediately —")
            print("     they cap the achievable hit rate at near-zero.")
    print()
    print(
        f"  {_DIM}This is the same surface as `aegis cache-lint --transcript "
        f"<path>`.{_RESET}"
    )
    print(
        f"  {_DIM}For machine-readable output, add `--json`.{_RESET}"
    )


# ──────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Aegis-ATV prompt-cache lint walkthrough.",
    )
    p.add_argument(
        "--keep", action="store_true",
        help="Keep the tempdir on exit (handy for `aegis cache-lint --json`)",
    )
    args = p.parse_args(argv)

    workdir = Path(tempfile.mkdtemp(prefix="aegis-cache-lint-demo-"))
    transcript_path = workdir / "transcript.jsonl"
    sysprompt_path = workdir / "system_prompt.txt"

    try:
        print(f"{_DIM}working dir: {workdir}{_RESET}")
        print(f"{_DIM}building synthetic transcript ...{_RESET}")
        build_synthetic_transcript(transcript_path)
        sysprompt_path.write_text(SYS_PROMPT_WITH_ANTIPATTERNS, encoding="utf-8")

        print(f"{_DIM}running cache_lint analyser ...{_RESET}")
        report = analyze_transcript(
            transcript_path,
            system_prompt=SYS_PROMPT_WITH_ANTIPATTERNS,
        )
        render(report)

        if args.keep:
            print()
            print(f"{_GREEN}--keep set; tempdir preserved at {workdir}{_RESET}")
            print(
                f"{_DIM}try:  uv run python tools/aegis_cli.py cache-lint "
                f"--transcript {transcript_path} --json | jq .{_RESET}"
            )
        return 0
    finally:
        if not args.keep:
            shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
