"""Hardware telemetry — T3 tier.

Provides the physical-counter side of AegisData's two-axis double-check
model. The agent's SW-reported cost / behavior (ATV SW band, 1880-D)
is compared against hardware-rooted measurements (ATV HW band, 200-D)
in :mod:`aegis.cost.divergence`; when the two diverge above a baseline
multiplier, :mod:`aegis.cost.escalation` flips the verdict to
REQUIRE_APPROVAL independent of the sLLM judge (patent Claim 27).

Two providers selected by ``AEGIS_HW_PROVIDER``:

* ``none`` (default) — HW band stays zero-filled (T2 behavior).
  ``compute_divergence`` returns 0 for every metric. Existing M1–M17
  test surface and v2.0/v2.2 sidecar runs all use this.

* ``sim`` — Software emulator (this module) produces deterministic
  HW counters seeded by SHA3 of (tool, args, aid). Realistic noise
  ±10% around the SW-expected baseline so divergence stays tiny on
  honest agents. ``AEGIS_HW_INJECT_ATTACK`` rewrites a chosen counter
  to a divergent value so the escalation gate fires — used to demo
  the two-axis defense without a real Linux server / TDX VM / FPGA /
  IOMMU / CSD eval kit (M19–M22).

The simulator is the bridge between v2.2 ("HW band 0-fill, contract
ready") and a future T3 ("HW counters from real silicon"). It can
be replaced one provider at a time as physical telemetry lands.
"""

from __future__ import annotations

from aegis.hw_telemetry.simulator import (
    ATTACK_MODES,
    HWCounters,
    simulate,
    simulate_from_env,
)

__all__ = [
    "ATTACK_MODES",
    "HWCounters",
    "simulate",
    "simulate_from_env",
]
