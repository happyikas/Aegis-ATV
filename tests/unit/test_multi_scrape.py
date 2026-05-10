"""Unit tests for src/aegis/inference/multi_scrape.py.

Covers the multi-endpoint orchestration: concurrent scrape, graceful
degradation on unreachable endpoints, and the discriminated-union
result map (InferenceMetrics | EndpointUnreachable | EndpointSkipped).
"""

from __future__ import annotations

from unittest.mock import patch

from aegis.inference.multi_scrape import (
    EndpointSkipped,
    EndpointUnreachable,
    kv_pressure_band,
    scrape_all,
)
from aegis.inference.registry import EndpointConfig, InferenceRegistry
from aegis.inference.vllm_metrics import InferenceMetrics, VLLMMetricsError


# ── fixtures ──────────────────────────────────────────────────────


def _ok_metrics(captured_at_ns: int = 1_000_000) -> InferenceMetrics:
    return InferenceMetrics(
        captured_at_ns=captured_at_ns,
        kv_cache_used_pct=0.55,
        requests_running=2,
        requests_waiting=0,
        prompt_tokens_total=12345,
        generation_tokens_total=678,
    )


def _critical_metrics() -> InferenceMetrics:
    return InferenceMetrics(
        captured_at_ns=2_000_000,
        kv_cache_used_pct=0.97,
        requests_running=4,
        requests_waiting=12,
    )


# ── empty registry ────────────────────────────────────────────────


def test_empty_registry_returns_empty_dict() -> None:
    assert scrape_all(InferenceRegistry()) == {}


# ── happy path: single vllm endpoint ──────────────────────────────


def test_single_vllm_scrape() -> None:
    reg = InferenceRegistry(
        endpoints=(
            EndpointConfig(
                aid="agent-a",
                provider="vllm",
                metrics_url="http://10.0.0.10:8000/metrics",
            ),
        ),
    )
    snap = _ok_metrics()
    with patch(
        "aegis.inference.multi_scrape.scrape_vllm_metrics",
        return_value=snap,
    ) as mock:
        results = scrape_all(reg)
    assert mock.call_count == 1
    assert results == {"agent-a": snap}


# ── multi-endpoint mixed providers ────────────────────────────────


def test_multi_endpoint_mixed() -> None:
    reg = InferenceRegistry(
        endpoints=(
            EndpointConfig(
                aid="agent-a",
                provider="vllm",
                metrics_url="http://10.0.0.10:8000/metrics",
            ),
            EndpointConfig(
                aid="agent-b",
                provider="cloud",
                provider_name="anthropic-claude-3-5",
            ),
            EndpointConfig(
                aid="agent-c",
                provider="vllm",
                metrics_url="http://10.0.0.30:8000/metrics",
            ),
        ),
    )

    # The mock returns different metrics per URL so we can verify
    # results aren't crossed-up.
    def fake_scrape(url: str, *, timeout_s: float) -> InferenceMetrics:
        if "10.0.0.10" in url:
            return _ok_metrics(1)
        if "10.0.0.30" in url:
            return _critical_metrics()
        raise AssertionError(f"unexpected url {url}")

    with patch(
        "aegis.inference.multi_scrape.scrape_vllm_metrics",
        side_effect=fake_scrape,
    ):
        results = scrape_all(reg)

    assert set(results.keys()) == {"agent-a", "agent-b", "agent-c"}
    assert isinstance(results["agent-a"], InferenceMetrics)
    assert results["agent-a"].kv_cache_used_pct == 0.55
    # agent-b is cloud → skipped
    assert isinstance(results["agent-b"], EndpointSkipped)
    assert results["agent-b"].provider == "cloud"
    assert results["agent-b"].provider_name == "anthropic-claude-3-5"
    assert "/metrics" in results["agent-b"].reason
    # agent-c hit the critical path
    assert isinstance(results["agent-c"], InferenceMetrics)
    assert results["agent-c"].kv_cache_pressure_band() == "critical"


# ── graceful degradation ─────────────────────────────────────────


