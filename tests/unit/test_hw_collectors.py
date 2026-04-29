"""Unit tests for src/aegis/hw_telemetry/collectors/* (v4.1, Claim 55)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from aegis.hw_telemetry.collectors import (
    AvailabilityReport,
    BMCRedfishCollector,
    CollectorAggregator,
    CollectorResult,
    EDACCollector,
    EthtoolCollector,
    HWCollector,
    IOMMUCollector,
    MockAegisFPGACollector,
    MockTEEQuoteCollector,
    NVMLCollector,
    PMUCollector,
    aggregate_from_env,
)
from aegis.hw_telemetry.collectors.aggregator import reset_default_aggregator
from aegis.hw_telemetry.simulator import HWCounters
from aegis.schema import ATVHeader, ATVInput, CostEfficiencyMetrics

# ─────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────


def _atv_input(*, tool: str = "Bash") -> ATVInput:
    return ATVInput(
        header=ATVHeader(
            trace_id="t" * 32, span_id="s" * 16,
            tenant_id="demo", aid="agent-test", timestamp_ns=0,
            model_hash="claude-haiku-4-5",
        ),
        tool_name=tool,
        tool_args_json=json.dumps({"command": "ls"}),
        cost_estimate=CostEfficiencyMetrics(
            input_token_count=1000.0, output_token_count=500.0,
            cumulative_tokens=1500.0, cumulative_dollars=0.0001,
        ),
    )


@pytest.fixture(autouse=True)
def _reset_aggregator() -> None:
    reset_default_aggregator()
    yield
    reset_default_aggregator()


# ─────────────────────────────────────────────────────────────────────
# CollectorResult / Protocol
# ─────────────────────────────────────────────────────────────────────


def test_collector_result_default_empty() -> None:
    r = CollectorResult(available=False)
    assert r.values == {}
    assert r.metadata == {}


def test_protocol_recognises_collector() -> None:
    """All concrete collectors must be HWCollector."""
    for cls in (PMUCollector, EDACCollector, IOMMUCollector,
                EthtoolCollector, NVMLCollector, BMCRedfishCollector,
                MockTEEQuoteCollector, MockAegisFPGACollector):
        c = cls()
        assert isinstance(c, HWCollector)
        assert isinstance(c.name, str) and len(c.name) > 0


# ─────────────────────────────────────────────────────────────────────
# PMU collector
# ─────────────────────────────────────────────────────────────────────


def test_pmu_unavailable_when_proc_missing(tmp_path: Path) -> None:
    c = PMUCollector()
    with (
        patch.object(PMUCollector, "_PROC_STAT", tmp_path / "no_stat"),
        patch.object(PMUCollector, "_PROC_LOADAVG", tmp_path / "no_loadavg"),
    ):
        assert c.is_available() is False
        r = c.collect()
        assert r.available is False


def test_pmu_parses_proc_stat(tmp_path: Path) -> None:
    proc_stat = tmp_path / "stat"
    proc_loadavg = tmp_path / "loadavg"
    proc_stat.write_text("cpu  100 0 50 1000 0 0 0\n")
    proc_loadavg.write_text("0.5 0.2 0.1 1/100 12345\n")

    c = PMUCollector()
    with (
        patch.object(PMUCollector, "_PROC_STAT", proc_stat),
        patch.object(PMUCollector, "_PROC_LOADAVG", proc_loadavg),
    ):
        r = c.collect()
    assert r.available is True
    # cpu_util = (user+sys) / (user+sys+idle+iowait) = 150/1150 ≈ 0.13
    assert 0.10 < r.values["gpu_utilization"] < 0.20
    assert r.metadata["cpu_count"] >= 1


# ─────────────────────────────────────────────────────────────────────
# EDAC collector
# ─────────────────────────────────────────────────────────────────────


def test_edac_unavailable_without_root(tmp_path: Path) -> None:
    c = EDACCollector()
    with patch.object(EDACCollector, "_EDAC_ROOT", tmp_path / "no_edac"):
        assert c.is_available() is False
        r = c.collect()
        assert r.available is False


def test_edac_aggregates_across_controllers(tmp_path: Path) -> None:
    edac = tmp_path / "edac"
    edac.mkdir()
    (edac / "mc0").mkdir()
    (edac / "mc0" / "ce_count").write_text("5\n")
    (edac / "mc0" / "ue_count").write_text("0\n")
    (edac / "mc1").mkdir()
    (edac / "mc1" / "ce_count").write_text("3\n")
    (edac / "mc1" / "ue_count").write_text("1\n")

    c = EDACCollector()
    with patch.object(EDACCollector, "_EDAC_ROOT", edac):
        r = c.collect()
    assert r.available is True
    assert r.values["ecc_correctable"] == 8.0  # 5 + 3
    assert r.values["ecc_uncorrectable"] == 1.0


# ─────────────────────────────────────────────────────────────────────
# IOMMU collector
# ─────────────────────────────────────────────────────────────────────


def test_iommu_unavailable_without_class(tmp_path: Path) -> None:
    c = IOMMUCollector()
    with (
        patch.object(IOMMUCollector, "_IOMMU_CLASS", tmp_path / "absent"),
        patch.object(IOMMUCollector, "_IOMMU_GROUPS", tmp_path / "also_absent"),
    ):
        assert c.is_available() is False


def test_iommu_counts_devices(tmp_path: Path) -> None:
    groups = tmp_path / "iommu_groups"
    groups.mkdir()
    for g in range(3):
        gdir = groups / str(g)
        gdir.mkdir()
        devs = gdir / "devices"
        devs.mkdir()
        for d in range(2):  # 2 devices per group
            (devs / f"0000:0{g}:0{d}.0").touch()
    iommu_class = tmp_path / "iommu"
    iommu_class.mkdir()

    c = IOMMUCollector()
    with (
        patch.object(IOMMUCollector, "_IOMMU_CLASS", iommu_class),
        patch.object(IOMMUCollector, "_IOMMU_GROUPS", groups),
    ):
        r = c.collect()
    assert r.available is True
    assert r.values["dma_fanout"] == 6.0  # 3 × 2
    assert r.values["iommu_tag_violations"] == 0.0


# ─────────────────────────────────────────────────────────────────────
# Ethtool / /proc/net/dev
# ─────────────────────────────────────────────────────────────────────


def test_ethtool_unavailable(tmp_path: Path) -> None:
    c = EthtoolCollector()
    with patch.object(EthtoolCollector, "_PROC_NET_DEV", tmp_path / "missing"):
        assert c.is_available() is False


def test_ethtool_aggregates_interfaces(tmp_path: Path) -> None:
    proc = tmp_path / "net_dev"
    proc.write_text(
        "Inter-|   Receive                                                |  Transmit\n"
        " face |bytes    packets errs drop fifo frame compressed multicast|"
        "bytes    packets errs drop fifo colls carrier compressed\n"
        "    lo:     100      1    0    0    0     0          0         0      "
        "100      1    0    0    0     0       0          0\n"
        "  eth0:    1000     10    0    0    0     0          0         0     "
        "2000     20    0    0    0     0       0          0\n"
    )
    c = EthtoolCollector()
    with patch.object(EthtoolCollector, "_PROC_NET_DEV", proc):
        r = c.collect()
    assert r.available is True
    # lo skipped, eth0 counts
    assert r.values["network_bytes_in"] == 1000.0
    assert r.values["network_bytes_out"] == 2000.0


# ─────────────────────────────────────────────────────────────────────
# NVML (no real GPU; verify the unavailable path)
# ─────────────────────────────────────────────────────────────────────


def test_nvml_unavailable_without_pynvml() -> None:
    """In CI / dev environments without pynvml installed, NVML stays
    unavailable and never crashes."""
    c = NVMLCollector()
    # If pynvml IS installed and a GPU is present, this could be True.
    # Either way, collect() must not raise.
    r = c.collect()
    assert r.available in (True, False)


# ─────────────────────────────────────────────────────────────────────
# BMC Redfish
# ─────────────────────────────────────────────────────────────────────


def test_bmc_unavailable_without_env() -> None:
    c = BMCRedfishCollector(url=None, token=None)
    assert c.is_available() is False
    r = c.collect()
    assert r.available is False


def test_bmc_unavailable_with_partial_env() -> None:
    c = BMCRedfishCollector(url="https://bmc", token="")
    assert c.is_available() is False


# ─────────────────────────────────────────────────────────────────────
# Mock TEE + Aegis-FPGA
# ─────────────────────────────────────────────────────────────────────


def test_mock_tee_quote_always_available() -> None:
    """v4.4: collector now auto-detects TEE; without device returns mock."""
    c = MockTEEQuoteCollector()
    assert c.is_available() is True
    r = c.collect()
    assert r.available is True
    assert r.values["hypervisor_ring_violations"] == 0.0
    assert r.metadata["tee_provider"] in ("mock", "tdx", "sev-snp", "none")
    assert r.metadata["trust_level"] in ("mock", "tdx-attested", "sev-snp-attested")


def test_mock_tee_quote_deterministic_per_host() -> None:
    """Same host name → identical enclave measurement."""
    c1 = MockTEEQuoteCollector()
    c2 = MockTEEQuoteCollector()
    r1 = c1.collect()
    r2 = c2.collect()
    assert r1.metadata["enclave_measurement"] == r2.metadata["enclave_measurement"]


def test_mock_aegis_fpga() -> None:
    c = MockAegisFPGACollector()
    assert c.is_available() is True
    r = c.collect()
    assert r.values["iommu_tag_violations"] == 0.0
    assert r.metadata["fpga_present"] is False


# ─────────────────────────────────────────────────────────────────────
# Aggregator
# ─────────────────────────────────────────────────────────────────────


def test_availability_report_partitions_collectors() -> None:
    agg = CollectorAggregator()
    rep = agg.availability_report()
    assert isinstance(rep, AvailabilityReport)
    # Mock TEE + Mock FPGA always present
    assert "tee_quote" in rep.available
    assert "aegis_fpga" in rep.available


def test_aggregate_returns_hwcounters_dataclass() -> None:
    agg = CollectorAggregator()
    counters = agg.aggregate(_atv_input())
    assert isinstance(counters, HWCounters)
    # Mocks produce 0 for ring violations
    assert counters.hypervisor_ring_violations == 0
    # FPGA mock zeroes iommu_tag_violations
    assert counters.iommu_tag_violations == 0


def test_aggregate_falls_back_to_simulator_for_missing_slots() -> None:
    """When all collectors skip a slot, the simulator's baseline fills it."""
    # Empty collector list = full fallback
    agg = CollectorAggregator(collectors=[])
    counters = agg.aggregate(_atv_input())
    # Simulator baseline produces non-zero flops + thermal
    assert counters.flops_observed > 0
    assert counters.thermal_celsius_p95 > 0


