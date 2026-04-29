"""HW band encoders (v2.3, T3 emulation).

The ATV-2080 schema reserves 200-D / 11 subfields for hardware-rooted
telemetry (slices :data:`SLICE_MEMORY_TIMING_HISTOGRAMS` …
:data:`SLICE_LINKAGE_CONSISTENCY`). T2 zero-fills them; v2.3 lets a
caller pass an :class:`aegis.hw_telemetry.HWCounters` instance and
populates each subfield with a deterministic projection of the
counters.

The encoders follow the same fixed-slot convention as the SW band:
named keys go in low slots, the rest stay 0. This keeps the audit
signature stable across encoder revisions and lets a verifier
introspect a specific dimension (e.g. *slot 3 of GPU state =
thermal-norm*) without reading the encoder source.

Patent linkage:

* ``hw_cost_attestation`` — Claim 30 + Claim 26: last 3 slots carry
  the canonical j-14 / j-15 / j-16 divergence values so the
  cryptographic record is self-attesting (the SW vendor's signature
  on the SW band + the HW vendor's signature on the HW band, with
  the divergence numerically inside the HW band).
* ``aid_tag_transitions`` / ``atmu_anomaly`` / ``hypervisor_signals``
  — Claim 5: enforces the AID isolation contract at memory-controller
  granularity. Non-zero slots here are the smoking gun for
  cross-tenant memory access.
"""

from __future__ import annotations

import numpy as np

from aegis.cost.divergence import compute_divergence
from aegis.hw_telemetry import HWCounters
from aegis.schema import (
    SLICE_AID_TAG_TRANSITIONS,
    SLICE_ATMU_ANOMALY,
    SLICE_DMA_FANOUT,
    SLICE_GPU_ACCELERATOR_STATE,
    SLICE_HW_COST_ATTESTATION,
    SLICE_HYPERVISOR_SIGNALS,
    SLICE_LINKAGE_CONSISTENCY,
    SLICE_MEMORY_TIMING_HISTOGRAMS,
    SLICE_NETWORK_TELEMETRY,
    SLICE_THERMAL_ECC_DRIFT,
    SLICE_WATCHDOG_SIGNALS,
    ATVInput,
    CostEfficiencyMetrics,
)


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def encode_memory_timing_histograms(hw: HWCounters) -> np.ndarray:
    """32-D — DRAM access pattern histogram.

    Slot 0 = entropy (low = repetitive pattern, high = scattered).
    Slot 1 = HBM utilisation. Slots 2..31 = 30-bin uniform histogram
    derived from the entropy + HBM utilisation; high-entropy + high
    utilisation produces a flat tail (anomaly indicator).
    """
    arr = np.zeros(32, dtype=np.float32)
    arr[0] = _clamp01(hw.dram_access_pattern_entropy)
    arr[1] = _clamp01(hw.hbm_utilization)
    # Tail: 30 bins. Each bin's value is entropy × util × position-decay.
    decay = np.linspace(1.0, 0.1, 30, dtype=np.float32)
    arr[2:32] = arr[0] * arr[1] * decay
    return arr


def encode_aid_tag_transitions(hw: HWCounters) -> np.ndarray:
    """24-D — IOMMU AID-tag transitions per memory channel.

    Slot 0 = total tag violations (Claim 5 enforcement target).
    Slot 1 = hypervisor ring violations.
    Slots 2..23 = position-encoded violation severity.
    """
    arr = np.zeros(24, dtype=np.float32)
    arr[0] = _clamp01(hw.iommu_tag_violations / 5.0)
    arr[1] = _clamp01(hw.hypervisor_ring_violations / 3.0)
    if hw.iommu_tag_violations > 0 or hw.hypervisor_ring_violations > 0:
        # Spread the alarm across the array so a renderer's heatmap shows
        # the breach without zooming.
        arr[2:24] = (arr[0] + arr[1]) * 0.5
    return arr


