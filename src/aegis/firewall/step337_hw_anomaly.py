"""Step 337 — HW band anomaly gate (v2.4).

The ``atmu_anomaly`` subfield referenced below is the Agent Telemetry
Management Unit (ATMU) anomaly band of the ATV HW envelope.

Closes the gap surfaced by v2.3's ``smoke_hw_double_check.py`` demo:
``thermal_spike`` / ``network_exfil`` / ``iommu_violation`` attacks
populate the ATV HW band visibly but didn't trip step340's M12
cost-divergence escalation (which only watches j-14/j-15/j-16). This
step reads the HW band normalized signals directly and converts
clear-cut anomalies into BLOCK / REQUIRE_APPROVAL.

Three severity-1 (BLOCK) signals — non-recoverable isolation /
data-exfil indicators:

* ``aid_tag_transitions[0]`` ≥ 0.2  — IOMMU breach (any single tag
  violation hits 1/5 = 0.2). Cross-tenant memory access by a memory
  controller (Claim 5).
* ``atmu_anomaly[2]``       ≥ 0.34 — hypervisor ring violation (1/3
  ≈ 0.33). VM-to-host privilege escalation attempt.
* ``network_telemetry[0]``  ≥ 0.2 — ≥10 MB egress in one tool call
  AND ``inp.tool_name`` is NOT in the explicit egress-allowed set
  (fetch / curl / render_image / send_email / post_message etc.).
  Unexpected exfil pattern.

Three severity-2 (REQUIRE_APPROVAL) signals — suspicious but
recoverable / human-judgable:

* ``thermal_ecc_drift[3]``  == 1.0 — sustained 90°C+ p95 flag.
  Suggests hidden background work even if the visible tool is
  innocuous.
* ``dma_fanout[0]``         ≥ 0.5 — ≥16 distinct DMA peers in one
  call. A single read_file shouldn't fan out to 16 endpoints.
* ``atmu_anomaly[3]``       ≥ 1.0 — uncorrectable ECC error. Could
  be a transient glitch or rowhammer probe.

The step is a **no-op when the HW band is zero-filled** (the v2.2
T2 default + sidecar without ``AEGIS_HW_PROVIDER=sim``). That keeps
the existing 829-test surface unaffected; only callers that opt in
to HW telemetry pay the gate.

Pipeline position: between step336 (loop / cost runaway) and step340
(sLLM judge). step340 still runs on REQUIRE_APPROVAL signals so the
LLM can refine — it does NOT run on BLOCK (per orchestrator
short-circuit).
"""

from __future__ import annotations

import numpy as np

from aegis.firewall.core import FirewallContext, StepResult
from aegis.schema import (
    SLICE_AID_TAG_TRANSITIONS,
    SLICE_ATMU_ANOMALY,
    SLICE_DMA_FANOUT,
    SLICE_HW_BAND,
    SLICE_NETWORK_TELEMETRY,
    SLICE_THERMAL_ECC_DRIFT,
    ATVInput,
)

# Tool names where outbound network traffic is the *intended* effect.
# step337 exempts these from the network-exfil rule; other layers
# (step311 exfil_url, step340 sLLM judge) still scrutinise the URL /
# payload itself.
_EGRESS_ALLOWED: frozenset[str] = frozenset({
    # Claude Code canonical
    "WebFetch", "WebSearch",
    # Donor / aegis-mvp tool names
    "fetch", "http_request", "curl", "browse",
    "render_image", "embed_image",
    "send_email", "post_message",
    # Generic
    "call_external_api",
})

