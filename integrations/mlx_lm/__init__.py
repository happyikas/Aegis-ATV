"""MLX-LM adapter (v3.3).

MLX-LM (https://github.com/ml-explore/mlx-lm) does not ship a paged
KV cache today; on Apple Silicon all KV stays in unified memory and
the practical perf knobs are:

* **Speculative decoding on/off** (mlx_lm.generate.speculative_decode)
* **Sliding-window attention masking** for long contexts
* **Draft-model selection** (small q4 model alongside the main one)

This adapter calls Aegis ``/advisory/kv_cache`` once per turn and
maps ``KVCacheAdvice`` onto these MLX-LM knobs:

    speculative_decode → mlx_lm speculative draft on/off
    residency_class    → sliding-window k (hot=full, warm=4096,
                         cold=2048)
    batch_key          → request-cohort tag for ``mlx_lm.serve``
                         scheduler

The adapter is a *plain Python* class — no C++ binding required.
Drop it into the call site that builds an MLX-LM ``GenerationConfig``.
"""

from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class MLXAdvice:
    """MLX-LM-specific projection of a KVCacheAdvice."""

    speculative: bool
    sliding_window: int
    cohort_tag: str
    confidence: float
    raw: dict[str, Any]


_RESIDENCY_TO_WINDOW = {
    "hot":  16384,  # effectively full (most MLX-LM contexts ≤16k)
    "warm":  4096,
    "cold":  2048,
}


class MLXLMAegisAdvisor:
    """Thin wrapper that POSTs ATVInput to /advisory/kv_cache and
    returns an MLX-LM-shaped advice.

    Usage::

        advisor = MLXLMAegisAdvisor("http://aegis:8080")
        advice = advisor.advise(atv_input_dict)
        gen_config = mlx_lm.GenerationConfig(
            use_speculative_decode=advice.speculative,
            max_kv_size=advice.sliding_window,
        )
    """

    def __init__(self, base_url: str = "http://localhost:8080", *, timeout_s: float = 1.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s

    def advise(self, atv_input: dict[str, Any]) -> MLXAdvice:
        body = json.dumps(atv_input).encode("utf-8")
        req = urllib.request.Request(
            url=f"{self.base_url}/advisory/kv_cache",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
            payload: dict[str, Any] = json.loads(resp.read().decode("utf-8"))
        return MLXAdvice(
            speculative=bool(payload.get("speculative_decode", False)),
            sliding_window=_RESIDENCY_TO_WINDOW.get(payload.get("residency_class", "warm"), 4096),
            cohort_tag=str(payload.get("batch_key", "")),
            confidence=float(payload.get("confidence", 0.0)),
            raw=payload,
        )

    def report(
        self,
        record_id: str,
        *,
        tenant_id: str,
        aid: str,
        cache_hit_rate: float | None = None,
        tokens_per_second: float | None = None,
        runtime_latency_ms: float | None = None,
    ) -> None:
        """Close the loop — report measured perf back to Aegis."""
        body = json.dumps({
            "record_id": record_id,
            "status": "success",
            "result_hash": "mlx-runtime",
            "tenant_id": tenant_id,
            "aid": aid,
            "cache_hit_rate": cache_hit_rate,
            "tokens_per_second": tokens_per_second,
            "runtime_latency_ms": runtime_latency_ms,
        }).encode("utf-8")
        req = urllib.request.Request(
            url=f"{self.base_url}/tool-outcome",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout_s):
            pass


__all__ = ["MLXLMAegisAdvisor", "MLXAdvice"]
