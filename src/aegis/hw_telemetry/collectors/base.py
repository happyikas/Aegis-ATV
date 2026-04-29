"""Collector base contract."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

# Sentinel value: "this collector's interface is not present on the host."
# The aggregator treats UNAVAILABLE as a graceful skip — the v2.3
# simulator baseline (or zero-fill) covers the missing slice.
UNAVAILABLE = object()


@dataclass(frozen=True)
class CollectorResult:
    """One collector's contribution.

    ``values`` is a flat dict of named scalars. The aggregator merges
    them into the appropriate :class:`HWCounters` slots by name.
    Unknown names are silently dropped (forward-compat with future
    collectors that emit slots not yet in the envelope).

    Attributes
    ----------
    available:
        ``False`` when the underlying OS interface was missing or
        permission-denied. ``values`` will be empty.
    values:
        Numeric counter values keyed by the canonical name documented
        in the aggregator (``flops_observed``, ``hbm_bytes_observed``,
        ``thermal_celsius_p95``, etc.).
    metadata:
        Optional collector-specific extras (kernel version, NVML
        driver version, BMC firmware rev, attestation quote bytes,
        etc.). Never feeds the HW band; useful for forensic logs.
    """

    available: bool
    values: dict[str, float] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class HWCollector(Protocol):
    """The interface every collector implements.

    Implementations MUST NOT raise from :meth:`collect`. Any exception
    is the aggregator's signal of a bug, not a degraded environment.
    Use :data:`UNAVAILABLE` semantics (return ``CollectorResult(available=False)``).
    """

    name: str

    def is_available(self) -> bool:
        """Cheap probe — is the underlying interface present?

        Called once at aggregator construction. If False, the collector
        is excluded from the per-call hot path entirely.
        """
        ...

    def collect(self) -> CollectorResult:
        """Read current counters. Must complete in ≤5 ms p99."""
        ...
