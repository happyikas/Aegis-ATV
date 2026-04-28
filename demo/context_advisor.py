#!/usr/bin/env python3
"""v3.7 Context window advisor — live demo.

Simulates a 12-turn agent conversation and asks the context advisor
which past turns to keep verbatim, summarise, or drop under a tight
token budget. No infrastructure needed.

Run:
    AEGIS_EMBEDDING_PROVIDER=dummy AEGIS_JUDGE_PROVIDER=dummy \\
      uv run python demo/context_advisor.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

os.environ.setdefault("AEGIS_EMBEDDING_PROVIDER", "dummy")
os.environ.setdefault("AEGIS_JUDGE_PROVIDER", "dummy")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aegis.atv.builder import build_atv  # noqa: E402
from aegis.cost.model_flops import DEFAULT_DOLLAR_PER_FLOP, expected_flops  # noqa: E402
from aegis.performance import context_advisor  # noqa: E402
from aegis.schema import ATVHeader, ATVInput, CostEfficiencyMetrics  # noqa: E402

MODEL = "claude-haiku-4-5"


def make_input(*, agent_state_text: str, progress: float, novelty: float = 0.1, tool: str = "Bash") -> ATVInput:
    cum = expected_flops(MODEL, 1000.0, 500.0) * DEFAULT_DOLLAR_PER_FLOP
    return ATVInput(
        header=ATVHeader(
            trace_id="d" * 32, span_id="d" * 16,
            tenant_id="demo", aid="agent-ctx-demo", timestamp_ns=0,
            model_hash=MODEL,
        ),
        tool_name=tool,
        tool_args_json=json.dumps({"command": "ls"}),
        agent_state_text=agent_state_text,
        cost_estimate=CostEfficiencyMetrics(
            input_token_count=1000.0, output_token_count=500.0,
            cumulative_tokens=1500.0, cumulative_dollars=cum,
            task_progress_score=progress,
        ),
        novelty={"composite_novelty": novelty},
    )


# Simulated 12-turn conversation. Three task phases:
#   turns 0-3: setup / exploration  ("explore-codebase")
#   turns 4-7: mid-task              ("refactor-auth")
#   turns 8-11: focused on current   ("fix-import-bug")  ← current is here
HISTORY = [
    ("turn-00", "explore-codebase",  0.05, 0.5,  300),
    ("turn-01", "explore-codebase",  0.10, 0.4,  450),
    ("turn-02", "explore-codebase",  0.15, 0.3,  600),
    ("turn-03", "explore-codebase",  0.20, 0.2,  500),
    ("turn-04", "refactor-auth",     0.30, 0.4,  700),
    ("turn-05", "refactor-auth",     0.40, 0.3,  650),
    ("turn-06", "refactor-auth",     0.50, 0.2,  600),
    ("turn-07", "refactor-auth",     0.55, 0.1,  550),
    ("turn-08", "fix-import-bug",    0.60, 0.3,  400),
    ("turn-09", "fix-import-bug",    0.65, 0.2,  350),
    ("turn-10", "fix-import-bug",    0.70, 0.1,  500),
    ("turn-11", "fix-import-bug",    0.75, 0.1,  450),
]


def main() -> int:
    # Current upcoming turn — same task phase as turns 8-11
    current_inp = make_input(agent_state_text="fix-import-bug", progress=0.80, novelty=0.05)
    current_atv = build_atv(current_inp)

    history_atvs = []
    history_ids = []
    history_costs = []
    for turn_id, state, progress, novelty, cost in HISTORY:
        inp = make_input(agent_state_text=state, progress=progress, novelty=novelty)
        history_atvs.append(build_atv(inp))
        history_ids.append(turn_id)
        history_costs.append(cost)

    total_history_tokens = sum(history_costs)

    print("v3.7 Aegis Context Window Advisor — live demo")
    print("=" * 70)
    print(f"  History: {len(HISTORY)} turns, {total_history_tokens} tokens total")
    print("  Current task: 'fix-import-bug', progress=0.80")
    print()

    for budget in (5000, 2000, 800):
        advice = context_advisor(
            current_atv, history_atvs, history_ids, history_costs,
            token_budget=budget,
        )
        print(f"━━━ Budget: {budget} tokens ━━━")
        print(f"  After   : {advice.total_token_cost_after} tokens "
              f"(savings={advice.expected_token_savings})")
        print(f"  Latency : {advice.latency_ms:.3f}ms  "
              f"Confidence: {advice.confidence:.2f}")
        print(f"  keep    ({len(advice.keep_verbatim_turn_ids)}): "
              f"{advice.keep_verbatim_turn_ids}")
        print(f"  summarize ({len(advice.summarize_turn_ids)}): "
              f"{advice.summarize_turn_ids}")
        print(f"  drop    ({len(advice.drop_turn_ids)}): "
              f"{advice.drop_turn_ids}")
        print()

    print("Per-turn relevance scores (highest first, budget=2000):")
    advice = context_advisor(
        current_atv, history_atvs, history_ids, history_costs,
        token_budget=2000,
    )
    sorted_per_turn = sorted(advice.per_turn, key=lambda t: t.score, reverse=True)
    for t in sorted_per_turn:
        print(f"  {t.turn_id}  score={t.score:.3f}  cost={t.token_cost:>4}  "
              f"→ {t.decision}")
    print()
    print("Reading the trace:")
    print("  • Recent same-phase turns (turn-08..11) score highest.")
    print("  • Older different-phase turns (turn-00..03) score lowest, drop first.")
    print("  • Mid-phase turns (turn-04..07) get summarised under tight budget.")
    print("  • advisor_hash pins the advice to a specific advisor revision.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
