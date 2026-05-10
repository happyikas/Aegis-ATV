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
* :func:`registry.load_registry` + :func:`multi_scrape.scrape_all` —
  Gap B (issue #145): multi-endpoint registry at
  ``~/.aegis/inference.toml`` mapping each agent (``aid``) to its
  inference backend, so ``aegis metrics --all`` can scrape per-agent
  telemetry in deployments where different agents use different vLLM
  servers (or a mix of vLLM + cloud).

Planned (separate PRs):

* Ollama metrics adapter (Ollama uses a custom JSON schema, not
  Prometheus).
* TGI (Text Generation Inference) metrics adapter — Prometheus,
  schema-compatible with vLLM with minor field-name differences.
* GPU metrics via ``nvidia-smi --query-gpu=...`` for environments
  without DCGM.
"""

from aegis.inference.logit_metrics import (
    DEFAULT_LOW_CONFIDENCE_THRESHOLD,
    LogitMetrics,
    parse_vllm_logprobs,
)
from aegis.inference.multi_scrape import (
    EndpointSkipped,
    EndpointUnreachable,
    ScrapeResult,
    kv_pressure_band,
    scrape_all,
)
from aegis.inference.registry import (
    DEFAULT_TIMEOUT_S,
    EndpointConfig,
    InferenceRegistry,
    InferenceRegistryError,
    default_registry_path,
    load_registry,
)
from aegis.inference.vllm_metrics import (
    InferenceMetrics,
    VLLMMetricsError,
    parse_prometheus_metrics,
    scrape_vllm_metrics,
)

__all__ = [
    "DEFAULT_LOW_CONFIDENCE_THRESHOLD",
    "DEFAULT_TIMEOUT_S",
    "EndpointConfig",
    "EndpointSkipped",
    "EndpointUnreachable",
    "InferenceMetrics",
    "InferenceRegistry",
    "InferenceRegistryError",
    "LogitMetrics",
    "ScrapeResult",
    "VLLMMetricsError",
    "default_registry_path",
    "kv_pressure_band",
    "load_registry",
    "parse_prometheus_metrics",
    "parse_vllm_logprobs",
    "scrape_all",
    "scrape_vllm_metrics",
]