def test_aggregate_collector_precedence() -> None:
    """Earlier collectors override later ones for the same slot."""

    class _FakeA:
        name = "a"
        def is_available(self) -> bool: return True
        def collect(self) -> CollectorResult:
            return CollectorResult(available=True, values={"thermal_celsius_p95": 75.0})

    class _FakeB:
        name = "b"
        def is_available(self) -> bool: return True
        def collect(self) -> CollectorResult:
            return CollectorResult(available=True, values={"thermal_celsius_p95": 90.0})

    agg = CollectorAggregator(collectors=[_FakeA(), _FakeB()])
    counters = agg.aggregate(_atv_input())
    # _FakeA wins because it's first
    assert counters.thermal_celsius_p95 == 75.0


def test_aggregate_swallows_collector_exception() -> None:
    """A buggy collector must not crash the aggregator."""

    class _Crashing:
        name = "boom"
        def is_available(self) -> bool: return True
        def collect(self) -> CollectorResult:
            raise RuntimeError("intentional")

    agg = CollectorAggregator(collectors=[_Crashing()])
    counters = agg.aggregate(_atv_input())
    # Falls back to simulator baseline cleanly
    assert isinstance(counters, HWCounters)
    assert counters.flops_observed > 0


def test_aggregate_skips_unavailable_collectors() -> None:
    class _Off:
        name = "off"
        def is_available(self) -> bool: return False
        def collect(self) -> CollectorResult:
            return CollectorResult(available=False)

    class _On:
        name = "on"
        def is_available(self) -> bool: return True
        def collect(self) -> CollectorResult:
            return CollectorResult(available=True, values={"flops_observed": 1.5e15})

    agg = CollectorAggregator(collectors=[_Off(), _On()])
    rep = agg.availability_report()
    assert "off" in rep.unavailable
    assert "on" in rep.available
    counters = agg.aggregate(_atv_input())
    assert counters.flops_observed == 1.5e15