# ─────────────────────────────────────────────────────────────────────
# Per-rule thresholds (in normalized [0,1] space — see hw_encoders).
# ─────────────────────────────────────────────────────────────────────
_TH_IOMMU       = 0.20    # 1+ AID-tag violation (1/5 = 0.20)
_TH_HYPERVISOR  = 0.34    # 1+ ring violation (1/3 ≈ 0.33)
_TH_EXFIL       = 0.20    # 10 MB / 50 MB norm
_TH_THERMAL     = 1.0     # thermal_anomaly flag (== 1.0 when ≥90°C)
_TH_DMA_FANOUT  = 0.50    # 16+ peers (16/32 = 0.50)
_TH_ECC_UNCORR  = 1.0     # any uncorrectable ECC


def _hw_band_is_zero(atv: np.ndarray) -> bool:
    """Cheap T2-default short-circuit. ``np.any`` instead of ``.any()``
    on the slice for clarity."""
    return not bool(np.any(atv[SLICE_HW_BAND]))


def run(
    atv: np.ndarray, inp: ATVInput, ctx: FirewallContext
) -> StepResult:
    if _hw_band_is_zero(atv):
        return StepResult(
            verdict=None, reason="", trace="step337: HW band zero (T2 default)"
        )

    # Pull the slot values we care about.
    iommu = float(atv[SLICE_AID_TAG_TRANSITIONS][0])
    hypervisor = float(atv[SLICE_ATMU_ANOMALY][2])
    thermal_anom = float(atv[SLICE_THERMAL_ECC_DRIFT][3])
    network_out = float(atv[SLICE_NETWORK_TELEMETRY][0])
    dma_fanout = float(atv[SLICE_DMA_FANOUT][0])
    ecc_uncorrectable = float(atv[SLICE_ATMU_ANOMALY][3])

    ctx.extras["hw_iommu_norm"] = iommu
    ctx.extras["hw_hypervisor_norm"] = hypervisor
    ctx.extras["hw_thermal_anomaly"] = thermal_anom
    ctx.extras["hw_network_out_norm"] = network_out
    ctx.extras["hw_dma_fanout_norm"] = dma_fanout
    ctx.extras["hw_ecc_uncorrectable"] = ecc_uncorrectable

    # ── Severity 1: BLOCK ────────────────────────────────────────────
    if iommu >= _TH_IOMMU:
        return StepResult(
            verdict="BLOCK",
            reason=f"rule:hw_iommu_violation (norm={iommu:.2f})",
            trace="step337: IOMMU AID-tag breach detected in HW band",
        )

    if hypervisor >= _TH_HYPERVISOR:
        return StepResult(
            verdict="BLOCK",
            reason=f"rule:hw_hypervisor_violation (norm={hypervisor:.2f})",
            trace="step337: hypervisor ring violation in HW band",
        )

    if network_out >= _TH_EXFIL and inp.tool_name not in _EGRESS_ALLOWED:
        return StepResult(
            verdict="BLOCK",
            reason=(
                f"rule:hw_network_exfil ({inp.tool_name} egress "
                f"≥10 MB; expected egress tools: fetch/curl/render_image/...)"
            ),
            trace="step337: unexpected egress burst on non-egress tool",
        )

    # ── Severity 2: REQUIRE_APPROVAL ─────────────────────────────────
    if thermal_anom >= _TH_THERMAL:
        return StepResult(
            verdict="REQUIRE_APPROVAL",
            reason="rule:hw_thermal_spike (sustained ≥90°C p95)",
            trace="step337: thermal anomaly flag set",
        )

    if dma_fanout >= _TH_DMA_FANOUT:
        return StepResult(
            verdict="REQUIRE_APPROVAL",
            reason=f"rule:hw_dma_fanout (norm={dma_fanout:.2f}, ≥16 peers)",
            trace="step337: high DMA fan-out — possible side channel",
        )

    if ecc_uncorrectable >= _TH_ECC_UNCORR:
        return StepResult(
            verdict="REQUIRE_APPROVAL",
            reason="rule:hw_ecc_uncorrectable (uncorrected memory error)",
            trace="step337: uncorrectable ECC — possible rowhammer or HW fault",
        )

    return StepResult(
        verdict=None, reason="", trace="step337: HW signals nominal"
    )
