#!/usr/bin/env python3
"""v3.1 KV cache advisor — live demo.

Runs ``aegis.performance.kv_cache_advisor`` over a few canonical
scenarios and prints the advisory payload a downstream LLM serving
runtime would consume. No infrastructure required.

Run:
    AEGIS_EMBEDDING_PROVIDER=dummy AEGIS_JUDGE_PROVIDER=dummy \\
      uv run python demo/kv_cache_advisor.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

os.environ.setdefault("AEGIS_EMBEDDING_PROVIDER", "dummy")
os.environ.setdefault("AEGIS_JUDGE_PROVIDER", "dummy")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aegis.atv.builder import build_atv  # noqa: E402
from aegis.cost.model_flops import DEFAULT_DOLLAR_PER_FLOP, expected_flops  # noqa: E402
from aegis.performance import kv_cache_advisor  # noqa: E402
from aegis.schema import ATVHeader, ATVInput, CostEfficiencyMetrics  # noqa: E402

MODEL = "claude-haiku-4-5"


def make_input(
    *,
    tool: str = "Bash",
    args: dict[str, Any] | None = None,
    in_tokens: float = 1000.0,
    out_tokens: float = 500.0,
    plan_text: str = "",
    cache_hit_rate: float = 0.0,
    context_util: float = 0.0,
    progress: float = 0.0,
    novelty: float = 0.0,
) -> ATVInput:
    args = args or {"command": "ls"}
    cum_dollars = expected_flops(MODEL, in_tokens, out_tokens) * DEFAULT_DOLLAR_PER_FLOP
    return ATVInput(
        header=ATVHeader(
            trace_id="d" * 32, span_id="d" * 16,
            tenant_id="demo", aid="agent-kv-demo", timestamp_ns=0,
            model_hash=MODEL,
        ),
        tool_name=tool,
        tool_args_json=json.dumps(args),
        plan_text=plan_text,
        cost_estimate=CostEfficiencyMetrics(
            input_token_count=in_tokens, output_token_count=out_tokens,
            cumulative_tokens=in_tokens + out_tokens,
            cumulative_dollars=cum_dollars,
            cache_hit_rate=cache_hit_rate,
            context_utilization_ratio=context_util,
            task_progress_score=progress,
        ),
        novelty={"composite_novelty": novelty},
    )


SCENARIOS: list[tuple[str, dict[str, Any]]] = [
    (
        "fresh task (no signal yet)",
        {"tool": "read_file", "args": {"file_path": "/tmp/x.txt"}},
    ),
    (
        "mid-task hot path (re-visit likely)",
        {"tool": "read_file", "args": {"file_path": "/tmp/x.txt"},
         "progress": 0.55, "novelty": 0.10, "cache_hit_rate": 0.7,
         "context_util": 0.4},
    ),
    (
        "long structured prompt → speculative candidate",
        {"tool": "read_file", "args": {"file_path": "/tmp/x.txt"},
         "plan_text": "```python\ndef foo():\n    pass\n```\n" * 30,
         "progress": 0.4, "novelty": 0.05},
    ),
    (
        "OOM-pressure: low cache hit + high context util → cold",
        {"tool": "Bash", "args": {"command": "ls /tmp"},
         "cache_hit_rate": 0.05, "context_util": 0.85, "progress": 0.20},
    ),
    (
        "high-blast tool (different cohort)",
        {"tool": "execute_shell", "args": {"command": "rm -rf /tmp/x"},
         "cache_hit_rate": 0.3},
    ),
]


def run_one(label: str, params: dict[str, Any]) -> None:
    inp = make_input(**params)
    atv = build_atv(inp)
    advice = kv_cache_advisor(atv, inp)

    print()
    print(f"━━━ Scenario: {label} ━━━")
    print(f"  tool={inp.tool_name}, plan_len={len(inp.plan_text)}")
    print(f"  residency={advice.residency_class}  speculative={advice.speculative_decode}  "
          f"confidence={advice.confidence:.3f}  latency={advice.latency_ms:.3f}ms")
    print(f"  batch_key={advice.batch_key}")
    if advice.prefetch_segment_ids:
        print(f"  prefetch  ({len(advice.prefetch_segment_ids)}): "
              f"{advice.prefetch_segment_ids[:3]}{'...' if len(advice.prefetch_segment_ids) > 3 else ''}")
    if advice.evict_candidates:
        print(f"  evict     ({len(advice.evict_candidates)}): "
              f"{advice.evict_candidates[:3]}{'...' if len(advice.evict_candidates) > 3 else ''}")
    print("  reasons:")
    for r in advice.reasons:
        print(f"    - {r}")


def main() -> int:
    print("v3.1 Aegis KV Cache Advisor — live demo")
    print("=" * 70)
    print(
        "  Pure function: 2080-D ATV → KVCacheAdvice (prefetch IDs,\n"
        "  evict candidates, residency_class, batch_key, speculative).\n"
        "  Sub-millisecond, deterministic, advisory-only.\n"
        "  Runtime (vLLM/MLX/llama.cpp) consumes via /advisory/kv_cache."
    )
    for label, params in SCENARIOS:
        run_one(label, params)
    print()
    print("Reading the trace:")
    print("  • residency_class=hot → prefetch into HBM aggressively.")
    print("  • residency_class=cold → emit eviction candidates.")
    print("  • batch_key clusters peers in the same task phase.")
    print("  • speculative_decode → enable draft-model decoding.")
    print("  • advisor_hash pins the advice to a specific advisor revision.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
