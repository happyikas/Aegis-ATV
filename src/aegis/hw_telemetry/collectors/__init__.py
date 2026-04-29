"""Hardware telemetry collectors (v4.1, Claim 55).

T2-implementable subset of the T3 HW capture surface. Each collector
reads from a standard host OS interface (procfs, sysfs, NVML, ethtool,
IOReport, Redfish) and contributes a normalised slice of the
:class:`aegis.hw_telemetry.HWCounters` envelope.

Collectors must:
1. Implement :class:`HWCollector` (a Protocol).
2. Return :data:`UNAVAILABLE` from :meth:`collect` when the underlying
   interface is missing — never raise. The aggregator treats this as
   "this counter family stays at the v2.3 simulator baseline" so the
   firewall keeps working in degraded environments.
3. Be deterministic given identical input where possible. Counters
   that read live OS state are inherently non-deterministic; that's
   fine — they replace the simulator's *jitter*, not its envelope.

T3 swap-in
----------
Once silicon arrives (M19+), each ``Mock*`` collector can be replaced
in-place by a real backend without touching the aggregator or
``simulate_from_env`` callers. The :class:`HWCollector` Protocol is
the contract.
"""

from __future__ import annotations

from aegis.hw_telemetry.collectors.aggregator import (
    AvailabilityReport,
    CollectorAggregator,
    aggregate_from_env,
)
from aegis.hw_telemetry.collectors.base import (
    UNAVAILABLE,
    CollectorResult,
    HWCollector,
)
from aegis.hw_telemetry.collectors.bmc_redfish import BMCRedfishCollector
from aegis.hw_telemetry.collectors.edac import EDACCollector
from aegis.hw_telemetry.collectors.ethtool import EthtoolCollector
from aegis.hw_telemetry.collectors.iommu import IOMMUCollector
from aegis.hw_telemetry.collectors.mock_aegis_fpga import MockAegisFPGACollector
from aegis.hw_telemetry.collectors.mock_tee_quote import MockTEEQuoteCollector
from aegis.hw_telemetry.collectors.nvml import NVMLCollector
from aegis.hw_telemetry.collectors.pmu import PMUCollector

__all__ = [
    "UNAVAILABLE",
    "AvailabilityReport",
    "BMCRedfishCollector",
    "CollectorAggregator",
    "CollectorResult",
    "EDACCollector",
    "EthtoolCollector",
    "HWCollector",
    "IOMMUCollector",
    "MockAegisFPGACollector",
    "MockTEEQuoteCollector",
    "NVMLCollector",
    "PMUCollector",
    "aggregate_from_env",
]