def test_aggregate_ignores_private_keys() -> None:
    """Keys starting with _ are metadata, not HW band slots."""

    class _C:
        name = "c"
        def is_available(self) -> bool: return True
        def collect(self) -> CollectorResult:
            return CollectorResult(
                available=True,
                values={
                    "_pmu_load_1m_normalised": 0.42,
                    "_bmc_thermal_celsius_p95": 88.0,
                },
            )

    agg = CollectorAggregator(collectors=[_C()])
    counters = agg.aggregate(_atv_input())
    # Private keys did NOT overwrite thermal (which would be 88 if they had).
    # Simulator baseline thermal ≈ 62 ± 10%.
    assert counters.thermal_celsius_p95 < 75.0


# ─────────────────────────────────────────────────────────────────────
# Env-driven path
# ─────────────────────────────────────────────────────────────────────


def test_aggregate_from_env_disabled_when_provider_none(monkeypatch) -> None:
    monkeypatch.setenv("AEGIS_HW_PROVIDER", "none")
    assert aggregate_from_env(_atv_input()) is None


def test_aggregate_from_env_disabled_when_provider_sim(monkeypatch) -> None:
    monkeypatch.setenv("AEGIS_HW_PROVIDER", "sim")
    # aggregator stays None — simulator path stays in simulate_from_env
    assert aggregate_from_env(_atv_input()) is None


