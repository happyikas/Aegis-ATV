"""Mock Aegis-FPGA collector.

T2 placeholder for the M21+ custom Aegis silicon — an FPGA
bitstream that tags every DMA descriptor with the agent ID and
records (a) ``aid_tag_transitions``, (b) per-tenant DRAM access
histograms, (c) ``flops_observed`` from a hardware counter pinned
to the FFMA path.

The mock returns deterministic zero values so the firewall keeps
working (a real tampered FPGA would surface non-zero counters; a
T2 host has no FPGA to count anything).

Production swap: read from PCIe MMIO BAR2 (or NVMe-CSD admin
command) to fetch the live counters and signed attestation page.
"""

from __future__ import annotations

from aegis.hw_telemetry.collectors.base import CollectorResult, HWCollector


class MockAegisFPGACollector(HWCollector):
    name = "aegis_fpga"

    def is_available(self) -> bool:
        # Always-on mock so the aggregator can show "FPGA absent" in
        # its report. T3 swap-in detects the actual PCIe BAR.
        return True

    def collect(self) -> CollectorResult:
        return CollectorResult(
            available=True,
            values={
                # Honest defaults for T2:
                # FPGA absent → no counter readings, all zero.
                # `flops_observed` is left to NVML/PMU; this collector
                # contributes only the FPGA-exclusive isolation slots.
                "iommu_tag_violations": 0.0,
                "dram_access_pattern_entropy": 0.0,
            },
            metadata={
                "fpga_provider": "mock",
                "fpga_present": False,
                "bitstream_version": "v0.0",
            },
        )
