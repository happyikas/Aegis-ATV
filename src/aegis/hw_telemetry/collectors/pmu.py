"""CPU Performance Monitoring Unit collector.

Reads ``perf_event_open()``-equivalent counters via Linux ``/proc``
and ``/sys``. We deliberately avoid linking against libperf or the
``perf`` python wrapper — those require ``CAP_PERFMON`` and are
distro-specific. Instead we sample the readily-available
``/proc/stat`` (CPU jiffies) and ``/proc/loadavg`` and infer
instruction-level signal from them. This is coarser than real PMU
counters but enough to populate the ``flops_observed`` /
``gpu_utilization`` proxies for CPU-only nodes.

For NVIDIA / AMD GPU nodes the :class:`NVMLCollector` provides the
authoritative compute metrics; this collector is the CPU-only fallback.
"""

from __future__ import annotations

import os
from pathlib import Path

from aegis.hw_telemetry.collectors.base import CollectorResult, HWCollector


class PMUCollector(HWCollector):
    name = "pmu"

    _PROC_STAT = Path("/proc/stat")
    _PROC_LOADAVG = Path("/proc/loadavg")

    def is_available(self) -> bool:
        return self._PROC_STAT.is_file() and self._PROC_LOADAVG.is_file()

    def collect(self) -> CollectorResult:
        if not self.is_available():
            return CollectorResult(available=False)
        try:
            with self._PROC_STAT.open() as f:
                first = f.readline()
            # /proc/stat first line: cpu user nice system idle iowait irq softirq steal guest guest_nice
            parts = first.split()
            if len(parts) < 5 or parts[0] != "cpu":
                return CollectorResult(available=False)
            user = int(parts[1])
            sys = int(parts[3])
            idle = int(parts[4])
            iowait = int(parts[5]) if len(parts) > 5 else 0
            total = user + sys + idle + iowait
            cpu_util = (user + sys) / total if total > 0 else 0.0

            with self._PROC_LOADAVG.open() as f:
                load = f.readline().split()
            load_1m = float(load[0]) if load else 0.0
            n_cpu = max(1, os.cpu_count() or 1)
            load_norm = min(1.0, load_1m / n_cpu)

            # Project CPU activity into the GPU-shaped slots. A real
            # T3 setup overrides these via NVMLCollector or AegisFPGA.
            return CollectorResult(
                available=True,
                values={
                    "gpu_utilization": cpu_util,           # CPU acts as compute proxy
                    "_pmu_load_1m_normalised": load_norm,  # not in HW band, metadata
                },
                metadata={
                    "cpu_count": n_cpu,
                    "user_jiffies": user,
                    "system_jiffies": sys,
                    "idle_jiffies": idle,
                },
            )
        except OSError:
            return CollectorResult(available=False)