def encode_atmu_anomaly(hw: HWCounters) -> np.ndarray:
    """16-D — ATMU (Agent Telemetry Management Unit) anomaly slots
    (M10 patent §[0063] + Claim 18)."""
    arr = np.zeros(16, dtype=np.float32)
    arr[0] = _clamp01(hw.iommu_tag_violations / 5.0)
    arr[1] = _clamp01(hw.watchdog_strikes / 5.0)
    arr[2] = _clamp01(hw.hypervisor_ring_violations / 3.0)
    arr[3] = _clamp01(hw.ecc_uncorrectable / 1.0)
    return arr


def encode_dma_fanout(hw: HWCounters) -> np.ndarray:
    """16-D — DMA peer count + entropy.

    High fan-out (e.g. 32 distinct peers for one tool call) suggests
    side-channel exfil: a benign read_file shouldn't be DMA'ing to
    32 different network endpoints.
    """
    arr = np.zeros(16, dtype=np.float32)
    arr[0] = _clamp01(hw.dma_fanout / 32.0)
    arr[1] = _clamp01(hw.network_bytes_out / 50_000_000.0)  # 50 MB norm
    arr[2] = _clamp01(hw.network_bytes_in / 50_000_000.0)
    return arr


def encode_thermal_ecc_drift(hw: HWCounters) -> np.ndarray:
    """16-D — thermal envelope + ECC drift."""
    arr = np.zeros(16, dtype=np.float32)
    arr[0] = _clamp01(hw.thermal_celsius_p95 / 100.0)
    arr[1] = _clamp01(hw.ecc_correctable / 100.0)
    arr[2] = _clamp01(hw.ecc_uncorrectable / 1.0)
    # Slot 3 = thermal-anomaly flag (>= 90°C sustained)
    arr[3] = 1.0 if hw.thermal_celsius_p95 >= 90.0 else 0.0
    return arr


def encode_watchdog_signals(hw: HWCounters) -> np.ndarray:
    """12-D — heartbeat / liveness signals."""
    arr = np.zeros(12, dtype=np.float32)
    arr[0] = _clamp01(hw.watchdog_strikes / 5.0)
    return arr


def encode_network_telemetry(hw: HWCounters) -> np.ndarray:
    """24-D — egress / ingress / fan-out + log-scale total."""
    arr = np.zeros(24, dtype=np.float32)
    arr[0] = _clamp01(hw.network_bytes_out / 50_000_000.0)
    arr[1] = _clamp01(hw.network_bytes_in / 50_000_000.0)
    arr[2] = _clamp01(hw.dma_fanout / 32.0)
    # Slot 3: log-scale total bytes (1 MB → 0.5, 50 MB → 1.0)
    total = hw.network_bytes_out + hw.network_bytes_in
    arr[3] = _clamp01(np.log1p(total / 1_000_000.0) / 4.0)
    # Slot 4: egress-to-ingress ratio (high = exfil-shaped)
    if hw.network_bytes_in > 0:
        ratio = hw.network_bytes_out / max(hw.network_bytes_in, 1.0)
        arr[4] = _clamp01(ratio / 100.0)
    return arr


def encode_gpu_accelerator_state(hw: HWCounters) -> np.ndarray:
    """16-D — GPU/accelerator runtime state."""
    arr = np.zeros(16, dtype=np.float32)
    arr[0] = _clamp01(hw.gpu_utilization)
    arr[1] = _clamp01(hw.hbm_utilization)
    arr[2] = _clamp01(hw.thermal_celsius_p95 / 100.0)
    # Slot 3: log-scale FLOPS (1 TFLOP → 0.5, 100 TFLOP → 1.0)
    arr[3] = _clamp01(float(np.log1p(hw.flops_observed / 1e12)) / 6.0)
    # Slot 4: ECC corrected per-call
    arr[4] = _clamp01(hw.ecc_correctable / 100.0)
    return arr


def encode_hypervisor_signals(hw: HWCounters) -> np.ndarray:
    """8-D — hypervisor / VM-exit signals."""
    arr = np.zeros(8, dtype=np.float32)
    arr[0] = _clamp01(hw.hypervisor_ring_violations / 3.0)
    arr[1] = _clamp01(hw.iommu_tag_violations / 5.0)
    return arr


