"""Unit tests for src/aegis/inference/vllm_metrics.py."""

from __future__ import annotations

import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from aegis.inference.vllm_metrics import (
    InferenceMetrics,
    VLLMMetricsError,
    _to_inference_metrics,
    parse_prometheus_metrics,
    scrape_vllm_metrics,
)

# ── Fixture: a realistic vLLM /metrics payload ──────────────────────


VLLM_METRICS_FIXTURE = """\
# HELP vllm:gpu_cache_usage_perc GPU KV-cache usage. 1 means 100 percent usage.
# TYPE vllm:gpu_cache_usage_perc gauge
vllm:gpu_cache_usage_perc{model_name="meta-llama/Llama-3-8B"} 0.42
# HELP vllm:cpu_cache_usage_perc CPU KV-cache usage.
# TYPE vllm:cpu_cache_usage_perc gauge
vllm:cpu_cache_usage_perc{model_name="meta-llama/Llama-3-8B"} 0.0
# HELP vllm:num_requests_running Number of requests currently running on GPU.
# TYPE vllm:num_requests_running gauge
vllm:num_requests_running{model_name="meta-llama/Llama-3-8B"} 3
# HELP vllm:num_requests_waiting Number of requests waiting to be processed.
# TYPE vllm:num_requests_waiting gauge
vllm:num_requests_waiting{model_name="meta-llama/Llama-3-8B"} 1
# HELP vllm:prompt_tokens_total Number of prefill tokens processed.
# TYPE vllm:prompt_tokens_total counter
vllm:prompt_tokens_total{model_name="meta-llama/Llama-3-8B"} 482310
# HELP vllm:generation_tokens_total Number of generation tokens processed.
# TYPE vllm:generation_tokens_total counter
vllm:generation_tokens_total{model_name="meta-llama/Llama-3-8B"} 91204
# HELP vllm:request_prompt_tokens Number of prefill tokens processed (histogram).
# TYPE vllm:request_prompt_tokens histogram
vllm:request_prompt_tokens_sum{model_name="meta-llama/Llama-3-8B"} 482310
vllm:request_prompt_tokens_count{model_name="meta-llama/Llama-3-8B"} 412
vllm:request_prompt_tokens_bucket{model_name="meta-llama/Llama-3-8B",le="1.0"} 0
vllm:request_prompt_tokens_bucket{model_name="meta-llama/Llama-3-8B",le="+Inf"} 412
# HELP vllm:request_generation_tokens Number of generation tokens (histogram).
# TYPE vllm:request_generation_tokens histogram
vllm:request_generation_tokens_sum{model_name="meta-llama/Llama-3-8B"} 91204
vllm:request_generation_tokens_count{model_name="meta-llama/Llama-3-8B"} 412
# HELP vllm:time_to_first_token_seconds Time to first token in seconds.
# TYPE vllm:time_to_first_token_seconds histogram
vllm:time_to_first_token_seconds_sum{model_name="meta-llama/Llama-3-8B"} 28.4
vllm:time_to_first_token_seconds_count{model_name="meta-llama/Llama-3-8B"} 412
# HELP vllm:time_per_output_token_seconds TPOT in seconds.
# TYPE vllm:time_per_output_token_seconds histogram
vllm:time_per_output_token_seconds_sum{model_name="meta-llama/Llama-3-8B"} 19.2
vllm:time_per_output_token_seconds_count{model_name="meta-llama/Llama-3-8B"} 412
"""


# ── parse_prometheus_metrics() ─────────────────────────────────────


def test_parse_extracts_known_vllm_metrics() -> None:
    raw = parse_prometheus_metrics(VLLM_METRICS_FIXTURE)
    assert raw["vllm:gpu_cache_usage_perc"] == pytest.approx(0.42)
    assert raw["vllm:num_requests_running"] == 3
    assert raw["vllm:prompt_tokens_total"] == 482310


def test_parse_ignores_comment_lines_and_unknown_metrics() -> None:
    text = (
        "# HELP something_else A different metric.\n"
        "# TYPE something_else counter\n"
        "something_else_we_dont_care_about 999\n"
        "vllm:gpu_cache_usage_perc 0.5\n"
    )
    raw = parse_prometheus_metrics(text)
    assert raw == {"vllm:gpu_cache_usage_perc": 0.5}


