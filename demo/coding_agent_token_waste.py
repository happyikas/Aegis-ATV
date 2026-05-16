"""Coding-AI token-waste detection demo (v0.5.9+).

Five Claude-Code-shaped agents attempt **the same coding task** —
"fix the bug in src/aegis/firewall/step336_loop.py" — with five
different efficiency profiles. ``agent-baseline`` is the gold
standard; the other four exhibit a specific token-waste anti-
pattern each. The demo lets Aegis ATV measure exactly how much
each anti-pattern costs vs the baseline, then surface the
proposals that would close the gap.

Anti-patterns covered (each tied to one or more Aegis miners):

* **Re-reader** — re-Reads the same file 8× during one session
  (no cache awareness). Token bloat → high-cost-tool miner picks
  up Read as a top spender + loop-detector fires on repeated
  Read calls.

* **Overscope** — Reads 5000-line files in full when only ~50
  lines are needed. Hits cost-optimizer + context-compactor
  advisor recommendations.

* **Loop-stuck** — runs the same pytest invocation 6× without
  varying inputs after each failure. step336 loop-detector
  fires; loop-breaker advisor recommended.

* **Toolbox-confused** — reaches for WebSearch when Grep over
  the local codebase would answer the same question. Wrong-tool
  surfaces via high-cost-tool (WebSearch) + cost-optimizer
  advisor.

The demo computes the waste ratio per agent vs baseline and
prints it BEFORE Aegis runs — so operators can compare what the
synthetic data should reveal against what Aegis actually
surfaces.

Run::

    python demo/coding_agent_token_waste.py --reset
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

# ──────────────────────────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────────────────────────

DEMO_DIR = Path("/tmp/aegis-coding-waste")
DEMO_CM_PATH = DEMO_DIR / "context_memory.jsonl"
DEMO_CLAUDE_MD = DEMO_DIR / "CLAUDE.md"
REPO_ROOT = Path(__file__).resolve().parent.parent


# Sonnet 3.5 approximate pricing (USD / 1M tokens, 2026-05 rates):
#   input:  $3.00 / 1M  → $0.000003 / token
#   output: $15.00 / 1M → $0.000015 / token
USD_PER_INPUT_TOK = 3.0 / 1_000_000
USD_PER_OUTPUT_TOK = 15.0 / 1_000_000


def _cost(tokens_in: int, tokens_out: int) -> float:
    return tokens_in * USD_PER_INPUT_TOK + tokens_out * USD_PER_OUTPUT_TOK


# ──────────────────────────────────────────────────────────────────
# Tool-call shape
# ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Call:
    """One coding-AI tool invocation."""

    tool: str
    tokens_in: int
    tokens_out: int
    decision: str = "ALLOW"
    latency_ms: float = 50.0
    reason: str = ""
    advisors: tuple[str, ...] = ()
    note: str = ""   # human-readable purpose; not stored, just rendering

    @property
    def cost_usd(self) -> float:
        return _cost(self.tokens_in, self.tokens_out)


@dataclass(frozen=True)
class Agent:
    aid: str
    headline: str          # one-line pattern description
    failure_mode: str      # what's wrong with this pattern
    calls: tuple[Call, ...] = field(default_factory=tuple)


# ──────────────────────────────────────────────────────────────────
# Agent populations — each runs the SAME task differently
# ──────────────────────────────────────────────────────────────────


def _agents() -> list[Agent]:
    agents: list[Agent] = []

    # ============================================================
    # agent-baseline — the gold standard.
    # ============================================================
    baseline = [
        # Locate the bug with grep first (cheap).
        Call("Grep", 800, 120, note="grep for 'loop' in step336"),
        # Read just the relevant section with offset/limit.
        Call("Read", 1500, 600, note="Read step336_loop.py:40-80"),
        # One Edit fix.
        Call("Edit", 1200, 200, note="Edit: fix off-by-one in counter"),
        # Run the focused test.
        Call("Bash", 800, 250, latency_ms=820.0,
             note="pytest tests/unit/test_step336.py -k count"),
        # Confirm green.
        Call("Bash", 400, 80, latency_ms=12.0,
             note="quick git diff"),
    ]
    agents.append(Agent(
        aid="agent-baseline",
        headline="Locate → read scoped → edit → focused test",
        failure_mode="(none — this is what good looks like)",
        calls=tuple(baseline),
    ))

    # ============================================================
    # agent-rereader — re-Reads the same file 8 times in a session.
    # The agent forgot it already loaded the file. Each Read pulls
    # the entire 500-line file → ~5K tokens × 8 = 40K wasted.
    # ============================================================
    rereader: list[Call] = []
    # First Read — legitimate.
    rereader.append(Call("Read", 5000, 2500, note="Read step336_loop.py (full)"))
    # Six pointless re-Reads of the exact same file. After the 3rd,
    # the loop-detector starts firing.
    for i in range(7):
        reason = ""
        advisors: tuple[str, ...] = ()
        decision = "ALLOW"
        if i >= 2:
            # step336 loop-detector trips on the 3rd identical call.
            decision = "REQUIRE_APPROVAL"
            reason = (
                "same Read call repeated 3 times this session (threshold=3)"
            )
            advisors = ("loop-breaker", "context-compactor")
        rereader.append(Call(
            "Read", 5000, 2500,
            decision=decision, reason=reason, advisors=advisors,
            note=f"Re-Read same file (#{i + 2}) — should be cached",
        ))
    # Some Edits + tests mixed in (real-feeling).
    rereader.extend([
        Call("Edit", 1500, 200, note="Edit attempt 1"),
        Call("Bash", 800, 250, latency_ms=820.0, note="pytest"),
        Call("Edit", 1500, 200, note="Edit attempt 2"),
        Call("Bash", 800, 250, latency_ms=820.0, note="pytest again"),
        Call("Edit", 1500, 200, note="Edit attempt 3 — finally works"),
    ])
    agents.append(Agent(
        aid="agent-rereader",
        headline="Re-Reads the SAME 500-line file 8× (no cache awareness)",
        failure_mode=(
            "Token waste: 35K tokens re-fetched for content already "
            "loaded; loop detector fires on 6 of the 8 reads"
        ),
        calls=tuple(rereader),
    ))

    # ============================================================
    # agent-overscope — Reads whole 5000-line files when only a
    # 50-line section is needed. Each Read is 50K tokens but the
    # relevant slice is 500.
    # ============================================================
    overscope: list[Call] = []
    overscope.extend([
        Call("Read", 50_000, 5000,
             advisors=("context-compactor",),
             note="Read entire src/aegis/firewall/step336_loop.py (5000 LoC)"),
        Call("Grep", 800, 400,
             note="grep for 'count' inside that file (after-read)"),
        Call("Read", 50_000, 5000,
             advisors=("context-compactor", "cost-optimizer"),
             note="Read entire policy file (5000 LoC) — needed 20 lines"),
        Call("Edit", 2000, 300, note="Edit step336"),
        Call("Read", 50_000, 5000,
             advisors=("context-compactor", "cost-optimizer"),
             note="Re-Read step336 to verify"),
        Call("Bash", 1000, 300, latency_ms=920.0, note="pytest"),
        Call("Read", 50_000, 5000,
             advisors=("context-compactor",),
             note="Read test fixture file in full"),
        Call("Edit", 1500, 200, note="Edit fixture"),
        Call("Bash", 1000, 300, latency_ms=820.0, note="pytest again"),
    ])
    agents.append(Agent(
        aid="agent-overscope",
        headline="Reads 5000-line files in full when 50 lines would do",
        failure_mode=(
            "Token waste: 4× full-file Reads = 200K tokens; offset/"
            "limit would have used ~2K tokens total"
        ),
        calls=tuple(overscope),
    ))

    # ============================================================
    # agent-loop-stuck — runs the same pytest invocation 6× without
    # varying inputs. Classic "did I fix it yet?" without thinking.
    # ============================================================
    loop_stuck: list[Call] = []
    loop_stuck.append(Call("Read", 5000, 1500, note="Read step336"))
    loop_stuck.append(Call("Edit", 1000, 200, note="Edit attempt"))
    # 6 identical pytest runs. Loop detector fires on #3 onward.
    for i in range(6):
        decision = "ALLOW"
        reason = ""
        advisors: tuple[str, ...] = ()
        if i >= 2:
            decision = "REQUIRE_APPROVAL"
            reason = (
                "same Bash call repeated 3 times this session (threshold=3)"
            )
            advisors = ("loop-breaker", "test-runner")
        loop_stuck.append(Call(
            "Bash", 800, 400,
            latency_ms=920.0,
            decision=decision, reason=reason, advisors=advisors,
            note=f"pytest (run #{i + 1}, same args, same output)",
        ))
    # Then a few Read of test output (also pretty redundant).
    loop_stuck.extend([
        Call("Read", 600, 150, note="Read pytest output"),
        Call("Read", 600, 150, note="Re-Read same output"),
        Call("Read", 600, 150,
             decision="REQUIRE_APPROVAL",
             reason="same Read call repeated 3 times this session (threshold=3)",
             advisors=("loop-breaker",),
             note="3rd Read of same output"),
    ])
    loop_stuck.append(Call("Edit", 1000, 200, note="Edit (finally)"))
    loop_stuck.append(Call("Bash", 800, 300, latency_ms=820.0, note="pytest (passes)"))
    agents.append(Agent(
        aid="agent-loop-stuck",
        headline="6× identical pytest runs without varying inputs",
        failure_mode=(
            "Token + wall-clock waste: 6 × 1.2K = 7.2K tokens on "
            "identical pytest output. Loop detector + loop-breaker "
            "advisor flag this clearly"
        ),
        calls=tuple(loop_stuck),
    ))

    # ============================================================
    # agent-toolbox-confused — reaches for WebSearch / wrong tools
    # when the answer is already in the codebase.
    # ============================================================
    toolbox: list[Call] = []
    toolbox.extend([
        # Asks WebSearch about Aegis's own step336 instead of Grep.
        Call(
            "WebSearch", 600, 4000,
            latency_ms=480.0,
            advisors=("cost-optimizer",),
            note="WebSearch 'aegis step336 loop detector source' — useless",
        ),
        Call(
            "WebSearch", 700, 4500,
            latency_ms=420.0,
            advisors=("cost-optimizer",),
            note="WebSearch 'python loop counter off-by-one' — generic",
        ),
        Call(
            "WebSearch", 800, 5000,
            latency_ms=510.0,
            advisors=("cost-optimizer", "context-compactor"),
            note="WebSearch '... fix' — still wrong tool",
        ),
        # Eventually does the right thing.
        Call("Grep", 800, 400, note="Grep for 'count_threshold'"),
        Call("Read", 2500, 1000, note="Read step336 (with offset)"),
        # But wrong tool again — uses Bash `find` instead of Grep.
        Call(
            "Bash", 1500, 8000, latency_ms=2400.0,
            advisors=("cost-optimizer",),
            note="bash find . -name '*.py' | xargs cat — huge output",
        ),
        Call(
            "Bash", 1500, 8500, latency_ms=2600.0,
            advisors=("cost-optimizer", "context-compactor"),
            note="another find . | xargs grep — should've been Grep tool",
        ),
        # Finally Edit + test.
        Call("Edit", 1500, 200, note="Edit"),
        Call("Bash", 800, 300, latency_ms=820.0, note="pytest"),
    ])
    agents.append(Agent(
        aid="agent-toolbox-confused",
        headline="WebSearch + bash find instead of in-codebase Grep",
        failure_mode=(
            "Token + cost waste: 3 WebSearches (~$0.05 each) + 2 "
            "huge Bash find outputs (~16K tokens each). Total waste "
            "~$0.20 — 40× baseline for the same task"
        ),
        calls=tuple(toolbox),
    ))

    return agents


# ──────────────────────────────────────────────────────────────────
# Per-agent metrics + waste table
# ──────────────────────────────────────────────────────────────────


def _print_metrics(agents: list[Agent]) -> None:
    baseline_cost = sum(c.cost_usd for c in agents[0].calls)
    baseline_tokens = sum(
        c.tokens_in + c.tokens_out for c in agents[0].calls
    )

    print()
    print("=" * 84)
    print(" Coding-AI token-waste demo — same task, 5 agents, 5 efficiency profiles ")
    print("=" * 84)
    print()
    print(
        f"{'agent':<26}  {'calls':>5}  {'tokens':>9}  "
        f"{'cost $':>8}  {'vs baseline':>14}"
    )
    print("-" * 84)
    for a in agents:
        cost = sum(c.cost_usd for c in a.calls)
        toks = sum(c.tokens_in + c.tokens_out for c in a.calls)
        ratio = (cost / baseline_cost) if baseline_cost else 1.0
        ratio_str = (
            f"{ratio:>8.2f}× baseline"
            if a.aid != "agent-baseline" else "  (baseline)  "
        )
        print(
            f"{a.aid:<26}  {len(a.calls):>5}  {toks:>9,}  "
            f"{cost:>8.4f}  {ratio_str:>14}"
        )
    print()
    print(
        f"Baseline cost: ${baseline_cost:.4f} ({baseline_tokens:,} tokens)"
    )
    print(
        f"Total waste: $"
        f"{sum(sum(c.cost_usd for c in a.calls) for a in agents) - baseline_cost * len(agents):.4f}"
    )
    print()
    print("Anti-pattern attribution:")
    for a in agents:
        print(f"  {a.aid:<26}  {a.headline}")
        if a.failure_mode and a.failure_mode != "(none — this is what good looks like)":
            print(f"  {'':<26}  → {a.failure_mode}")
    print()


# ──────────────────────────────────────────────────────────────────
# ContextMemory seeding
# ──────────────────────────────────────────────────────────────────


def _seed_context_memory(agents: list[Agent]) -> int:
    DEMO_DIR.mkdir(parents=True, exist_ok=True)
    if DEMO_CM_PATH.exists():
        DEMO_CM_PATH.unlink()

    now_ns = time.time_ns()
    n_total = sum(len(a.calls) for a in agents)
    # Spread over the last hour so doctor --since 2h captures all.
    delta_ns = (60 * 60 * 1_000_000_000) // max(n_total, 1)
    written = 0

    with DEMO_CM_PATH.open("w", encoding="utf-8") as fh:
        for agent in agents:
            for i, call in enumerate(agent.calls):
                ts = now_ns - (n_total - written) * delta_ns
                rec = {
                    "schema_version": 1,
                    "ts_ns": ts,
                    "trace_id": f"{agent.aid}-tr-{i:03d}",
                    "invocation_id": f"{agent.aid}-inv-{i:03d}",
                    "aid": agent.aid,
                    "tenant_id": "demo",
                    "tool_name": call.tool,
                    "decision": call.decision,
                    "reason": call.reason,
                    "channel": None,
                    "provider": "anthropic-claude-3-5-sonnet",
                    "latency_ms": call.latency_ms,
                    "cost_usd": call.cost_usd,
                    "tokens_in": call.tokens_in,
                    "tokens_out": call.tokens_out,
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
    DEMO_CLAUDE_MD.write_text(
        "# Demo coding project\n"
        "\n"
        "Synthetic project for the coding-AI token-waste demo.\n"
        "\n"
        "## Workflow Discipline\n"
        "\n"
        "(sparse on purpose — Aegis proposes the missing rules)\n",
        encoding="utf-8",
    )


# ──────────────────────────────────────────────────────────────────
# CLI invocation
# ──────────────────────────────────────────────────────────────────


def _run(label: str, argv: list[str]) -> None:
    print()
    print("=" * 84)
    print(f" {label}")
    print(f"   $ {' '.join(argv)}")
    print("=" * 84)
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


def _aegis_argv(*args: str) -> list[str]:
    return ["uv", "run", "--project", str(REPO_ROOT), "aegis", *args]


# ──────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────


def main() -> int:
    agents = _agents()
    _print_metrics(agents)

    print(f"Seeding ContextMemory: {DEMO_CM_PATH}")
    n_written = _seed_context_memory(agents)
    print(f"  ✓ {n_written:,} records written")
    _seed_claude_md()
    print(f"  ✓ demo CLAUDE.md: {DEMO_CLAUDE_MD}")

    # Step 1: store inventory.
    _run(
        "Step 1 — Store inventory (`aegis memory show`)",
        _aegis_argv(
            "memory", "show",
            "--context-memory", str(DEMO_CM_PATH),
        ),
    )

    # Step 2: doctor cost / perf / security rollup.
    _run(
        "Step 2 — Doctor report — Cost / Performance / Security",
        _aegis_argv(
            "doctor", "--since", "2h",
            "--context-memory", str(DEMO_CM_PATH),
        ),
    )

    # Step 3: the actionable claude-md proposals.
    # Lower the cost threshold so the smaller wastes also surface.
    _run(
        "Step 3 — `memory claude-md` proposals (token-waste optimization)",
        _aegis_argv(
            "memory", "claude-md",
            "--context-memory", str(DEMO_CM_PATH),
            "--since", "2h",
            "--min-count", "3",
            "--min-tool-cost-usd", "0.01",
        ),
    )

    # Step 4: apply top proposal.
    _run(
        "Step 4 — Auto-apply top proposal (`--apply 1`)",
        _aegis_argv(
            "memory", "claude-md",
            "--context-memory", str(DEMO_CM_PATH),
            "--since", "2h",
            "--min-count", "3",
            "--min-tool-cost-usd", "0.01",
            "--apply", "1",
        ),
    )

    # Step 5: verify round-trip.
    _run(
        "Step 5 — Verify applied proposal (`aegis memory diff`)",
        _aegis_argv(
            "memory", "diff",
            "--claude-md", str(DEMO_CLAUDE_MD),
        ),
    )

    print()
    print("=" * 84)
    print(" Demo complete — token-waste detection effectiveness")
    print("=" * 84)
    print()
    baseline_cost = sum(c.cost_usd for c in agents[0].calls)
    total_cost = sum(
        sum(c.cost_usd for c in a.calls) for a in agents
    )
    waste = total_cost - baseline_cost * len(agents)
    pct = waste / total_cost * 100
    print(
        f"  Total observed spend:  ${total_cost:.4f}  "
        f"({sum(len(a.calls) for a in agents)} calls × 5 agents)"
    )
    print(f"  Optimal spend:         ${baseline_cost * len(agents):.4f}")
    print(f"  Surfaced waste:        ${waste:.4f}  ({pct:.1f}% of total)")
    print()
    print("How Aegis surfaced each waste pattern:")
    print("  • agent-rereader      → loop-detector (Read repeated 6×)")
    print("                          + advisor-recommendation rollup")
    print("                            (context-compactor recommended 6×)")
    print("  • agent-overscope     → high-cost-tool (Read total ~$1+)")
    print("                          + 8× context-compactor advisor calls")
    print("  • agent-loop-stuck    → loop-detector (Bash pytest repeated 4×)")
    print("                          + loop-breaker advisor recommended 4×")
    print("  • agent-toolbox       → high-cost-tool (WebSearch + huge Bash)")
    print("                          + 7× cost-optimizer advisor recommended")
    print()
    print("Operator workflow after seeing the proposals:")
    print()
    print("  1. Review proposals in step 3 (priority-sorted)")
    print("  2. `aegis memory claude-md --apply 1` (then 2, 3, ...)")
    print("  3. Future Claude Code sessions on this project see")
    print("     the new CLAUDE.md guidance → repeat-mistakes")
    print("     blocked at the source")
    print()
    print(f"Demo artifacts preserved at: {DEMO_DIR}")
    print("Re-run with --reset to wipe and re-seed.")
    return 0


def _ensure_clean_demo_dir() -> None:
    if "--reset" in sys.argv[1:] and DEMO_DIR.exists():
        shutil.rmtree(DEMO_DIR)


if __name__ == "__main__":
    _ensure_clean_demo_dir()
    raise SystemExit(main())
