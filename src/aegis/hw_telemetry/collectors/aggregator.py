"""Collector aggregator — assemble HWCounters from real collectors.

Strategy
--------
1. Probe every collector once at construction. Store the available set.
2. On each ``aggregate(inp)`` call, run each available collector in
   sequence (sub-millisecond each → no need for parallelism in v4.1).
3. Merge results into the :class:`HWCounters` envelope, using the
   v2.3 simulator's modeled value as the **fallback baseline** for
   any slot no collector contributed.

Collector precedence
--------------------
Some slots can be filled by multiple collectors. Hand-tuned priority:

    flops_observed:           NVML > PMU > simulator
    gpu_utilization:          NVML > PMU > simulator
    hbm_bytes_observed:       NVML > simulator
    hbm_utilization:          NVML > simulator
    thermal_celsius_p95:      NVML > BMC (cross-check) > simulator
    network_bytes_in/out:     ethtool > simulator
    dma_fanout:               IOMMU > simulator
    ecc_correctable/uncorr:   EDAC > simulator
    iommu_tag_violations:     AegisFPGA > IOMMU > simulator
    hypervisor_ring_violations: TEE quote > simulator
    watchdog_strikes:         TEE quote > simulator
    dram_access_pattern_entropy: AegisFPGA > simulator

Why fall back to the simulator
------------------------------
The simulator's "honest baseline" gives us a sensible filler so the
ATV HW band is never partially zeroed (which would make the M12
divergence math noisy). When a real collector is available, its
measurement *replaces* the simulator value for that slot.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from aegis.hw_telemetry.collectors.base import CollectorResult, HWCollector
from aegis.hw_telemetry.collectors.bmc_redfish import BMCRedfishCollector
from aegis.hw_telemetry.collectors.edac import EDACCollector
from aegis.hw_telemetry.collectors.ethtool import EthtoolCollector
from aegis.hw_telemetry.collectors.iommu import IOMMUCollector
from aegis.hw_telemetry.collectors.mock_aegis_fpga import MockAegisFPGACollector
from aegis.hw_telemetry.collectors.mock_tee_quote import MockTEEQuoteCollector
from aegis.hw_telemetry.collectors.nvml import NVMLCollector
from aegis.hw_telemetry.collectors.pmu import PMUCollector
from aegis.hw_telemetry.simulator import HWCounters, simulate
from aegis.schema import ATVInput

# Collector merge order — earlier collectors win for shared slots.
_DEFAULT_ORDER: list[type[HWCollector]] = [
    NVMLCollector,
    PMUCollector,
    EDACCollector,
    EthtoolCollector,
    IOMMUCollector,
    BMCRedfishCollector,
    MockAegisFPGACollector,
    MockTEEQuoteCollector,
]


@dataclass(frozen=True)
class AvailabilityReport:
    """Snapshot of which collectors are usable on this host."""

    available: list[str]
    unavailable: list[str]

    def to_dict(self) -> dict[str, list[str]]:
        return {"available": self.available, "unavailable": self.unavailable}


class CollectorAggregator:
    """Run all available collectors and merge into HWCounters.

    Constructor probes once; ``aggregate(inp)`` is sub-millisecond
    per call.
    """

    def __init__(self, collectors: list[HWCollector] | None = None) -> None:
        if collectors is None:
            collectors = [cls() for cls in _DEFAULT_ORDER]
        self._all = collectors
        self._available = [c for c in collectors if c.is_available()]

    def availability_report(self) -> AvailabilityReport:
        available = [c.name for c in self._available]
        unavailable = [c.name for c in self._all if c not in self._available]
        return AvailabilityReport(available=available, unavailable=unavailable)

    def aggregate(self, inp: ATVInput) -> HWCounters:
        """Build a :class:`HWCounters` from the available collectors,
        filling gaps from the v2.3 simulator's honest baseline."""
        # 1. Simulator baseline (honest path; no attack injection).
        baseline = simulate(inp, attack="")

        # 2. Walk available collectors in priority order; first non-zero
        #    contribution per slot wins.
        merged: dict[str, float] = {}
        for collector in self._available:
            try:
                result: CollectorResult = collector.collect()
            except Exception:  # noqa: BLE001 — never let a buggy collector kill the call
                continue
            if not result.available:
                continue
            for k, v in result.values.items():
                if k.startswith("_"):
                    continue  # private metadata-style keys
                merged.setdefault(k, float(v))

        # 3. Build HWCounters; collector value if present, else baseline.
        return HWCounters(
            flops_observed=merged.get("flops_observed", baseline.flops_observed),
            gpu_utilization=merged.get("gpu_utilization", baseline.gpu_utilization),
            hbm_bytes_observed=merged.get("hbm_bytes_observed", baseline.hbm_bytes_observed),
            hbm_utilization=merged.get("hbm_utilization", baseline.hbm_utilization),
            network_bytes_out=merged.get("network_bytes_out", baseline.network_bytes_out),
            network_bytes_in=merged.get("network_bytes_in", baseline.network_bytes_in),
            dma_fanout=int(merged.get("dma_fanout", baseline.dma_fanout)),
            thermal_celsius_p95=merged.get("thermal_celsius_p95", baseline.thermal_celsius_p95),
            ecc_correctable=int(merged.get("ecc_correctable", baseline.ecc_correctable)),
            ecc_uncorrectable=int(merged.get("ecc_uncorrectable", baseline.ecc_uncorrectable)),
            iommu_tag_violations=int(
                merged.get("iommu_tag_violations", baseline.iommu_tag_violations)
            ),
            hypervisor_ring_violations=int(
                merged.get("hypervisor_ring_violations", baseline.hypervisor_ring_violations)
            ),
            watchdog_strikes=int(merged.get("watchdog_strikes", baseline.watchdog_strikes)),
            dram_access_pattern_entropy=merged.get(
                "dram_access_pattern_entropy", baseline.dram_access_pattern_entropy,
            ),
            attack_mode="",
        )


# Process-wide singleton for the env-driven path.
_DEFAULT_AGGREGATOR: CollectorAggregator | None = None


def _get_default_aggregator() -> CollectorAggregator:
    global _DEFAULT_AGGREGATOR
    if _DEFAULT_AGGREGATOR is None:
        _DEFAULT_AGGREGATOR = CollectorAggregator()
    return _DEFAULT_AGGREGATOR


def reset_default_aggregator() -> None:
    """Test helper — drop the cached aggregator."""
    global _DEFAULT_AGGREGATOR
    _DEFAULT_AGGREGATOR = None


def aggregate_from_env(inp: ATVInput) -> HWCounters | None:
    """Convenience entrypoint for ``simulate_from_env`` integration.

    Returns None when ``AEGIS_HW_PROVIDER`` ≠ ``real``. Otherwise runs
    the singleton aggregator.
    """
    if os.environ.get("AEGIS_HW_PROVIDER", "none").lower().strip() != "real":
        return None
    return _get_default_aggregator().aggregate(inp)
