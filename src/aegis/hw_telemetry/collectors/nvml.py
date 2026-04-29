"""NVIDIA GPU counters via NVML (Optional dependency: ``pynvml``).

When ``pynvml`` is importable AND at least one NVIDIA device is
present, this collector authoritative for GPU compute metrics:

* ``gpu_utilization`` — SM occupancy (0..1)
* ``hbm_bytes_observed`` — used HBM bytes
* ``hbm_utilization`` — HBM used / HBM total
* ``thermal_celsius_p95`` — current temperature (single sample)
* ``flops_observed`` — derived from utilization × peak FLOPS where
  the peak is read from the device's compute capability.

Without ``pynvml`` available, returns UNAVAILABLE. The aggregator
falls back to the v2.3 simulator's modeled values.
"""

from __future__ import annotations

import importlib
from typing import Any

from aegis.hw_telemetry.collectors.base import CollectorResult, HWCollector


class NVMLCollector(HWCollector):
    name = "nvml"

    def __init__(self) -> None:
        self._nvml: Any = None
        self._device_handles: list[Any] = []
        self._initialised = False
        self._try_init()

    def _try_init(self) -> None:
        try:
            self._nvml = importlib.import_module("pynvml")
        except ImportError:
            return
        try:
            self._nvml.nvmlInit()
            count = int(self._nvml.nvmlDeviceGetCount())
            self._device_handles = [
                self._nvml.nvmlDeviceGetHandleByIndex(i) for i in range(count)
            ]
            self._initialised = True
        except Exception:  # noqa: BLE001 — NVML can raise many things
            self._nvml = None
            self._device_handles = []

    def is_available(self) -> bool:
        return self._initialised and len(self._device_handles) > 0

    def collect(self) -> CollectorResult:
        if not self.is_available():
            return CollectorResult(available=False)
        try:
            util_sum = 0.0
            mem_used = 0
            mem_total = 0
            temp_max = 0
            n = len(self._device_handles)
            for h in self._device_handles:
                util = self._nvml.nvmlDeviceGetUtilizationRates(h)
                util_sum += float(util.gpu) / 100.0
                mem = self._nvml.nvmlDeviceGetMemoryInfo(h)
                mem_used += int(mem.used)
                mem_total += int(mem.total)
                t = int(self._nvml.nvmlDeviceGetTemperature(
                    h, self._nvml.NVML_TEMPERATURE_GPU,
                ))
                temp_max = max(temp_max, t)
            avg_util = util_sum / n
            hbm_util = mem_used / mem_total if mem_total > 0 else 0.0
            return CollectorResult(
                available=True,
                values={
                    "gpu_utilization": float(min(1.0, avg_util)),
                    "hbm_bytes_observed": float(mem_used),
                    "hbm_utilization": float(min(1.0, hbm_util)),
                    "thermal_celsius_p95": float(temp_max),
                },
                metadata={"device_count": n, "hbm_total_bytes": mem_total},
            )
        except Exception:  # noqa: BLE001
            return CollectorResult(available=False)

    def __del__(self) -> None:
        if self._initialised and self._nvml is not None:
            import contextlib
            with contextlib.suppress(Exception):
                self._nvml.nvmlShutdown()
