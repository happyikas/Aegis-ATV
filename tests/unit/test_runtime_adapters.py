"""Unit tests for integrations/{mlx_lm,llama_cpp} adapters (v3.3).

These adapters are HTTP-only — we mock urlopen instead of standing
up a TestClient (the adapters target the running sidecar at
http://aegis:8080, not an in-process FastAPI app)."""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from integrations.llama_cpp import LlamaCppAegisAdvisor  # noqa: E402
from integrations.mlx_lm import MLXLMAegisAdvisor  # noqa: E402
from integrations.vllm import VLLMAegisAdvisor  # noqa: E402


def _mock_advice_response(**overrides: Any) -> Any:
    payload = {
        "prefetch_segment_ids": ["mem-abc", "mem-def"],
        "evict_candidates": [],
        "residency_class": "hot",
        "batch_key": "ab12cd34",
        "speculative_decode": True,
        "confidence": 0.85,
        "reasons": ["test"],
        "latency_ms": 0.4,
        "advisor_hash": "deadbeef",
    }
    payload.update(overrides)
    body = json.dumps(payload).encode("utf-8")
    mock_resp = MagicMock()
    mock_resp.__enter__ = lambda self: io.BytesIO(body)
    mock_resp.__exit__ = lambda *args: None
    return mock_resp


# ── MLX-LM ────────────────────────────────────────────────────────────


def test_mlx_advisor_translates_hot_residency_to_max_window() -> None:
    advisor = MLXLMAegisAdvisor(base_url="http://x:8080")
    with patch("urllib.request.urlopen", return_value=_mock_advice_response(residency_class="hot")):
        advice = advisor.advise({"foo": "bar"})
    assert advice.sliding_window == 16384
    assert advice.speculative is True
    assert advice.cohort_tag == "ab12cd34"


def test_mlx_advisor_warm_uses_4k_window() -> None:
    advisor = MLXLMAegisAdvisor()
    with patch("urllib.request.urlopen", return_value=_mock_advice_response(residency_class="warm")):
        advice = advisor.advise({})
    assert advice.sliding_window == 4096


def test_mlx_advisor_cold_uses_2k_window() -> None:
    advisor = MLXLMAegisAdvisor()
    with patch("urllib.request.urlopen", return_value=_mock_advice_response(residency_class="cold")):
        advice = advisor.advise({})
    assert advice.sliding_window == 2048


def test_mlx_advisor_report_posts_perf_metrics() -> None:
    advisor = MLXLMAegisAdvisor()
    captured: dict[str, Any] = {}

    def fake_urlopen(req: Any, timeout: float = 1.0) -> Any:
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data.decode("utf-8"))
        m = MagicMock()
        m.__enter__ = lambda self: m
        m.__exit__ = lambda *args: None
        return m

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        advisor.report(
            "rec-1", tenant_id="t", aid="a",
            cache_hit_rate=0.83, tokens_per_second=215.0,
            runtime_latency_ms=43.2,
        )
    assert captured["url"].endswith("/tool-outcome")
    assert captured["body"]["cache_hit_rate"] == 0.83
    assert captured["body"]["tenant_id"] == "t"


# ── llama.cpp ─────────────────────────────────────────────────────────


def test_llama_advisor_hot_keeps_f16_no_layer_change() -> None:
    advisor = LlamaCppAegisAdvisor()
    with patch("urllib.request.urlopen", return_value=_mock_advice_response(residency_class="hot")):
        advice = advisor.advise({})
    assert advice.kv_cache_dtype == "f16"
    assert advice.suggested_n_gpu_layers_delta == 0
    assert advice.use_draft_model is True


def test_llama_advisor_cold_quantises_and_demotes_layers() -> None:
    advisor = LlamaCppAegisAdvisor()
    with patch("urllib.request.urlopen", return_value=_mock_advice_response(
        residency_class="cold", speculative_decode=False,
    )):
        advice = advisor.advise({})
    assert advice.kv_cache_dtype == "q8_0"
    assert advice.suggested_n_gpu_layers_delta == -8
    assert advice.use_draft_model is False


def test_llama_advisor_preserves_raw_payload() -> None:
    advisor = LlamaCppAegisAdvisor()
    with patch("urllib.request.urlopen", return_value=_mock_advice_response()):
        advice = advisor.advise({})
    assert "advisor_hash" in advice.raw
    assert advice.raw["confidence"] == 0.85


# ── vLLM ──────────────────────────────────────────────────────────────


def _mock_advisory_all_response(**overrides: Any) -> Any:
    payload = {
        "kv_cache": {
            "prefetch_segment_ids": ["mem-1", "mem-2"],
            "evict_candidates": ["hist-X"],
            "residency_class": "hot",
            "batch_key": "cohort-A",
            "speculative_decode": True,
            "confidence": 0.80,
            "reasons": [],
            "latency_ms": 0.3,
            "advisor_hash": "h1",
        },
        "scheduling": {
            "priority_class": "interactive",
            "preempt_safe": False,
            "max_concurrent_in_cohort": 8,
            "deadline_ms": 2000,
            "confidence": 0.75,
            "reasons": [],
            "latency_ms": 0.2,
            "advisor_hash": "h2",
        },
        "placement": {
            "layer_residency_plan": {0: "hbm", 1: "hbm"},
            "kv_quantisation_dtype": "f16",
            "prefetch_window_tokens": 128,
            "swap_threshold_bytes": 1_000_000_000,
            "confidence": 0.70,
            "reasons": [],
            "latency_ms": 0.2,
            "advisor_hash": "h3",
        },
    }
    payload.update(overrides)
    body = json.dumps(payload).encode("utf-8")
    mock_resp = MagicMock()
    mock_resp.__enter__ = lambda self: io.BytesIO(body)
    mock_resp.__exit__ = lambda *args: None
    return mock_resp


def test_vllm_advisor_projects_combined_response() -> None:
    advisor = VLLMAegisAdvisor()
    with patch("urllib.request.urlopen", return_value=_mock_advisory_all_response()):
        advice = advisor.advise({})
    assert advice.pin_block_ids == ["mem-1", "mem-2"]
    assert advice.evict_priority_block_ids == ["hist-X"]
    assert advice.priority_class == "interactive"
    assert advice.kv_dtype == "f16"
    assert advice.prefetch_tokens == 128
    assert advice.cohort == "cohort-A"
    assert advice.speculative_decode is True
    # Average of 0.80, 0.75, 0.70
    assert 0.74 < advice.confidence < 0.76


def test_vllm_advisor_preserves_raw_subblocks() -> None:
    advisor = VLLMAegisAdvisor()
    with patch("urllib.request.urlopen", return_value=_mock_advisory_all_response()):
        advice = advisor.advise({})
    assert advice.raw_kv_cache["advisor_hash"] == "h1"
    assert advice.raw_scheduling["advisor_hash"] == "h2"
    assert advice.raw_placement["advisor_hash"] == "h3"


def test_vllm_advisor_handles_partial_payload() -> None:
    """If /advisory/all returns degraded data (e.g. one head failed),
    the projector falls back to safe defaults."""
    advisor = VLLMAegisAdvisor()
    body = json.dumps({"kv_cache": {}}).encode("utf-8")
    mock_resp = MagicMock()
    mock_resp.__enter__ = lambda self: io.BytesIO(body)
    mock_resp.__exit__ = lambda *args: None
    with patch("urllib.request.urlopen", return_value=mock_resp):
        advice = advisor.advise({})
    assert advice.pin_block_ids == []
    assert advice.priority_class == "batch"
    assert advice.kv_dtype == "f16"
    assert advice.confidence == 0.0
