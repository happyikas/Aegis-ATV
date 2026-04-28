#!/usr/bin/env python3
"""v2.3+v2.4 HW/SW double-check live demo.

Boots NO infrastructure of its own. Builds a synthetic ATVInput,
runs ``aegis.hw_telemetry.simulate`` once per attack mode, then
exercises BOTH the M12 cost-divergence escalation gate (Claim 27)
AND the v2.4 step337 HW band anomaly gate. The combination catches
all 6 attack modes: M12 covers the 3 cost-axis attacks
(token_flops_mismatch / hbm_exfil / cost_underreport) and step337
covers the 3 hardware-state attacks (thermal_spike / network_exfil /
iommu_violation).

This is the demo that lets MVP show the two-axis double-check value
proposition without needing real T3 hardware (M19-M22 deferred).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Force dummy embedding/judge so the demo runs without API keys.
os.environ.setdefault("AEGIS_EMBEDDING_PROVIDER", "dummy")
os.environ.setdefault("AEGIS_JUDGE_PROVIDER", "dummy")
os.environ.setdefault("AEGIS_SAFETY_PROVIDER", "dummy")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aegis.atv.builder import build_atv
from aegis.cost.divergence import compute_divergence
from aegis.cost.escalation import evaluate_escalation
from aegis.cost.model_flops import DEFAULT_DOLLAR_PER_FLOP, expected_flops
from aegis.firewall import step337_hw_anomaly
from aegis.firewall.core import FirewallContext
from aegis.hw_telemetry import ATTACK_MODES, simulate
from aegis.schema import ATVHeader, ATVInput, CostEfficiencyMetrics

MODEL = "claude-haiku-4-5"


def make_input() -> ATVInput:
    """Realistic small Haiku call: 1k tokens in, 500 out, ~$0.024."""
    in_tokens, out_tokens = 1000.0, 500.0
    expected_dollars = expected_flops(MODEL, in_tokens, out_tokens) * DEFAULT_DOLLAR_PER_FLOP
    return ATVInput(
        header=ATVHeader(
            trace_id="d" * 32, span_id="d" * 16,
            tenant_id="demo", aid="agent-double-check-demo",
            timestamp_ns=0, model_hash=MODEL,
        ),
        tool_name="Bash",
        tool_args_json=json.dumps({"command": "summarize FOO repository"}),
        cost_estimate=CostEfficiencyMetrics(
            input_token_count=in_tokens,
            output_token_count=out_tokens,
            cumulative_tokens=in_tokens + out_tokens,
            cumulative_dollars=expected_dollars,
        ),
    )


def run_one(label: str, attack: str = "") -> tuple[str, str, str, str, str, str, str]:
    inp = make_input()
    hw = simulate(inp, attack=attack)

    # Cost-divergence escalation (M12, Claim 27)
    div = compute_divergence(
        inp.cost_estimate,
        model_name=MODEL,
        hw_flops_observed=hw.flops_observed,
        hw_hbm_bytes_observed=hw.hbm_bytes_observed,
    )
    decision = evaluate_escalation(div)
    m12_gate = "ESCALATE" if decision.triggered else "ok"

    # v2.4: step337 HW band anomaly gate
    atv = build_atv(inp, hw=hw)
    step_res = step337_hw_anomaly.run(atv, inp, FirewallContext())
    if step_res.verdict == "BLOCK":
        s337_gate = "BLOCK"
    elif step_res.verdict == "REQUIRE_APPROVAL":
        s337_gate = "ASK"
    else:
        s337_gate = "ok"

    return (
        label,
        f"{div.token_to_flops:.2f}",
        f"{div.memory_cost:.2f}",
        f"{div.dollar_cost:.2f}",
        m12_gate,
        s337_gate,
        decision.metric if decision.triggered else (
            step_res.reason.split("(")[0].strip() if step_res.verdict else "-"
        ),
    )


def main() -> int:
    print("v2.3+v2.4 HW/SW double-check demo — same SW request, varying HW telemetry")
    print("=" * 84)
    print()
    print("  Test SW input:")
    print("    tool=Bash, model=claude-haiku-4-5, in_tokens=1000, out_tokens=500")
    print("    cum_dollars≈$0.024 (= expected_flops × DEFAULT_DOLLAR_PER_FLOP)")
    print()
    print("  Two gates run on the populated ATV HW band:")
    print("    M12   = cost-divergence escalation (Claim 27, j-14/j-15/j-16)")
    print("    s337  = HW band anomaly step (v2.4): IOMMU/hypervisor/exfil")
    print()
    print(
        f"  {'scenario':<32} {'tok→FLOPS':>9} {'mem':>5} "
        f"{'$':>5} {'M12':>9} {'s337':>5} {'rule':>26}"
    )
    print(f"  {'-' * 32} {'-' * 9} {'-' * 5} {'-' * 5} {'-' * 9} {'-' * 5} {'-' * 26}")

    rows = [run_one("honest agent (no attack)", attack="")]
    for mode in sorted(ATTACK_MODES):
        rows.append(run_one(f"attack: {mode}", attack=mode))
    rows.append(run_one("multi: token+network+iommu",
                        attack="token_flops_mismatch,network_exfil,iommu_violation"))

    for r in rows:
        label, t, m, d, m12, s337, rule = r
        caught = m12 == "ESCALATE" or s337 in {"BLOCK", "ASK"}
        marker = "✗" if caught else "✓"
        print(
            f"  {marker} {label:<30} {t:>9} {m:>5} {d:>5} {m12:>9} {s337:>5} {rule:>26}"
        )

    print()
    print("  Reading the table:")
    print("    • token→FLOPS / mem / $   = Claim 26 j-14/j-15/j-16 (cost axis)")
    print("    • M12 ESCALATE            = step340 verdict → REQUIRE_APPROVAL")
    print("    • s337 BLOCK / ASK        = step337 returns BLOCK / REQUIRE_APPROVAL")
    print("    • cost-axis attacks (token_flops_mismatch, hbm_exfil, cost_underreport)")
    print("      → caught by M12.")
    print("    • hardware-state attacks (thermal_spike, network_exfil, iommu_violation)")
    print("      → caught by s337.")
    print()
    print("  Honest agent stays quiet; every attack mode is now blocked or")
    print("  escalated by at least one of the two gates. This is the SW path that")
    print("  becomes T3's HW path when M19 (RAPL/NVML) — M22 (CSD) physical")
    print("  telemetry lands.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
