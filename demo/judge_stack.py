#!/usr/bin/env python3
"""v3.0 hybrid judge stack live demo.

Boots NO infrastructure of its own. For each canonical scenario,
runs both the v2.5 attribution head alone AND the v3.0 hybrid stack
(M13 → Phi → Dummy by default) and prints the per-tier trace plus
the final verdict. Demonstrates the (latency × determinism) tower
described in the v2.5/v2.6/v3.0 conversation.

Run:
    AEGIS_EMBEDDING_PROVIDER=dummy AEGIS_JUDGE_PROVIDER=dummy \\
      uv run python demo/judge_stack.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

os.environ.setdefault("AEGIS_EMBEDDING_PROVIDER", "dummy")
os.environ.setdefault("AEGIS_JUDGE_PROVIDER", "dummy")
os.environ.setdefault("AEGIS_SAFETY_PROVIDER", "dummy")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aegis.atv.builder import build_atv  # noqa: E402
from aegis.cost.model_flops import DEFAULT_DOLLAR_PER_FLOP, expected_flops  # noqa: E402
from aegis.hw_telemetry import simulate  # noqa: E402
from aegis.judge.attribution_head import AttributionHead  # noqa: E402
from aegis.judge.hybrid import HybridJudge  # noqa: E402
from aegis.schema import ATVHeader, ATVInput, CostEfficiencyMetrics  # noqa: E402

MODEL = "claude-haiku-4-5"


def make_input(
    *,
    tool: str = "Bash",
    args: dict[str, Any] | None = None,
    in_tokens: float = 1000.0,
    out_tokens: float = 500.0,
) -> ATVInput:
    args = args or {"command": "ls"}
    cum_dollars = expected_flops(MODEL, in_tokens, out_tokens) * DEFAULT_DOLLAR_PER_FLOP
    return ATVInput(
        header=ATVHeader(
            trace_id="d" * 32, span_id="d" * 16,
            tenant_id="demo", aid="agent-judge-stack",
            timestamp_ns=0, model_hash=MODEL,
        ),
        tool_name=tool,
        tool_args_json=json.dumps(args),
        cost_estimate=CostEfficiencyMetrics(
            input_token_count=in_tokens, output_token_count=out_tokens,
            cumulative_tokens=in_tokens + out_tokens,
            cumulative_dollars=cum_dollars,
        ),
    )


SCENARIOS: list[tuple[str, dict[str, Any]]] = [
    (
        "innocent read_file",
        {"tool": "read_file", "args": {"file_path": "/tmp/x.txt"},
         "in_tokens": 10.0, "out_tokens": 5.0, "attack": ""},
    ),
    (
        "rm -rf (destructive)",
        {"tool": "Bash", "args": {"command": "rm -rf /tmp/foo"},
         "attack": ""},
    ),
    (
        "Bash with execute_shell semantic",
        {"tool": "execute_shell", "args": {"command": "df -h"}, "attack": ""},
    ),
    (
        "send_email (high-impact tool)",
        {"tool": "send_email", "args": {"body": "hi"}, "attack": ""},
    ),
    (
        "HW iommu_violation injected",
        {"tool": "Bash", "args": {"command": "ls"},
         "attack": "iommu_violation"},
    ),
]


def run_one(label: str, params: dict[str, Any]) -> None:
    inp = make_input(
        tool=params["tool"], args=params["args"],
        in_tokens=params.get("in_tokens", 1000.0),
        out_tokens=params.get("out_tokens", 500.0),
    )
    if params.get("attack"):
        atv = build_atv(inp, hw=simulate(inp, attack=params["attack"]))
    else:
        atv = build_atv(inp)

    print()
    print(f"━━━ Scenario: {label} ━━━")
    print(f"  tool={inp.tool_name}, args={inp.tool_args_json[:60]}, "
          f"hw_attack={params.get('attack') or 'none'}")

    # Tier 1: M13 alone
    head = AttributionHead()
    v_m13 = head.evaluate_full("", atv=atv, inp=inp)
    print(f"  [M13 alone]      {v_m13.decision:<18} conf={v_m13.confidence:.3f}  "
          f"latency={v_m13.latency_ms:.2f}ms")

    # Hybrid stack (default)
    hybrid = HybridJudge()
    v_hyb = hybrid.evaluate_full("", atv=atv, inp=inp)
    print(f"  [hybrid stack]   {v_hyb.decision:<18} conf={v_hyb.confidence:.3f}  "
          f"cumulative={v_hyb.latency_ms:.2f}ms")
    print(f"    → reason: {v_hyb.reason[:80]}")
    print(f"    → routing trace ({len(v_hyb.layer_traces)} tier(s)):")
    for t in v_hyb.layer_traces:
        print(f"        {t}")


def main() -> int:
    print("v3.0 Aegis Hybrid Judge Stack — live demo")
    print("=" * 70)
    print(
        "  Tier 1: M13 AttributionHead   (frozen 30-feature, <1ms, bit-determ.)\n"
        "  Tier 2: LocalPhiJudge         (Phi-4-mini-q4 stub, <1ms, attestable)\n"
        "  Tier 3: DummyJudge            (regex fallback, <1ms, bit-determ.)\n"
        "          (Haiku tier auto-added when ANTHROPIC_API_KEY is set.)"
    )
    for label, params in SCENARIOS:
        run_one(label, params)
    print()
    print("Reading the trace:")
    print("  • Each tier reports its decision + confidence + observed latency.")
    print("  • A tier 'commits' on BLOCK / REQUIRE_APPROVAL OR on")
    print("    ALLOW with confidence ≥ allow_threshold (M13: 0.30, Phi: 0.40).")
    print("  • The hybrid model_hash points to the deciding tier so")
    print("    `aegis verify-audit` can re-run that exact path bit-for-bit.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
