"""vLLM ``/metrics`` Prometheus-format scraper for the OpenClaw +
Local OSS LLM release track.

vLLM (https://docs.vllm.ai/) exposes a ``/metrics`` endpoint with
~50 Prometheus metrics. Aegis only cares about the slice that maps
to ATV signals:

* ``vllm:gpu_cache_usage_perc``       → InferenceMetrics.kv_cache_used_pct
* ``vllm:cpu_cache_usage_perc``       → InferenceMetrics.cpu_cache_used_pct
* ``vllm:num_requests_running``       → InferenceMetrics.requests_running
* ``vllm:num_requests_waiting``       → InferenceMetrics.requests_waiting
* ``vllm:prompt_tokens_total``        → tokens-in counter
* ``vllm:generation_tokens_total``    → tokens-out counter
* ``vllm:request_prompt_tokens``      → prompt-token histogram (sum / count)
* ``vllm:request_generation_tokens``  → gen-token histogram (sum / count)
* ``vllm:time_to_first_token_seconds`` → TTFT histogram
* ``vllm:time_per_output_token_seconds`` → TPOT histogram
* ``vllm:spec_decode_efficiency``      → speculative decoding acceptance %
* ``vllm:spec_decode_num_accepted_tokens_total`` → counter
* ``vllm:spec_decode_num_emitted_tokens_total``  → counter

Throughput (tokens/sec) is *derived*: caller scrapes twice with a
known interval, computes ``Δ(prompt_tokens_total) / Δt``. The single
snapshot ``InferenceMetrics`` has the raw counters; the
``ThroughputDerivation`` helper does the diff.

This module has **zero hard dependencies** beyond the stdlib +
``urllib.request`` for the HTTP fetch. No prometheus-client lib —
keeps the Aegis install footprint minimal.

Cloud LLM tracks (Claude Code / OpenClaw + Cloud) — these metrics
do not exist; the API providers do not expose KV-cache or scheduler
internals. See docs/releases/OPENCLAW_LOCAL.ko.md §2 for the
positioning.
"""

from __future__ import annotations

import math
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Final


class VLLMMetricsError(RuntimeError):
    """Raised when a scrape fails (network, parse, missing metric).

    Aegis treats this as "inference telemetry unavailable" — never
    blocks a PreToolUse on it; falls back to the cloud-LLM behaviour
    of relying on the transcript ``usage`` block alone.
    """


# Metrics we extract — ordered for stability in the dataclass.
# Both single-value gauges and histogram derived stats are listed here.
_VLLM_METRIC_NAMES: Final[set[str]] = {
    "vllm:gpu_cache_usage_perc",
    "vllm:cpu_cache_usage_perc",
    "vllm:num_requests_running",
    "vllm:num_requests_waiting",
    "vllm:prompt_tokens_total",
    "vllm:generation_tokens_total",
    "vllm:spec_decode_efficiency",
    "vllm:spec_decode_num_accepted_tokens_total",
    "vllm:spec_decode_num_emitted_tokens_total",
    # histograms expose `<name>_sum` and `<name>_count` lines:
    "vllm:request_prompt_tokens_sum",
    "vllm:request_prompt_tokens_count",
    "vllm:request_generation_tokens_sum",
    "vllm:request_generation_tokens_count",
    "vllm:time_to_first_token_seconds_sum",
    "vllm:time_to_first_token_seconds_count",
    "vllm:time_per_output_token_seconds_sum",
    "vllm:time_per_output_token_seconds_count",
}