def encode_hw_cost_attestation(
    hw: HWCounters, sw_metrics: CostEfficiencyMetrics, model_name: str
) -> np.ndarray:
    """16-D — HW-rooted cost attestation, mirrors SW cost_efficiency_metrics.

    Slots 0..12 carry the HW-side analog of the SW cost slots
    (FLOPS, HBM, network, thermal, ECC, …). Slots 13/14/15 carry the
    j-14 / j-15 / j-16 **divergence** values per Claim 26 — these are
    what step340 / step335 read for the escalation gate.
    """
    arr = np.zeros(16, dtype=np.float32)
    arr[0] = _clamp01(float(np.log1p(hw.flops_observed / 1e12)) / 6.0)
    arr[1] = _clamp01(hw.hbm_bytes_observed / 1e10)        # 10 GB norm
    arr[2] = _clamp01(hw.network_bytes_out / 50_000_000.0)
    arr[3] = _clamp01(hw.gpu_utilization)
    arr[4] = _clamp01(hw.hbm_utilization)
    arr[5] = _clamp01(hw.thermal_celsius_p95 / 100.0)
    arr[6] = _clamp01(hw.ecc_correctable / 100.0)
    arr[7] = _clamp01(hw.iommu_tag_violations / 5.0)
    arr[8] = _clamp01(hw.hypervisor_ring_violations / 3.0)
    arr[9] = _clamp01(hw.dma_fanout / 32.0)
    # 10..12 reserved for future HW vendor-specific signals
    div = compute_divergence(
        sw_metrics,
        model_name=model_name,
        hw_flops_observed=hw.flops_observed,
        hw_hbm_bytes_observed=hw.hbm_bytes_observed,
    )
    arr[13] = float(div.token_to_flops)   # j-14
    arr[14] = float(div.memory_cost)       # j-15
    arr[15] = float(div.dollar_cost)       # j-16
    return arr


def encode_linkage_consistency(
    hw: HWCounters, sw_metrics: CostEfficiencyMetrics, model_name: str
) -> np.ndarray:
    """20-D — SW↔HW linkage consistency score.

    Inverse of divergence: 1.0 = perfect agreement, 0.0 = diverged.
    A zero-floored score in slot 0 is the single number a renderer
    can put on a dashboard's 'attestation match' gauge.
    """
    arr = np.zeros(20, dtype=np.float32)
    div = compute_divergence(
        sw_metrics,
        model_name=model_name,
        hw_flops_observed=hw.flops_observed,
        hw_hbm_bytes_observed=hw.hbm_bytes_observed,
    )
    # Per-metric agreement (1 - divergence)
    arr[0] = 1.0 - div.token_to_flops
    arr[1] = 1.0 - div.memory_cost
    arr[2] = 1.0 - div.dollar_cost
    # Composite — geometric mean style (sensitive to the worst gap)
    arr[3] = arr[0] * arr[1] * arr[2]
    return arr


def fill_hw_band(atv: np.ndarray, inp: ATVInput, hw: HWCounters) -> None:
    """Populate the 11 HW subfields in-place. Caller pre-zeros the band."""
    model = inp.header.model_hash or "default"
    atv[SLICE_MEMORY_TIMING_HISTOGRAMS] = encode_memory_timing_histograms(hw)
    atv[SLICE_AID_TAG_TRANSITIONS] = encode_aid_tag_transitions(hw)
    atv[SLICE_ATMU_ANOMALY] = encode_atmu_anomaly(hw)
    atv[SLICE_DMA_FANOUT] = encode_dma_fanout(hw)
    atv[SLICE_THERMAL_ECC_DRIFT] = encode_thermal_ecc_drift(hw)
    atv[SLICE_WATCHDOG_SIGNALS] = encode_watchdog_signals(hw)
    atv[SLICE_NETWORK_TELEMETRY] = encode_network_telemetry(hw)
    atv[SLICE_GPU_ACCELERATOR_STATE] = encode_gpu_accelerator_state(hw)
    atv[SLICE_HYPERVISOR_SIGNALS] = encode_hypervisor_signals(hw)
    atv[SLICE_HW_COST_ATTESTATION] = encode_hw_cost_attestation(hw, inp.cost_estimate, model)
    atv[SLICE_LINKAGE_CONSISTENCY] = encode_linkage_consistency(hw, inp.cost_estimate, model)
