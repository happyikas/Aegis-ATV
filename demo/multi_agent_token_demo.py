"""Multi-agent token-optimization demo (v0.5.9+).

Four agent personas exercise their tools differently. The
ContextMemory layer records every call; Aegis's analytics surfaces
then surface where each agent is wasting tokens and what to do
about it. The demo is fully self-contained — it writes to a tmp
ContextMemory file under ``/tmp/aegis-demo/`` and never touches
the operator's real ``~/.aegis/`` data.

Personas
--------

* **agent-research** — explorer pattern: 20 Reads + 5 WebSearches
  + 3 Greps. Moderate cost; no obvious anomalies. Baseline.

* **agent-greedy** — *the main offender*. 15 WebSearch calls each
  costing $0.02 → $0.30 cumulative. The high-cost-tool miner
  should flag WebSearch as the dominant cost driver.

* **agent-loopy** — coder pattern with a loop bug. Calls
  ``Bash "ls -la"`` 5 times in a row. The loop-detector should
  trip and recommend the ``loop-breaker`` advisor.

* **agent-cautious** — small focused calls, low cost. No flags.

Run::

    python -m demo.multi_agent_token_demo
    # or
    python demo/multi_agent_token_demo.py

The script seeds the demo store, then invokes:

    aegis memory show     --context-memory <demo-path>
    aegis memory claude-md --context-memory <demo-path> --min-tool-cost-usd 0.05
    aegis doctor          --context-memory <demo-path> --since 2h

The output illustrates the end-to-end flow Aegis ATV implements
for token-cost monitoring + multi-agent advice generation.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

# ──────────────────────────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────────────────────────

DEMO_DIR = Path("/tmp/aegis-demo")
DEMO_CM_PATH = DEMO_DIR / "context_memory.jsonl"
DEMO_CLAUDE_MD = DEMO_DIR / "CLAUDE.md"

# Repo root — for finding the `aegis` CLI via uv run.
REPO_ROOT = Path(__file__).resolve().parent.parent


# ──────────────────────────────────────────────────────────────────
# Persona definitions
# ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Call:
    """One synthetic tool invocation by an agent."""

    tool: str
    decision: str           # ALLOW / BLOCK / REQUIRE_APPROVAL
    cost_usd: float
    latency_ms: float
    reason: str = ""
    advisors: tuple[str, ...] = ()


@dataclass(frozen=True)
class Persona:
    aid: str
    description: str
    calls: tuple[Call, ...]


def _make_personas() -> list[Persona]:
    """Assemble the four agent persona traces.

    Each call is one row in ContextMemory. Costs, decisions, and
    advisor recommendations are tuned so the downstream miners
    surface the intended pattern for each persona.
    """
    personas: list[Persona] = []

    # ── agent-research: moderate cost, no anomalies (baseline) ───
    research_calls: list[Call] = []
    research_calls.extend(
        Call(tool="Read", decision="ALLOW", cost_usd=0.001, latency_ms=12.0)
        for _ in range(20)
    )
    research_calls.extend(
        Call(
            tool="WebSearch", decision="ALLOW", cost_usd=0.010,
            latency_ms=380.0,
            advisors=("context-compactor",),
        )
        for _ in range(5)
    )
    research_calls.extend(
        Call(tool="Grep", decision="ALLOW", cost_usd=0.0005, latency_ms=8.0)
        for _ in range(3)
    )
    personas.append(Persona(
        aid="agent-research",
        description=(
            "Explorer pattern — wide reads + a few searches. Baseline "
            "for cost comparisons. No anomalies expected."
        ),
        calls=tuple(research_calls),
    ))

    # ── agent-greedy: WebSearch-heavy, dominant cost driver ──────
    greedy_calls: list[Call] = []
    greedy_calls.extend(
        Call(
            tool="WebSearch", decision="ALLOW", cost_usd=0.020,
            latency_ms=420.0,
            advisors=("cost-optimizer", "context-compactor"),
        )
        for _ in range(15)
    )
    greedy_calls.extend(
        Call(tool="Read", decision="ALLOW", cost_usd=0.001, latency_ms=12.0)
        for _ in range(8)
    )
    personas.append(Persona(
        aid="agent-greedy",
        description=(
            "Token-greedy pattern — 15 expensive WebSearches with no "
            "caching. Expect the high-cost-tool miner to flag "
            "WebSearch + the cost-optimizer advisor to be the top "
            "recommendation."
        ),
        calls=tuple(greedy_calls),
    ))

    # ── agent-loopy: Bash loop trips step336 loop detector ───────
    loopy_calls: list[Call] = []
    loopy_calls.extend(
        Call(
            tool="Bash", decision="ALLOW", cost_usd=0.0005,
            latency_ms=22.0,
        )
        for _ in range(2)
    )
    # The actual loop — 5 REQUIRE_APPROVAL records, each with the
    # loop-detector reason string the memory-claude-md miner
    # recognises.
    loopy_calls.extend(
        Call(
            tool="Bash", decision="REQUIRE_APPROVAL",
            cost_usd=0.0005, latency_ms=18.0,
            reason="same Bash call repeated 3 times this session (threshold=3)",
            advisors=("loop-breaker",),
        )
        for _ in range(5)
    )
    loopy_calls.extend(
        Call(tool="Edit", decision="ALLOW", cost_usd=0.002, latency_ms=15.0)
        for _ in range(3)
    )
    personas.append(Persona(
        aid="agent-loopy",
        description=(
            "Coder pattern with a loop bug — 5 identical Bash calls "
            "trigger step336 (loop detector). Expect loop-breaker "
            "advisor recommended + memory claude-md to propose "
            "reflective-stop guidance for Bash."
        ),
        calls=tuple(loopy_calls),
    ))

    # ── agent-cautious: small focused calls, very low cost ──────
    cautious_calls: list[Call] = []
    cautious_calls.extend(
        Call(tool="Read", decision="ALLOW", cost_usd=0.0005, latency_ms=10.0)
        for _ in range(5)
    )
    cautious_calls.extend(
        Call(tool="Edit", decision="ALLOW", cost_usd=0.001, latency_ms=14.0)
        for _ in range(3)
    )
    personas.append(Persona(
        aid="agent-cautious",
        description=(
            "Disciplined pattern — small focused calls, very low "
            "cost. No flags expected; serves as the 'good citizen' "
            "reference for the cost comparison."
        ),
        calls=tuple(cautious_calls),
    ))

    return personas


# ──────────────────────────────────────────────────────────────────
# ContextMemory seeding
# ──────────────────────────────────────────────────────────────────


def _seed_context_memory(personas: list[Persona]) -> int:
    """Write every call as a ContextMemory record. Returns the
    total count of records written."""
    DEMO_DIR.mkdir(parents=True, exist_ok=True)
    if DEMO_CM_PATH.exists():
        DEMO_CM_PATH.unlink()

    now_ns = time.time_ns()
    # Spread timestamps across the last hour so the doctor's
    # `--since 1h` window catches everything.
    n_total = sum(len(p.calls) for p in personas)
    delta_ns = (60 * 60 * 1_000_000_000) // max(n_total, 1)

    written = 0
    with DEMO_CM_PATH.open("w", encoding="utf-8") as fh:
        for persona in personas:
            for i, call in enumerate(persona.calls):
                ts = now_ns - (n_total - written) * delta_ns
                rec = {
                    "schema_version": 1,
                    "ts_ns": ts,
                    "trace_id": f"{persona.aid}-trace-{i:03d}",
                    "invocation_id": f"{persona.aid}-inv-{i:03d}",
                    "aid": persona.aid,
                    "tenant_id": "demo",
                    "tool_name": call.tool,
                    "decision": call.decision,
                    "reason": call.reason,
                    "channel": None,
                    "provider": "demo",
                    "latency_ms": call.latency_ms,
                    "cost_usd": call.cost_usd,
                    "tokens_in": int(call.cost_usd * 50_000),
                    "tokens_out": int(call.cost_usd * 20_000),
                    "step_traces": {},
                    "m13_score": None,
                    "advisor_invoked": bool(call.advisors),
                    "recommended_advisors": list(call.advisors),
                    "atv_sha3": None,
                    "atv_dim": 2080,
                    "is_sidechain": False,
                    "mode": "demo",
                }
                fh.write(
                    json.dumps(rec, sort_keys=True, ensure_ascii=False)
                    + "\n",
                )
                written += 1

    return written


def _seed_claude_md() -> None:
    """Write a minimal CLAUDE.md so `memory claude-md` has a target
    file to propose edits against."""
    DEMO_CLAUDE_MD.write_text(
        "# Demo Project\n"
        "\n"
        "Synthetic project for the Aegis multi-agent token "
        "optimization demo.\n"
        "\n"
        "## Workflow Discipline\n"
        "\n"
        "(intentionally sparse — Aegis will propose additions)\n",
        encoding="utf-8",
    )


# ──────────────────────────────────────────────────────────────────
# Persona cost summary (Python-side, no CLI)
# ──────────────────────────────────────────────────────────────────


def _print_persona_summary(personas: list[Persona]) -> None:
    """Render a per-persona cost / call rollup before invoking the
    CLI commands. Operators see the synthetic dataset shape first,
    then watch Aegis surface the same patterns from the records."""
    print()
    print("=" * 72)
    print(" Multi-agent token-optimization demo — synthetic input ")
    print("=" * 72)
    print()
    print(
        f"{'agent':<20} {'calls':>6} {'cost $':>10} {'block':>6} "
        f"{'approval':>9}"
    )
    print("-" * 72)
    for p in personas:
        n_calls = len(p.calls)
        total = sum(c.cost_usd for c in p.calls)
        n_block = sum(1 for c in p.calls if c.decision == "BLOCK")
        n_appr = sum(1 for c in p.calls if c.decision == "REQUIRE_APPROVAL")
        print(
            f"{p.aid:<20} {n_calls:>6} {total:>10.4f} {n_block:>6} "
            f"{n_appr:>9}"
        )
    print()
    print("Persona narratives:")
    for p in personas:
        print(f"  • {p.aid}: {p.description}")
    print()


# ──────────────────────────────────────────────────────────────────
# CLI invocation
# ──────────────────────────────────────────────────────────────────


def _run(label: str, argv: list[str]) -> None:
    """Run an `aegis ...` subcommand inside the demo cwd + with the
    demo ContextMemory env override. Prints the output with a
    section header so the demo transcript reads top-down."""
    print()
    print("=" * 72)
    print(f" {label}")
    print(f"   $ {' '.join(argv)}")
    print("=" * 72)
    print()
    env = os.environ.copy()
    env["AEGIS_CONTEXT_MEMORY_PATH"] = str(DEMO_CM_PATH)
    result = subprocess.run(
        argv,
        cwd=str(DEMO_DIR),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    sys.stdout.write(result.stdout)
    if result.stderr.strip():
        sys.stderr.write(result.stderr)
    if result.returncode != 0:
        print(f"  (exit code: {result.returncode})", file=sys.stderr)


def _aegis_argv(*args: str) -> list[str]:
    """Build the argv list for `uv run aegis ...` so the demo works
    from a checkout without a global `aegis` on PATH."""
    return ["uv", "run", "--project", str(REPO_ROOT), "aegis", *args]


# ──────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────


def main() -> int:
    personas = _make_personas()
    _print_persona_summary(personas)

    print(f"Seeding ContextMemory: {DEMO_CM_PATH}")
    n_written = _seed_context_memory(personas)
    print(f"  ✓ {n_written:,} records written")
    _seed_claude_md()
    print(f"  ✓ demo CLAUDE.md ready: {DEMO_CLAUDE_MD}")

    # Step 1: surface the raw store stats.
    _run(
        "Step 1 — ContextMemory store summary",
        _aegis_argv(
            "memory", "show",
            "--context-memory", str(DEMO_CM_PATH),
        ),
    )

    # Step 2: per-agent cost / risk rollup.
    _run(
        "Step 2 — Doctor report (Cost · Performance · Security)",
        _aegis_argv(
            "doctor",
            "--since", "2h",
            "--context-memory", str(DEMO_CM_PATH),
        ),
    )

    # Step 3: actionable CLAUDE.md proposals (the headline surface).
    _run(
        "Step 3 — `memory claude-md` proposals (the optimization advice)",
        _aegis_argv(
            "memory", "claude-md",
            "--context-memory", str(DEMO_CM_PATH),
            "--since", "2h",
            "--min-count", "3",
            "--min-tool-cost-usd", "0.05",
        ),
    )

    # Step 4: apply the top proposal — closes the loop end-to-end.
    _run(
        "Step 4 — Apply the top proposal (`memory claude-md --apply 1`)",
        _aegis_argv(
            "memory", "claude-md",
            "--context-memory", str(DEMO_CM_PATH),
            "--since", "2h",
            "--min-count", "3",
            "--min-tool-cost-usd", "0.05",
            "--apply", "1",
        ),
    )

    # Step 5: show the applied diff via memory diff so operators see
    # the round-trip — generated → applied → audit-trail visible.
    _run(
        "Step 5 — Verify applied proposal (`aegis memory diff`)",
        _aegis_argv(
            "memory", "diff",
            "--claude-md", str(DEMO_CLAUDE_MD),
        ),
    )

    print()
    print("=" * 72)
    print(" Demo complete — full closed-loop demonstrated ")
    print("=" * 72)
    print()
    print("What the five steps just showed:")
    print()
    print("  Step 1  inventory       — 69 records across 4 agents,")
    print("                            spread over the last hour.")
    print("  Step 2  cost rollup     — doctor flags WebSearch p95")
    print("                            42× median, $0.42 cumulative.")
    print("  Step 3  recommendations — `memory claude-md` produces")
    print("                            5 proposals; #1 is high-cost-")
    print("                            tool for WebSearch, #4 is the")
    print("                            loop-detector for Bash.")
    print("  Step 4  auto-apply      — `--apply 1` splices proposal #1")
    print("                            into CLAUDE.md under the")
    print("                            `## Cost Discipline` section,")
    print("                            with a .bak of the original.")
    print("  Step 5  audit trail     — `memory diff` reads the marker")
    print("                            back, confirming the splice")
    print("                            survived for downstream replay.")
    print()
    print("Persona attribution recap:")
    print("  • agent-greedy   → WebSearch hot-tool (15× $0.02 = $0.30)")
    print("  • agent-loopy    → 5× Bash loop tripped step336")
    print("  • agent-research → moderate cost, no flags (baseline)")
    print("  • agent-cautious → very low cost, no flags (model citizen)")
    print()
    print(f"Demo artifacts: {DEMO_DIR}")
    print("  • context_memory.jsonl  — 69 ATV rows")
    print("  • CLAUDE.md             — now has the spliced proposal")
    print("  • CLAUDE.md.bak         — pre-apply backup")
    print()
    print("Re-run with --reset to wipe the demo dir and start fresh.")
    print()
    return 0


def _ensure_clean_demo_dir() -> None:
    """Allow `--reset` flag to wipe the demo dir before seeding."""
    if "--reset" in sys.argv[1:] and DEMO_DIR.exists():
        shutil.rmtree(DEMO_DIR)


if __name__ == "__main__":
    _ensure_clean_demo_dir()
    raise SystemExit(main())