@dataclass(frozen=True)
class InferenceMetrics:
    """One snapshot of vLLM telemetry. Single point in time."""

    captured_at_ns: int

    # KV cache occupancy — the key signal for "cache hit" in cloud LLMs
    # is replaced here with "cache utilization" because we control the
    # server. >95% sustained = thrashing risk.
    kv_cache_used_pct: float = 0.0       # 0.0–1.0
    cpu_cache_used_pct: float = 0.0      # 0.0–1.0

    # Scheduler queue depth — proxy for inference latency under load.
    requests_running: int = 0
    requests_waiting: int = 0

    # Cumulative token counters — caller does Δ for throughput.
    prompt_tokens_total: int = 0
    generation_tokens_total: int = 0

    # Histogram-derived averages (sum/count). Falls to NaN when count==0.
    avg_prompt_tokens_per_request: float = 0.0
    avg_generation_tokens_per_request: float = 0.0
    avg_ttft_seconds: float = 0.0
    avg_tpot_seconds: float = 0.0

    # Speculative decoding (vLLM ≥ 0.6, optional flag).
    spec_decode_efficiency: float | None = None
    spec_decode_accepted_total: int = 0
    spec_decode_emitted_total: int = 0

    # Raw map — exposed for debugging / advisor extension.
    raw: dict[str, float] = field(default_factory=dict)

    def kv_cache_pressure_band(self) -> str:
        """Human-readable bucket for Live dashboards.

        Bands match what the kv-cache-optimizer advisor expects:
        * <0.6  ``low``       — capacity available, throughput optimum
        * <0.85 ``moderate``  — healthy
        * <0.95 ``high``      — getting tight
        * else  ``critical``  — impending eviction / thrashing
        """
        p = self.kv_cache_used_pct
        if p < 0.6:
            return "low"
        if p < 0.85:
            return "moderate"
        if p < 0.95:
            return "high"
        return "critical"

    def saturated(self) -> bool:
        """Combined heuristic — KV cache critical OR queue depth > 8."""
        return (
            self.kv_cache_pressure_band() == "critical"
            or self.requests_waiting > 8
        )


# ──────────────────────────────────────────────────────────────────
# Parser — Prometheus text exposition format (the simple v0.0.4 one,
# not OpenMetrics). Full BNF: https://prometheus.io/docs/instrumenting/exposition_formats/#text-format-details
# ──────────────────────────────────────────────────────────────────


# A metric line looks like:
#   <name>{<labels>} <value> [<timestamp>]
# We ignore labels and timestamps — vLLM's labels are model_name etc.
# which are constant across one server, and timestamp is always
# scrape-time so caller's wall clock is more reliable.
_LINE_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)"
    r"(?:\{[^}]*\})?"
    r"\s+"
    r"(?P<value>[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?|NaN|[+-]?Inf)"
    r"(?:\s+\d+)?\s*$"
)


def parse_prometheus_metrics(text: str) -> dict[str, float]:
    """Parse a Prometheus exposition-format payload, keeping only the
    vLLM metrics we care about.

    Comment lines (``# HELP``, ``# TYPE``) and labels are ignored —
    we want a single value per metric name. If the same metric name
    appears multiple times (different label sets), the LAST wins —
    caller can't disambiguate without label parsing, but in practice
    vLLM exposes one server per process so this is fine.

    Returns a flat ``{name: float}`` map. Caller is responsible for
    extracting per-metric semantics via :func:`_to_inference_metrics`.

    Raises :class:`VLLMMetricsError` if the payload is empty or
    contains no recognised vLLM metric.
    """
    if not text or not text.strip():
        raise VLLMMetricsError("empty prometheus payload")

    out: dict[str, float] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        m = _LINE_RE.match(line)
        if m is None:
            continue
        name = m["name"]
        if name not in _VLLM_METRIC_NAMES:
            continue
        try:
            value = float(m["value"])
        except ValueError:
            continue
        # vLLM histograms emit NaN for empty buckets and ±Inf for
        # boundary buckets. Treating those as numeric 0 would skew
        # advisor signals, so we drop them and let the dataclass
        # default kick in.
        if math.isnan(value) or math.isinf(value):
            continue
        out[name] = value

    if not out:
        raise VLLMMetricsError(
            "no recognised vllm:* metrics in payload — is this a "
            "vLLM /metrics endpoint? Check the URL."
        )
    return out


