#!/usr/bin/env python3
"""v3.3 — runtime ↔ Aegis closed-loop demo.

Simulates an LLM serving runtime (MLX-LM / llama.cpp / vLLM
equivalent) doing a multi-turn conversation. For each turn:

    1. Runtime asks Aegis for KV cache advice (POST /advisory/kv_cache).
    2. Runtime "decodes" the next response — here we synthesise a
       cache_hit_rate from the advice the runtime *would* honour.
    3. Runtime reports the measured perf back via /tool-outcome.
    4. Aegis folds it into the EWMA so the next turn's advice
       reflects measured reality.

No external runtime needed. The point is to show the *contract* —
what data flows in, what comes out, and how the loop closes.

Run:
    AEGIS_EMBEDDING_PROVIDER=dummy AEGIS_JUDGE_PROVIDER=dummy \\
      uv run python demo/runtime_closed_loop.py
"""

from __future__ import annotations

import json
import os
import random
import sys
from pathlib import Path
from typing import Any

os.environ.setdefault("AEGIS_EMBEDDING_PROVIDER", "dummy")
os.environ.setdefault("AEGIS_JUDGE_PROVIDER", "dummy")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from aegis.api.advisory import make_router as make_advisory_router  # noqa: E402
from aegis.api.tool_outcome import make_router as make_tool_outcome_router  # noqa: E402
from aegis.atmu import IntentLog  # noqa: E402
from aegis.cost.model_flops import DEFAULT_DOLLAR_PER_FLOP, expected_flops  # noqa: E402
from aegis.performance import reset_default_store  # noqa: E402
from aegis.schema import ATVHeader, ATVInput, CostEfficiencyMetrics  # noqa: E402

TENANT = "demo-tenant"
AID = "agent-rt-loop"


def _atv_input(
    *,
    tool: str,
    args: dict[str, Any],
    plan_text: str = "",
    progress: float = 0.0,
    cache_hit_rate: float = 0.0,
) -> ATVInput:
    cum = expected_flops("claude-haiku-4-5", 1000.0, 500.0) * DEFAULT_DOLLAR_PER_FLOP
    return ATVInput(
        header=ATVHeader(
            trace_id="d" * 32, span_id="d" * 16,
            tenant_id=TENANT, aid=AID, timestamp_ns=0,
            model_hash="claude-haiku-4-5",
        ),
        tool_name=tool,
        tool_args_json=json.dumps(args),
        plan_text=plan_text,
        cost_estimate=CostEfficiencyMetrics(
            input_token_count=1000.0, output_token_count=500.0,
            cumulative_tokens=1500.0, cumulative_dollars=cum,
            task_progress_score=progress,
            cache_hit_rate=cache_hit_rate,
        ),
        novelty={"composite_novelty": max(0.0, 0.4 - progress * 0.5)},
    )


def _runtime_simulated_cache_hit_rate(
    advice: dict[str, Any],
    rng: random.Random,
) -> float:
    """If the runtime had honoured `advice`, what hit-rate would it see?

    Synthetic: hot residency_class with non-empty prefetch → ~85% hit.
    Cold with eviction candidates only → ~20%. Warm baseline → ~50%.
    Random ±5% jitter for realism.
    """
    base = {
        "hot": 0.85,
        "warm": 0.50,
        "cold": 0.20,
    }[advice["residency_class"]]
    if advice["speculative_decode"]:
        base = min(1.0, base + 0.05)  # speculative cuts memory pressure
    return max(0.0, min(1.0, base + rng.uniform(-0.05, 0.05)))


def main() -> int:
    rng = random.Random(42)  # deterministic demo

    # Boot a minimal app with both endpoints
    intent_log = IntentLog(":memory:")
    app = FastAPI()
    app.include_router(make_tool_outcome_router(intent_log=intent_log))
    app.include_router(make_advisory_router())

    reset_default_store()  # clean slate

    print("v3.3 Aegis ↔ Runtime closed-loop demo")
    print("=" * 70)
    print(
        "  Simulated MLX-LM / llama.cpp runtime.\n"
        "  Per turn: ask Aegis for advice → 'decode' → report perf back.\n"
        "  After ~5 turns the EWMA converges; advice confidence climbs."
    )

    record_id = intent_log.append_tentative(
        aid=AID, tenant_id=TENANT,
        trace_id="d" * 32, span_id="d" * 16, parent_span_id=None,
        tool_name="Bash", tool_args_hash="d", blast_radius=1,
        atv_commitment="d" * 32,
    )["record_id"]

    with TestClient(app) as client:
        for turn in range(1, 9):
            # Vary the workload — early turns exploratory (high novelty),
            # later turns settle (low novelty + progress climbs)
            progress = min(0.8, turn * 0.10)

            inp = _atv_input(
                tool="read_file",
                args={"file_path": f"/work/turn{turn}.md"},
                plan_text="```python\ndef step():\n    pass\n```\n" * 25,
                progress=progress,
            )
            body = json.loads(inp.model_dump_json())
            r = client.post("/advisory/kv_cache", json=body)
            advice = r.json()

            measured = _runtime_simulated_cache_hit_rate(advice, rng)

            # Runtime reports back
            client.post("/tool-outcome", json={
                "record_id": record_id,
                "status": "success",
                "result_hash": f"r-{turn}",
                "tenant_id": TENANT, "aid": AID,
                "cache_hit_rate": measured,
                "context_utilization_ratio": 0.50,
                "tokens_per_second": 200.0 + turn * 5.0,
                "runtime_latency_ms": 45.0 - turn * 1.5,
            })

            print()
            print(f"━━━ Turn {turn}  (progress={progress:.2f}) ━━━")
            print(f"  advice  residency={advice['residency_class']:<5}  "
                  f"speculative={advice['speculative_decode']}  "
                  f"confidence={advice['confidence']:.3f}")
            if advice['prefetch_segment_ids']:
                print(f"          prefetch ({len(advice['prefetch_segment_ids'])}): "
                      f"{advice['prefetch_segment_ids'][:2]}…")
            if advice['evict_candidates']:
                print(f"          evict    ({len(advice['evict_candidates'])}): "
                      f"{advice['evict_candidates'][:2]}…")
            print(f"  measured cache_hit_rate={measured:.3f}  reported back")

    print()
    print("Reading the trace:")
    print("  • Turn 1: cost band empty → advice confidence ~0.")
    print("  • Turn 2+: EWMA carries measured cache_hit_rate forward.")
    print("  • As progress climbs and novelty drops, residency → hot.")
    print("  • A real runtime (vLLM/MLX) plugs into the same two endpoints.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