def test_unreachable_endpoint_does_not_raise() -> None:
    reg = InferenceRegistry(
        endpoints=(
            EndpointConfig(
                aid="agent-a",
                provider="vllm",
                metrics_url="http://10.0.0.10:8000/metrics",
            ),
            EndpointConfig(
                aid="agent-b",
                provider="vllm",
                metrics_url="http://10.0.0.20:8000/metrics",
            ),
        ),
    )

    def fake_scrape(url: str, *, timeout_s: float) -> InferenceMetrics:
        if "10.0.0.20" in url:
            raise VLLMMetricsError("connection refused")
        return _ok_metrics()

    with patch(
        "aegis.inference.multi_scrape.scrape_vllm_metrics",
        side_effect=fake_scrape,
    ):
        results = scrape_all(reg)

    # Both keys present — slow/dead endpoint doesn't take down the
    # whole scrape.
    assert isinstance(results["agent-a"], InferenceMetrics)
    assert isinstance(results["agent-b"], EndpointUnreachable)
    assert results["agent-b"].endpoint_unreachable == 1
    assert "connection refused" in results["agent-b"].reason
    assert results["agent-b"].metrics_url == "http://10.0.0.20:8000/metrics"


def test_disabled_endpoint_skipped_not_unreachable() -> None:
    reg = InferenceRegistry(
        endpoints=(
            EndpointConfig(
                aid="agent-paused",
                provider="vllm",
                metrics_url="http://10.0.0.30:8000/metrics",
                enabled=False,
            ),
        ),
    )

    with patch(
        "aegis.inference.multi_scrape.scrape_vllm_metrics",
    ) as mock:
        results = scrape_all(reg)

    # No HTTP scrape attempted at all.
    assert mock.call_count == 0
    r = results["agent-paused"]
    assert isinstance(r, EndpointSkipped)
    assert "disabled" in r.reason


def test_unknown_provider_skipped_with_hint() -> None:
    reg = InferenceRegistry(
        endpoints=(
            EndpointConfig(
                aid="agent-ollama",
                provider="ollama",
                metrics_url="http://10.0.0.40:11434/metrics",
            ),
        ),
    )
    results = scrape_all(reg)
    r = results["agent-ollama"]
    assert isinstance(r, EndpointSkipped)
    assert r.provider == "ollama"
    assert "not yet shipped" in r.reason


def test_unexpected_exception_recorded_as_unreachable() -> None:
    """Defensive: a bug in the worker should not crash the orchestrator."""
    reg = InferenceRegistry(
        endpoints=(
            EndpointConfig(
                aid="agent-buggy",
                provider="vllm",
                metrics_url="http://10.0.0.50:8000/metrics",
            ),
        ),
    )
    with patch(
        "aegis.inference.multi_scrape.scrape_vllm_metrics",
        side_effect=RuntimeError("worker pool bug"),
    ):
        results = scrape_all(reg)
    r = results["agent-buggy"]
    assert isinstance(r, EndpointUnreachable)
    assert "unexpected scraper exception" in r.reason


# ── kv_pressure_band convenience ─────────────────────────────────


def test_kv_pressure_band_for_metrics() -> None:
    # _ok_metrics has kv=0.55, which is "low" (<0.6).
    assert kv_pressure_band(_ok_metrics()) == "low"
    assert kv_pressure_band(_critical_metrics()) == "critical"


def test_kv_pressure_band_for_unreachable_is_na() -> None:
    r = EndpointUnreachable(aid="x", metrics_url="u", reason="r")
    assert kv_pressure_band(r) == "n/a"


def test_kv_pressure_band_for_skipped_is_na() -> None:
    r = EndpointSkipped(
        aid="x", provider="cloud", provider_name=None, reason="r",
    )
    assert kv_pressure_band(r) == "n/a"


# ── parallelism cap ──────────────────────────────────────────────


def test_max_workers_capped_at_endpoint_count() -> None:
    """A 1-endpoint registry should use a 1-thread pool, not the
    default cap of 16. We don't have a clean way to assert pool size
    from outside, but we can verify the function returns successfully
    even with extreme values (defensive edge case)."""
    reg = InferenceRegistry(
        endpoints=(
            EndpointConfig(
                aid="agent-a", provider="vllm",
                metrics_url="http://x/metrics",
            ),
        ),
    )
    with patch(
        "aegis.inference.multi_scrape.scrape_vllm_metrics",
        return_value=_ok_metrics(),
    ):
        results = scrape_all(reg, max_workers=0)
    assert "agent-a" in results
