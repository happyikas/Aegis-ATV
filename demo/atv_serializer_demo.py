"""Aegis-ATV → sLLM Serializer Diagnostic Demo (PR-β)
========================================================

The patent intent (option (b)) is that the 2080-D ATV alone is
sufficient sLLM context. This demo answers the empirical question:
"is it really?"

It builds a realistic ATVInput, runs both **strict** (ATV-only) and
**enriched** (ATV + ATVInput supplementation) modes of
``atv_to_prompt``, and prints them side by side along with:

* the explicit GAP list — bands the strict mode could not represent
* the byte delta between the two modes
* a verdict line that scopes the eventual schema-extension PR-α

Run
---

::

    uv run python demo/atv_serializer_demo.py
    uv run python demo/atv_serializer_demo.py --json   # for piping
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

# ruff: noqa: E402  -- imports follow sys.path bootstrap above.
from aegis.atv.builder import build_atv
from aegis.atv.serializer import atv_to_prompt, diagnose
from aegis.schema import (
    AttentionSummary,
    ATVHeader,
    ATVInput,
    CostEfficiencyMetrics,
)

_GREEN = "\033[32m"
_YELLOW = "\033[33m"
_RED = "\033[31m"
_BLUE = "\033[34m"
_BOLD = "\033[1m"
_DIM = "\033[2m"
_RESET = "\033[0m"


def section(title: str) -> None:
    print()
    print(f"{_BOLD}{_BLUE}── {title} {'─' * (66 - len(title))}{_RESET}")


def build_realistic_input() -> ATVInput:
    """A representative mid-session ATVInput with realistic signals."""
    return ATVInput(
        header=ATVHeader(
            trace_id="t" * 32, span_id="s" * 16,
            tenant_id="alice-org", aid="agent-007",
            timestamp_ns=1_775_000_000_000_000_000,
            tier_profile="T2",
            model_hash="claude-haiku-4-5",
        ),
        tool_name="Bash",
        tool_args_json=(
            '{"command": "uv run pytest tests/auth/", '
            '"description": "Run auth test suite"}'
        ),
        plan_text=(
            "I need to fix the validation bug in src/auth/login.py "
            "that we identified earlier. The plan is: "
            "(1) Read the file to confirm the bug location, "
            "(2) Edit the affected block to use the correct guard, "
            "(3) run pytest tests/auth/ to verify the fix doesn't "
            "regress neighbouring tests. Avoid touching session "
            "handling code; that's a separate concern."
        ),
        cost_estimate=CostEfficiencyMetrics(
            input_token_count=2_400, output_token_count=1_200,
            cumulative_tokens=12_480, cumulative_dollars=0.034,
            cache_hit_rate=0.85, context_utilization_ratio=0.42,
            task_progress_score=0.45,
            tokens_per_successful_tool_invocation=1_580,
            budget_burn_rate=0.012,
        ),
        novelty={"composite_novelty": 0.32},
        recent_actions=[
            {"tool": "Read", "result": "success"},
            {"tool": "Grep", "result": "success"},
            {"tool": "Edit", "result": "success"},
        ],
        attention_summary=AttentionSummary(
            n_tokens=2048,
            entropy_normalized=0.42,
            top_k_concentration=0.78,
            sink_presence=0.15,
            recency_bias=0.55,
            effective_rank=0.18,
        ),
    )


def render(args: argparse.Namespace) -> int:
    inp = build_realistic_input()
    atv = build_atv(inp)

    if args.json:
        import json

        d = diagnose(atv, inp)
        print(json.dumps(d, indent=2))
        return 0

    print(f"{_BOLD}Aegis ATV → sLLM Serializer Diagnostic{_RESET}")
    print(
        f"{_DIM}option (b.pragmatic) feasibility check — "
        f"is ATV-2080 alone sufficient for sLLM context?{_RESET}"
    )

    # ── 1. Build the two outputs ──
    strict = atv_to_prompt(atv, mode="strict")
    enriched = atv_to_prompt(atv, inp, mode="enriched")

    # ── 2. Strict mode (the patent claim) ──
    section("1. STRICT mode — ATV-only (the patent claim)")
    print(f"{_DIM}{strict.text}{_RESET}")
    print()
    print(
        f"  size: {len(strict)} chars / "
        f"{strict.line_count()} lines"
    )

    # ── 3. Enriched mode (the production fallback) ──
    section("2. ENRICHED mode — ATV + ATVInput supplementation")
    print(f"{_DIM}{enriched.text}{_RESET}")
    print()
    print(
        f"  size: {len(enriched)} chars / "
        f"{enriched.line_count()} lines"
    )
    print(
        f"  delta vs strict: "
        f"{_YELLOW}+{len(enriched) - len(strict)} bytes / "
        f"+{enriched.line_count() - strict.line_count()} lines{_RESET}"
    )

    # ── 4. The gap report ──
    section("3. Gap report — what STRICT mode could not represent")
    if not strict.gaps:
        print(f"  {_GREEN}no gaps detected{_RESET}")
    else:
        for i, g in enumerate(strict.gaps, 1):
            print(f"  {_RED}{i}.{_RESET} {g}")

    # ── 5. Verdict + PR-α scoping ──
    section("4. Verdict")
    n_gaps = len(strict.gaps)
    delta = len(enriched) - len(strict)
    if n_gaps == 0 and delta <= 100:
        verdict = (
            f"{_GREEN}✓ ATV-2080 alone is sufficient for sLLM context.{_RESET}"
        )
    elif n_gaps <= 2:
        verdict = (
            f"{_YELLOW}~ ATV-2080 covers most of the context but "
            f"{n_gaps} gap(s) remain.{_RESET}"
        )
    else:
        verdict = (
            f"{_RED}✗ ATV-2080 cannot stand alone as sLLM context — "
            f"{n_gaps} gap(s) require schema extension (PR-α).{_RESET}"
        )
    print(f"  {verdict}")
    print(
        f"  {_DIM}empirical delta: enriched mode adds "
        f"{delta} bytes ({(delta / max(len(strict), 1)) * 100:.1f}% "
        f"more){_RESET}"
    )

    # ── 6. PR-α scope recommendation ──
    section("5. PR-α scope recommendation")
    print("  Bands to add to ATV schema v5 (in priority order):")
    print()
    print(
        f"  1. {_BOLD}plan_text_embedding{_RESET} (768-D) — currently "
        "absent. Carries"
    )
    print("     the agent's intent for the in-flight tool call.")
    print()
    print(
        f"  2. {_BOLD}recent_history_summary_embedding{_RESET} (768-D) "
        "— currently"
    )
    print(
        "     a 640-D hash band (action_history) with no semantic "
        "content."
    )
    print(
        "     Semantic compression of last N turns is what sLLM "
        "needs to reason"
    )
    print("     about the agent's trajectory.")
    print()
    print(
        f"  3. {_BOLD}task_intent_categorical{_RESET} (16-D) — categorical"
    )
    print(
        "     embedding of {debug, explore, edit, test, refactor, …} "
        "task type."
    )
    print()
    print(
        f"  Reclaim candidates (for non-extension layout): "
        f"{_DIM}HW band (200-D zero in T2), inter_agent_graph (128-D "
        f"hash, only useful for multi-agent fleets){_RESET}"
    )

    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="ATV→sLLM serializer diagnostic demo (PR-β).",
    )
    p.add_argument(
        "--json", action="store_true",
        help="Emit the diagnose() dict as JSON (for jq / scripting)",
    )
    return render(p.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