def test_parse_ignores_histogram_buckets() -> None:
    """vllm:request_prompt_tokens_bucket is NOT in our metric set, so
    bucket lines are dropped — we only keep _sum and _count."""
    raw = parse_prometheus_metrics(VLLM_METRICS_FIXTURE)
    assert "vllm:request_prompt_tokens_sum" in raw
    assert "vllm:request_prompt_tokens_count" in raw
    # Verify bucket line did NOT make it in.
    assert not any("_bucket" in k for k in raw)


def test_parse_handles_nan_and_inf_values() -> None:
    """vLLM emits NaN for histograms with no observations yet."""
    text = (
        "vllm:gpu_cache_usage_perc 0.7\n"
        "vllm:time_to_first_token_seconds_sum NaN\n"
        "vllm:time_to_first_token_seconds_count 0\n"
    )
    raw = parse_prometheus_metrics(text)
    # gpu_cache_usage_perc parsed as float
    assert raw["vllm:gpu_cache_usage_perc"] == pytest.approx(0.7)
    # NaN row was dropped (parser returns no key for it).
    assert "vllm:time_to_first_token_seconds_sum" not in raw
    # count=0 still present.
    assert raw["vllm:time_to_first_token_seconds_count"] == 0


def test_parse_empty_payload_raises() -> None:
    with pytest.raises(VLLMMetricsError, match="empty"):
        parse_prometheus_metrics("")
    with pytest.raises(VLLMMetricsError, match="empty"):
        parse_prometheus_metrics("   \n  \n")


def test_parse_no_vllm_metrics_raises() -> None:
    """Payload exists but contains zero vllm:* metrics — likely the
    user pointed at a non-vLLM Prometheus endpoint."""
    text = (
        "# HELP foo counter\n"
        "# TYPE foo counter\n"
        "foo_total 42\n"
        "bar 17\n"
    )
    with pytest.raises(VLLMMetricsError, match="no recognised vllm:"):
        parse_prometheus_metrics(text)


# ── _to_inference_metrics() ────────────────────────────────────────


def test_to_inference_metrics_full_fixture() -> None:
    raw = parse_prometheus_metrics(VLLM_METRICS_FIXTURE)
    snap = _to_inference_metrics(raw)
    assert isinstance(snap, InferenceMetrics)
    assert snap.kv_cache_used_pct == pytest.approx(0.42)
    assert snap.cpu_cache_used_pct == 0.0
    assert snap.requests_running == 3
    assert snap.requests_waiting == 1
    assert snap.prompt_tokens_total == 482310
    assert snap.generation_tokens_total == 91204

    # avg_prompt_tokens = 482310 / 412 ≈ 1170.65
    assert snap.avg_prompt_tokens_per_request == pytest.approx(1170.65, rel=1e-3)
    # avg_ttft = 28.4 / 412 ≈ 0.0689 seconds
    assert snap.avg_ttft_seconds == pytest.approx(0.0689, rel=1e-2)


def test_to_inference_metrics_zero_count_avoids_divide_by_zero() -> None:
    raw = {
        "vllm:gpu_cache_usage_perc": 0.1,
        "vllm:request_prompt_tokens_sum": 100.0,
        "vllm:request_prompt_tokens_count": 0.0,
    }
    snap = _to_inference_metrics(raw)
    assert snap.avg_prompt_tokens_per_request == 0.0


def test_to_inference_metrics_missing_spec_decode_returns_none() -> None:
    """If vLLM was not started with --speculative-model, the
    spec_decode_efficiency metric isn't emitted at all."""
    raw = parse_prometheus_metrics(VLLM_METRICS_FIXTURE)
    snap = _to_inference_metrics(raw)
    assert snap.spec_decode_efficiency is None
    assert snap.spec_decode_accepted_total == 0


# ── kv_cache_pressure_band + saturated heuristics ──────────────────


