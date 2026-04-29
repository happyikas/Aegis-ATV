"""DRAM ECC error counter via Linux EDAC subsystem.

EDAC exposes per-memory-controller ECC counts at
``/sys/devices/system/edac/mc/mc*/`` :

* ``ce_count``  — correctable errors
* ``ue_count``  — uncorrectable errors

We aggregate across all controllers. ``ue_count`` jumping by even one
is a critical-grade signal. Both feed :data:`HWCounters.ecc_correctable`
and :data:`HWCounters.ecc_uncorrectable`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from aegis.hw_telemetry.collectors.base import CollectorResult, HWCollector


class EDACCollector(HWCollector):
    name = "edac"

    _EDAC_ROOT = Path("/sys/devices/system/edac/mc")

    def is_available(self) -> bool:
        return self._EDAC_ROOT.is_dir()

    def collect(self) -> CollectorResult:
        if not self.is_available():
            return CollectorResult(available=False)
        ce_total = 0
        ue_total = 0
        controllers: list[dict[str, Any]] = []
        try:
            for mc_dir in sorted(self._EDAC_ROOT.glob("mc*")):
                if not mc_dir.is_dir():
                    continue
                ce = self._read_int(mc_dir / "ce_count")
                ue = self._read_int(mc_dir / "ue_count")
                if ce is not None:
                    ce_total += ce
                if ue is not None:
                    ue_total += ue
                controllers.append({
                    "name": mc_dir.name, "ce_count": ce, "ue_count": ue,
                })
        except OSError:
            return CollectorResult(available=False)

        return CollectorResult(
            available=True,
            values={
                "ecc_correctable": float(ce_total),
                "ecc_uncorrectable": float(ue_total),
            },
            metadata={"controllers": controllers},
        )

    @staticmethod
    def _read_int(path: Path) -> int | None:
        try:
            with path.open() as f:
                return int(f.read().strip())
        except (OSError, ValueError):
            return None
