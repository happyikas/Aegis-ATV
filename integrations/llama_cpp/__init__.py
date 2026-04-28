"""llama.cpp adapter (v3.3).

llama.cpp exposes a richer perf surface than MLX-LM:

* ``--n-gpu-layers``       — how many transformer layers reside on GPU
* ``--cache-type-k/v``     — KV quantisation (f16/q8/q4)
* ``--ctx-size`` + sliding window
* ``--draft-model``        — speculative decoding
* ``--parallel``           — concurrent slot count for serving

This adapter projects ``KVCacheAdvice`` onto a llama.cpp config
delta — never restarts the server, only tunes runtime knobs that
``server`` accepts via the OpenAI-compatible ``/v1/completions``
or its native ``/slots``/``/props`` endpoints.

Mapping
-------
    residency_class=hot   → no eviction, full GPU layers
    residency_class=warm  → keep current state
    residency_class=cold  → reduce GPU layer count by 25%, hint
                            quantise KV cache to q8
    speculative_decode    → enable --draft-model if available
    batch_key             → llama.cpp ``--parallel`` slot tag

The adapter is HTTP-only — no llama.cpp Python binding required.
"""

from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class LlamaCppAdvice:
    """llama.cpp-shaped projection of KVCacheAdvice."""

    use_draft_model: bool
    kv_cache_dtype: str        # "f16" | "q8_0" | "q4_0"
    suggested_n_gpu_layers_delta: int  # signed delta from current
    slot_cohort: str
    confidence: float
    raw: dict[str, Any]


_RESIDENCY_TO_KV_DTYPE = {
    "hot":  "f16",
    "warm": "f16",
    "cold": "q8_0",
}
_RESIDENCY_TO_GPU_LAYER_DELTA = {
    "hot":  0,    # no change
    "warm": 0,
    "cold": -8,   # demote 8 layers to CPU under pressure
}


class LlamaCppAegisAdvisor:
    """HTTP advisor adapter for llama.cpp ``server`` deployments."""

    def __init__(self, base_url: str = "http://localhost:8080", *, timeout_s: float = 1.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s

    def advise(self, atv_input: dict[str, Any]) -> LlamaCppAdvice:
        body = json.dumps(atv_input).encode("utf-8")
        req = urllib.request.Request(
            url=f"{self.base_url}/advisory/kv_cache",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
            payload: dict[str, Any] = json.loads(resp.read().decode("utf-8"))
        residency = payload.get("residency_class", "warm")
        return LlamaCppAdvice(
            use_draft_model=bool(payload.get("speculative_decode", False)),
            kv_cache_dtype=_RESIDENCY_TO_KV_DTYPE.get(residency, "f16"),
            suggested_n_gpu_layers_delta=_RESIDENCY_TO_GPU_LAYER_DELTA.get(residency, 0),
            slot_cohort=str(payload.get("batch_key", "")),
            confidence=float(payload.get("confidence", 0.0)),
            raw=payload,
        )


__all__ = ["LlamaCppAegisAdvisor", "LlamaCppAdvice"]