@pytest.mark.parametrize(
    "pct,band",
    [
        (0.0, "low"),
        (0.55, "low"),
        (0.6, "moderate"),
        (0.84, "moderate"),
        (0.85, "high"),
        (0.94, "high"),
        (0.95, "critical"),
        (1.0, "critical"),
    ],
)
def test_kv_cache_pressure_band(pct: float, band: str) -> None:
    snap = InferenceMetrics(captured_at_ns=0, kv_cache_used_pct=pct)
    assert snap.kv_cache_pressure_band() == band


def test_saturated_when_kv_cache_critical() -> None:
    snap = InferenceMetrics(captured_at_ns=0, kv_cache_used_pct=0.99)
    assert snap.saturated() is True


def test_saturated_when_queue_deep() -> None:
    snap = InferenceMetrics(
        captured_at_ns=0,
        kv_cache_used_pct=0.3,
        requests_waiting=12,
    )
    assert snap.saturated() is True


def test_not_saturated_in_healthy_range() -> None:
    snap = InferenceMetrics(
        captured_at_ns=0,
        kv_cache_used_pct=0.7,
        requests_waiting=2,
    )
    assert snap.saturated() is False


# ── scrape_vllm_metrics() — network paths mocked ───────────────────


def _mock_urlopen(payload: str = VLLM_METRICS_FIXTURE, status: int = 200):
    """Build a context-manager mock for urllib.request.urlopen."""
    response = MagicMock()
    response.status = status
    response.read.return_value = payload.encode("utf-8")
    response.headers.get_content_charset.return_value = "utf-8"
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=response)
    cm.__exit__ = MagicMock(return_value=False)
    return cm


def test_scrape_happy_path() -> None:
    with patch(
        "urllib.request.urlopen",
        return_value=_mock_urlopen(),
    ):
        snap = scrape_vllm_metrics("http://localhost:8000")
    assert snap.kv_cache_used_pct == pytest.approx(0.42)


def test_scrape_appends_metrics_path() -> None:
    captured = {}

    def fake_urlopen(url, timeout):  # noqa: ANN001
        captured["url"] = url
        return _mock_urlopen()

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        scrape_vllm_metrics("http://localhost:8000/")
    assert captured["url"] == "http://localhost:8000/metrics"


def test_scrape_custom_path() -> None:
    captured = {}

    def fake_urlopen(url, timeout):  # noqa: ANN001
        captured["url"] = url
        return _mock_urlopen()

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        scrape_vllm_metrics(
            "http://10.0.0.5:9000",
            path="/v1/internal/metrics",
        )
    assert captured["url"] == "http://10.0.0.5:9000/v1/internal/metrics"


def test_scrape_500_response_raises() -> None:
    with patch(
        "urllib.request.urlopen",
        return_value=_mock_urlopen(status=500),
    ), pytest.raises(VLLMMetricsError, match="HTTP 500"):
        scrape_vllm_metrics("http://localhost:8000")


def test_scrape_network_failure_raises() -> None:
    with patch(
        "urllib.request.urlopen",
        side_effect=urllib.error.URLError("connection refused"),
    ), pytest.raises(VLLMMetricsError, match="unreachable"):
        scrape_vllm_metrics("http://localhost:8000")


def test_scrape_empty_url_raises() -> None:
    with pytest.raises(VLLMMetricsError, match="base_url is empty"):
        scrape_vllm_metrics("")


def test_scrape_unparseable_payload_raises() -> None:
    """If the URL is reachable but returns non-Prometheus text (e.g.
    HTML 404 page), the parser raises VLLMMetricsError which propagates."""
    with patch(
        "urllib.request.urlopen",
        return_value=_mock_urlopen(payload="<html>404 Not Found</html>"),
    ), pytest.raises(VLLMMetricsError, match="no recognised vllm"):
        scrape_vllm_metrics("http://localhost:8000")


def test_inference_module_exports_public_api() -> None:
    """Top-level imports from aegis.inference must work — used by the
    aegis CLI and any future advisors."""
    from aegis.inference import (
        InferenceMetrics as PublicMetrics,
    )
    from aegis.inference import (
        VLLMMetricsError as PublicError,
    )
    from aegis.inference import (
        scrape_vllm_metrics as public_scrape,
    )
    assert PublicMetrics is InferenceMetrics
    assert PublicError is VLLMMetricsError
    assert public_scrape is scrape_vllm_metrics