def test_aggregate_from_env_enabled_when_provider_real(monkeypatch) -> None:
    monkeypatch.setenv("AEGIS_HW_PROVIDER", "real")
    counters = aggregate_from_env(_atv_input())
    assert isinstance(counters, HWCounters)


def test_simulate_from_env_routes_real_to_aggregator(monkeypatch) -> None:
    """End-to-end: simulate_from_env(provider=real) returns aggregator output."""
    from aegis.hw_telemetry import simulate_from_env

    monkeypatch.setenv("AEGIS_HW_PROVIDER", "real")
    counters = simulate_from_env(_atv_input())
    assert isinstance(counters, HWCounters)


def test_simulate_from_env_routes_sim_to_simulator(monkeypatch) -> None:
    from aegis.hw_telemetry import simulate_from_env

    monkeypatch.setenv("AEGIS_HW_PROVIDER", "sim")
    counters = simulate_from_env(_atv_input())
    assert isinstance(counters, HWCounters)


def test_simulate_from_env_none_returns_none(monkeypatch) -> None:
    from aegis.hw_telemetry import simulate_from_env

    monkeypatch.setenv("AEGIS_HW_PROVIDER", "none")
    assert simulate_from_env(_atv_input()) is None


def test_aggregator_singleton_resets() -> None:
    """reset_default_aggregator clears the lazily-created singleton."""
    from aegis.hw_telemetry.collectors.aggregator import (
        _get_default_aggregator,
    )
    a1 = _get_default_aggregator()
    a2 = _get_default_aggregator()
    assert a1 is a2
    reset_default_aggregator()
    a3 = _get_default_aggregator()
    assert a3 is not a1
