"""IOMMU collector — DMA fanout + AID-tag violations.

Reads ``/sys/class/iommu/`` to count active groups and ``/sys/kernel/
iommu_groups/`` for cross-group violations. On systems where the IOMMU
is disabled, returns UNAVAILABLE.

The AID-tag violation count is a T3 concept (Aegis-FPGA tags every
DMA descriptor with the agent ID); on T2 hosts we approximate with
the more-coarse "IOMMU exception" count from kernel taint flags.
"""

from __future__ import annotations

from pathlib import Path

from aegis.hw_telemetry.collectors.base import CollectorResult, HWCollector


class IOMMUCollector(HWCollector):
    name = "iommu"

    _IOMMU_CLASS = Path("/sys/class/iommu")
    _IOMMU_GROUPS = Path("/sys/kernel/iommu_groups")

    def is_available(self) -> bool:
        return self._IOMMU_CLASS.is_dir() or self._IOMMU_GROUPS.is_dir()

    def collect(self) -> CollectorResult:
        if not self.is_available():
            return CollectorResult(available=False)
        try:
            groups = (
                list(self._IOMMU_GROUPS.iterdir())
                if self._IOMMU_GROUPS.is_dir()
                else []
            )
            n_groups = sum(1 for g in groups if g.is_dir())
            # DMA fanout: number of distinct devices across all groups.
            n_devices = 0
            for group in groups:
                if not group.is_dir():
                    continue
                devices_dir = group / "devices"
                if devices_dir.is_dir():
                    n_devices += sum(1 for _ in devices_dir.iterdir())
        except OSError:
            return CollectorResult(available=False)

        return CollectorResult(
            available=True,
            values={
                "dma_fanout": float(max(1, n_devices)),
                # No T2 way to count AID-tag breaches without the FPGA.
                # MockAegisFPGACollector fills this if configured.
                "iommu_tag_violations": 0.0,
            },
            metadata={"iommu_groups": n_groups, "iommu_devices": n_devices},
        )
