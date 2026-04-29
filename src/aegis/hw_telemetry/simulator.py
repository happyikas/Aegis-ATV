"""HW telemetry simulator (v2.3, T3 emulation).

Produces deterministic, realistic-ish hardware counters for one tool
invocation. The simulator is **not** a substitute for real silicon —
it's the SW emulation that lets MVP demo the two-axis double-check
*shape* (Claims 26 / 27 / 30 / 34) before M19–M22 land.

Determinism:
    The same (tool, args_canonical_json, aid) always produces the same
    HWCounters. This makes tests reproducible and lets a CI replay one
    transcript and get bit-identical ATV HW band content.

Realism (honest agent path):
    Each counter is the SW-expected baseline (e.g. ``expected_flops``
    for the named model + token count) jittered by ±10%. The
    divergence math (M12) reads <0.10 — well below the default 0.30
    threshold (DEFAULT_BASELINE × ESCALATION_MULTIPLIER) — so the
    escalation gate stays quiet on honest traffic.

Attack injection (compromised agent path):
    ``AEGIS_HW_INJECT_ATTACK=<mode>`` rewrites one or more counters
    to a divergent value so the escalation gate fires:

    * ``token_flops_mismatch`` — HW FLOPS = 50× SW expected → catches
      a model substitution or hidden background work.
    * ``hbm_exfil``            — HBM bytes = 30× SW expected → catches
      a side-channel that streams large data through HBM that the SW
      token report doesn't reflect.
    * ``cost_underreport``     — HW FLOPS = 100× while SW dollar stays
      the same → catches a billing-evasion compromise.
    * ``thermal_spike``        — sustained 95°C p95 + ECC spikes →
      hidden background work.
    * ``network_exfil``        — 50 MB egress on a "read_file" call →
      side-channel exfil.
    * ``iommu_violation``      — IOMMU AID-tag breaches → cross-tenant
      memory access (Claim 5 enforcement target).

Multiple attack modes can be combined comma-separated (e.g.
``token_flops_mismatch,network_exfil``). Unknown modes are ignored
so dev environments don't crash on typos.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass

from aegis.cost.divergence import HBM_BYTES_PER_TOKEN
from aegis.cost.model_flops import expected_flops
from aegis.schema import ATVInput


# ─────────────────────────────────────────────────────────────────────
# HW counter envelope
# ─────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class HWCounters:
    """One snapshot of physical telemetry for a single tool invocation.

    All units are explicit. Zero values are valid (counter wasn't
    touched). Negative values are not allowed — callers should clamp.
    """

    # ── Compute ───────────────────────────────────────────────────────
    flops_observed: float          # GPU FLOPS over the call window
    gpu_utilization: float         # 0..1 fraction of SM occupancy

    # ── Memory ────────────────────────────────────────────────────────
    hbm_bytes_observed: float      # HBM bytes touched (KV cache + activations)
    hbm_utilization: float         # 0..1 fraction of HBM capacity in use

    # ── Network ───────────────────────────────────────────────────────
    network_bytes_out: float       # bytes egressing the node
    network_bytes_in: float
    dma_fanout: int                # number of distinct DMA target endpoints

    # ── Thermal / ECC ─────────────────────────────────────────────────
    thermal_celsius_p95: float     # GPU temp p95 (°C)
    ecc_correctable: int           # ECC events corrected
    ecc_uncorrectable: int

    # ── Isolation / hypervisor ────────────────────────────────────────
    iommu_tag_violations: int      # AID-tag breaches by memory controller
    hypervisor_ring_violations: int  # ring-0 attempts from VM
    watchdog_strikes: int          # heartbeat misses

    # ── Memory access pattern (32-bin histogram source) ──────────────
    dram_access_pattern_entropy: float  # 0..1, low = pattern-y, high = random

    # ── Attestation linkage ──────────────────────────────────────────
    attack_mode: str               # comma-separated injected attacks (audit hint)


# ─────────────────────────────────────────────────────────────────────
# Attack catalogue — keep in sync with the docstring above.
# ─────────────────────────────────────────────────────────────────────
ATTACK_MODES: frozenset[str] = frozenset({
    "token_flops_mismatch",
    "hbm_exfil",
    "cost_underreport",
    "thermal_spike",
    "network_exfil",
    "iommu_violation",
})


# ─────────────────────────────────────────────────────────────────────
# Deterministic jitter helper (no numpy dep — keeps simulator stdlib).
# ─────────────────────────────────────────────────────────────────────
def _seeded_floats(seed: bytes, n: int) -> list[float]:
    """Return ``n`` floats in [0.0, 1.0) deterministic from ``seed``."""
    out: list[float] = []
    counter = 0
    while len(out) < n:
        digest = hashlib.sha3_512(seed + counter.to_bytes(4, "big")).digest()
        # Each digest gives 8 floats (8 bytes per float, 64 bytes total).
        for i in range(0, 64, 8):
            chunk = digest[i : i + 8]
            val = int.from_bytes(chunk, "big") / 2**64
            out.append(val)
            if len(out) >= n:
                break
        counter += 1
    return out[:n]


def _jitter(value: float, fraction: float, jitter_sample: float) -> float:
    """Multiplicative jitter — value × (1 + fraction × (2*jitter_sample − 1))."""
    return max(0.0, value * (1.0 + fraction * (2 * jitter_sample - 1)))


# ─────────────────────────────────────────────────────────────────────
# Simulator core
# ─────────────────────────────────────────────────────────────────────
def simulate(inp: ATVInput, *, attack: str = "") -> HWCounters:
    """Produce HWCounters for ``inp`` deterministically.

    ``attack`` is a comma-separated list of mode names (see
    :data:`ATTACK_MODES`). Unknown names are silently ignored so an
    env-var typo doesn't crash a Claude Code session.
    """
    aid = inp.header.aid or "default"
    seed_text = f"{inp.tool_name}|{inp.tool_args_json}|{aid}".encode()
    seed = hashlib.sha3_256(seed_text).digest()
    j = _seeded_floats(seed, 16)  # 16 jitter samples

    model = inp.header.model_hash or "default"
    in_tokens = float(inp.cost_estimate.input_token_count)
    out_tokens = float(inp.cost_estimate.output_token_count)
    cum_tokens = float(inp.cost_estimate.cumulative_tokens or (in_tokens + out_tokens))

    # ── Honest-agent baseline ────────────────────────────────────────
    expected_f = expected_flops(model, in_tokens, out_tokens)
    expected_hbm = max(1024.0, cum_tokens * HBM_BYTES_PER_TOKEN)

    flops = _jitter(expected_f, 0.10, j[0])              # ±10% noise
    hbm = _jitter(expected_hbm, 0.08, j[1])
    gpu_util = _jitter(0.45, 0.30, j[2])                  # 0.45 ± 30%
    hbm_util = _jitter(0.30, 0.30, j[3])

    net_in = _jitter(2048.0, 0.50, j[4])                  # ~2 KB request frame
    net_out = _jitter(8192.0, 0.50, j[5])                 # ~8 KB response
    dma_fanout = max(1, int(_jitter(2.0, 0.50, j[6])))

    thermal = _jitter(62.0, 0.10, j[7])                   # 62°C ± 10%
    ecc_corr = max(0, int(_jitter(1.0, 1.0, j[8])))
    ecc_uncorr = 0

    iommu_tag = 0
    hyp_ring = 0
    watchdog = max(0, int(_jitter(0.2, 1.0, j[9])))

    dram_entropy = _jitter(0.55, 0.20, j[10])

    # ── Attack injection ─────────────────────────────────────────────
    requested = {m.strip() for m in attack.split(",") if m.strip()}
    applied = sorted(requested & ATTACK_MODES)

    if "token_flops_mismatch" in applied:
        flops = expected_f * 50.0 + 1.0      # 50× SW expected
        gpu_util = 0.99
    if "hbm_exfil" in applied:
        hbm = expected_hbm * 30.0 + 1024.0   # 30× SW expected
        hbm_util = 0.95
    if "cost_underreport" in applied:
        flops = max(flops, expected_f * 100.0 + 1.0)
        gpu_util = 0.99
    if "thermal_spike" in applied:
        thermal = 96.0
        ecc_corr = 250
    if "network_exfil" in applied:
        net_out = 50_000_000.0               # 50 MB egress
        dma_fanout = 32
    if "iommu_violation" in applied:
        iommu_tag = 5
        hyp_ring = 2

    return HWCounters(
        flops_observed=flops,
        gpu_utilization=min(1.0, gpu_util),
        hbm_bytes_observed=hbm,
        hbm_utilization=min(1.0, hbm_util),
        network_bytes_out=net_out,
        network_bytes_in=net_in,
        dma_fanout=dma_fanout,
        thermal_celsius_p95=thermal,
        ecc_correctable=ecc_corr,
        ecc_uncorrectable=ecc_uncorr,
        iommu_tag_violations=iommu_tag,
        hypervisor_ring_violations=hyp_ring,
        watchdog_strikes=watchdog,
        dram_access_pattern_entropy=min(1.0, dram_entropy),
        attack_mode=",".join(applied),
    )


def simulate_from_env(inp: ATVInput) -> HWCounters | None:
    """Convenience for callers that want env-driven on/off + attack injection.

    ``AEGIS_HW_PROVIDER`` selects the source:

    * ``none`` (default) — return None; HW band stays zero-filled.
    * ``sim`` — run the v2.3 deterministic simulator.
      ``AEGIS_HW_INJECT_ATTACK`` (comma-separated mode list) injects
      the chosen attacks for demo / regression testing.
    * ``real`` (v4.1) — run the
      :class:`aegis.hw_telemetry.collectors.CollectorAggregator`
      against the real host's PMU / EDAC / NVML / ethtool / IOMMU /
      BMC interfaces, falling back to the simulator baseline for any
      slot no collector covers. Returns the aggregated counters.

    The existing T2 zero-fill path in :func:`aegis.atv.builder.build_atv`
    is preserved when provider is ``none``.
    """
    provider = os.environ.get("AEGIS_HW_PROVIDER", "none").lower().strip()
    if provider == "sim":
        attack = os.environ.get("AEGIS_HW_INJECT_ATTACK", "")
        return simulate(inp, attack=attack)
    if provider == "real":
        # Lazy import to avoid forcing collectors module load when not used
        from aegis.hw_telemetry.collectors import aggregate_from_env as _agg
        return _agg(inp)
    return None
