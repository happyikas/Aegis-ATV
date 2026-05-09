"""Server-side inference telemetry for self-hosted LLM deployments.

This module is *only* meaningful in the OpenClaw + Local OSS LLM
release track. Cloud LLM providers (Anthropic / OpenAI / Google) do
not expose these metrics — see docs/releases/OPENCLAW_LOCAL.ko.md §2
for the full list of what becomes observable in the local-OSS track.

Currently shipped:

* :func:`vllm_metrics.scrape_vllm_metrics` — pull a Prometheus-format
  ``/metrics`` snapshot from a running vLLM server and parse the slice
  Aegis cares about (KV cache hit rate, GPU memory, throughput,
  speculative-decoding acceptance, request queue depth).

Planned (separate PRs):

* Ollama metrics adapter (Ollama uses a custom JSON schema, not
  Prometheus).
* TGI (Text Generation Inference) metrics adapter — Prometheus,
  schema-compatible with vLLM with minor field-name differences.
* GPU metrics via ``nvidia-smi --query-gpu=...`` for environments
  without DCGM.
"""

from aegis.inference.vllm_metrics import (
    InferenceMetrics,
    VLLMMetricsError,
    parse_prometheus_metrics,
    scrape_vllm_metrics,
)

__all__ = [
    "InferenceMetrics",
    "VLLMMetricsError",
    "parse_prometheus_metrics",
    "scrape_vllm_metrics",
]