def _to_inference_metrics(raw: dict[str, float]) -> InferenceMetrics:
    """Convert the flat parser output into the typed dataclass."""
    def _avg(sum_key: str, count_key: str) -> float:
        s = raw.get(sum_key, 0.0)
        c = raw.get(count_key, 0.0)
        return float(s / c) if c > 0 else 0.0

    spec_eff_raw = raw.get("vllm:spec_decode_efficiency")
    return InferenceMetrics(
        captured_at_ns=time.time_ns(),
        kv_cache_used_pct=raw.get("vllm:gpu_cache_usage_perc", 0.0),
        cpu_cache_used_pct=raw.get("vllm:cpu_cache_usage_perc", 0.0),
        requests_running=int(raw.get("vllm:num_requests_running", 0)),
        requests_waiting=int(raw.get("vllm:num_requests_waiting", 0)),
        prompt_tokens_total=int(raw.get("vllm:prompt_tokens_total", 0)),
        generation_tokens_total=int(
            raw.get("vllm:generation_tokens_total", 0),
        ),
        avg_prompt_tokens_per_request=_avg(
            "vllm:request_prompt_tokens_sum",
            "vllm:request_prompt_tokens_count",
        ),
        avg_generation_tokens_per_request=_avg(
            "vllm:request_generation_tokens_sum",
            "vllm:request_generation_tokens_count",
        ),
        avg_ttft_seconds=_avg(
            "vllm:time_to_first_token_seconds_sum",
            "vllm:time_to_first_token_seconds_count",
        ),
        avg_tpot_seconds=_avg(
            "vllm:time_per_output_token_seconds_sum",
            "vllm:time_per_output_token_seconds_count",
        ),
        spec_decode_efficiency=(
            None if spec_eff_raw is None else float(spec_eff_raw)
        ),
        spec_decode_accepted_total=int(
            raw.get("vllm:spec_decode_num_accepted_tokens_total", 0),
        ),
        spec_decode_emitted_total=int(
            raw.get("vllm:spec_decode_num_emitted_tokens_total", 0),
        ),
        raw=raw,
    )


def scrape_vllm_metrics(
    base_url: str,
    *,
    timeout_s: float = 2.0,
    path: str = "/metrics",
) -> InferenceMetrics:
    """Scrape a single snapshot from a running vLLM server.

    Args:
        base_url: vLLM server base URL, e.g. ``http://localhost:8000``.
            ``/metrics`` is appended automatically (configurable via
            ``path``).
        timeout_s: HTTP timeout in seconds. Aegis treats timeout as
            "telemetry unavailable" — defaults short (2 s) so a
            stuck vLLM doesn't block PreToolUse paths.
        path: Override metrics path (rare — only if the user runs
            vLLM behind a reverse proxy with a non-standard mount).

    Raises :class:`VLLMMetricsError` on any failure — network,
    timeout, parse error, missing metrics. Caller decides whether
    that's fatal (Doctor health check) or fallback-able (Live
    dashboard).
    """
    if not base_url:
        raise VLLMMetricsError("base_url is empty")

    url = base_url.rstrip("/") + path

    try:
        with urllib.request.urlopen(url, timeout=timeout_s) as resp:
            status = resp.status
            if status != 200:
                raise VLLMMetricsError(
                    f"vLLM /metrics returned HTTP {status} at {url}"
                )
            charset = resp.headers.get_content_charset() or "utf-8"
            payload = resp.read().decode(charset, errors="replace")
    except urllib.error.URLError as e:
        raise VLLMMetricsError(
            f"vLLM /metrics unreachable at {url}: {e.reason!r}"
        ) from e
    except TimeoutError as e:
        raise VLLMMetricsError(
            f"vLLM /metrics timed out after {timeout_s}s at {url}"
        ) from e

    raw = parse_prometheus_metrics(payload)
    return _to_inference_metrics(raw)
