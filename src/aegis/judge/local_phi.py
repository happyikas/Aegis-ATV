"""LocalPhiJudge — Phi-4-mini-q4 local sLLM (v2.6).

Solo Free / privacy-first deployment path: a small quantized Llama-
family model that runs on-device (Apple Silicon Metal, CUDA, or
CPU-only) so verdict reasoning happens **without any cloud round-
trip**. ATV-aware: passes the structured ATV summary plus key
named-slot signals into the prompt so the LM doesn't have to re-
derive features from raw text.

Three modes by environment:

* **Real model present** (``AEGIS_JUDGE_MODEL_PATH=/path/to/phi.gguf``
  + ``llama-cpp-python`` installed): loads the GGUF, computes
  ``SHA3-256`` over the file as ``model_hash``, runs greedy-decode
  inference (temperature=0, top_k=1, no sampling) and parses the
  decode for a JSON verdict.
* **Stub mode** (no model path or ``AEGIS_JUDGE_LOCAL_PHI_STUB=1``):
  deterministic verdict that matches the contract — used in CI, in
  containers without GPU, and on any machine without the model
  file. The model_hash is the SHA3 of the **stub seed string** so
  audits can distinguish stub vs real.
* **Disabled** (env points at a missing file, llama-cpp-python
  missing): returns a low-confidence ALLOW with a clear reason so
  the v3.0 HybridJudge can route past it to the next layer.

The contract that step340 / HybridJudge cares about is identical
across all three modes: ``evaluate_full(summary, atv, inp) →
JudgeVerdict`` with ``model_hash``, ``latency_ms``, deterministic
output for the same input. The actual quality of the verdict
depends on which mode is active — stub mode reuses the M13
AttributionHead's verdict so the contract still gives a meaningful
signal even without the real model file.
"""

from __future__ import annotations

import hashlib
import os
import time
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from aegis.judge.attribution_head import AttributionHead
from aegis.judge.base import Judge, JudgeVerdict


# ─────────────────────────────────────────────────────────────────────
# Mode detection
# ─────────────────────────────────────────────────────────────────────
def _model_path() -> Path | None:
    raw = os.environ.get("AEGIS_JUDGE_MODEL_PATH", "").strip()
    return Path(raw) if raw else None


def _stub_forced() -> bool:
    return os.environ.get("AEGIS_JUDGE_LOCAL_PHI_STUB", "0") in (
        "1", "true", "True", "yes",
    )


# ─────────────────────────────────────────────────────────────────────
# Model SHA3 (cached)
# ─────────────────────────────────────────────────────────────────────
@lru_cache(maxsize=4)
def _hash_model_file(path_str: str) -> str:
    """SHA3-256 of the GGUF file, chunked so multi-GB files don't OOM."""
    h = hashlib.sha3_256()
    with Path(path_str).open("rb") as fh:
        for chunk in iter(lambda: fh.read(64 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def reset_model_hash_cache() -> None:
    """Test helper — drop the cached SHA3 so a re-pointed model picks up."""
    _hash_model_file.cache_clear()


# ─────────────────────────────────────────────────────────────────────
# Stub deterministic seeded scoring (used when GGUF absent)
# ─────────────────────────────────────────────────────────────────────
_STUB_SEED = "aegis-local-phi-stub-v1-2026-04-28"


def _stub_model_hash() -> str:
    """Deterministic hash for the stub mode."""
    return hashlib.sha3_256(_STUB_SEED.encode()).hexdigest()


def _stub_evaluate(
    summary: str, attribution: AttributionHead, atv: Any, inp: Any
) -> tuple[Literal["ALLOW", "BLOCK", "REQUIRE_APPROVAL"], float, str]:
    """Stub uses the M13 attribution head as a ground-truth proxy.

    The reasoning: if the user has the local-phi judge configured but
    no actual model file, we'd rather give them M13's deterministic
    verdict than dummy regex. Stub mode is therefore "M13 with a Phi-
    flavored reason string" — same answer, deterministic, audit-clean.
    """
    if atv is not None:
        v = attribution.evaluate_full(summary, atv=atv, inp=inp)
    else:
        v = attribution.evaluate(summary)
    decision = v.decision
    confidence = v.confidence if v.confidence else 0.6
    if decision == "BLOCK":
        if v.subfield_attribution:
            top = max(v.subfield_attribution.items(), key=lambda kv: kv[1])
            reason = (
                f"local-phi (stub): risky tool call — top contributor {top[0]}"
            )
        else:
            reason = "local-phi (stub): regex matched destructive keyword"
    elif decision == "REQUIRE_APPROVAL":
        reason = "local-phi (stub): high-impact action, requesting human review"
    else:
        reason = "local-phi (stub): nominal request"
    return decision, confidence, reason


# ─────────────────────────────────────────────────────────────────────
# Real Phi loader (lazy)
# ─────────────────────────────────────────────────────────────────────
@lru_cache(maxsize=2)
def _load_real_phi(model_path_str: str) -> Any:
    """Load llama-cpp Llama instance with deterministic flags.

    Cached at process scope so a sidecar serves multiple requests
    without re-loading. Returns ``None`` if llama-cpp-python is not
    installed or the model fails to open.
    """
    try:
        from llama_cpp import Llama  # type: ignore[import-not-found]
    except ImportError:
        return None
    try:
        return Llama(
            model_path=model_path_str,
            n_ctx=2048,
            seed=42,
            verbose=False,
        )
    except Exception:  # noqa: BLE001 - downstream returns None for any load failure
        return None


def _real_evaluate(
    llm: Any,
    summary: str,
    attribution_dict: dict[str, float],
) -> tuple[Literal["ALLOW", "BLOCK", "REQUIRE_APPROVAL"], float, str]:
    """Run real Phi-4-mini-q4 with deterministic flags.

    Prompt format embeds the M13 attribution dict so the LM has the
    structured signal alongside the text summary. Output is a JSON
    line we parse for {decision, reason}.
    """
    prompt = _build_prompt(summary, attribution_dict)
    out = llm(
        prompt,
        max_tokens=64,
        temperature=0.0,
        top_k=1,
        top_p=1.0,
        repeat_penalty=1.0,
        stop=["\n\n"],
    )
    text = (out.get("choices", [{}])[0].get("text") or "").strip()
    return _parse_real_decode(text)


def _build_prompt(summary: str, attribution: dict[str, float]) -> str:
    top = sorted(attribution.items(), key=lambda kv: -kv[1])[:5]
    attr_lines = "\n".join(f"  {name}: {score:.2f}" for name, score in top)
    return (
        "You are AegisData's local sLLM judge. Decide ALLOW / BLOCK / "
        "REQUIRE_APPROVAL for this agent tool call.\n\n"
        f"Summary:\n{summary}\n\n"
        f"Top attribution scores (M13 head):\n{attr_lines}\n\n"
        'Respond with JSON: {"decision": "...", "reason": "..."}\n'
        "JSON: "
    )


def _parse_real_decode(
    text: str,
) -> tuple[Literal["ALLOW", "BLOCK", "REQUIRE_APPROVAL"], float, str]:
    import json
    import re

    match = re.search(r"\{[^{}]*\}", text)
    if match:
        try:
            payload = json.loads(match.group(0))
            decision_raw = str(payload.get("decision", "")).upper()
            if decision_raw in {"ALLOW", "BLOCK", "REQUIRE_APPROVAL"}:
                decision: Literal["ALLOW", "BLOCK", "REQUIRE_APPROVAL"] = (
                    decision_raw  # type: ignore[assignment]
                )
                reason = str(payload.get("reason") or "local-phi: " + decision_raw)
                return decision, 0.7, reason
        except (json.JSONDecodeError, ValueError):
            pass
    upper = text.upper()
    if "BLOCK" in upper:
        return "BLOCK", 0.5, f"local-phi (parsed): {text[:80]}"
    if "REQUIRE_APPROVAL" in upper or "APPROVAL" in upper:
        return "REQUIRE_APPROVAL", 0.5, f"local-phi (parsed): {text[:80]}"
    return "ALLOW", 0.5, f"local-phi (parsed): {text[:80]}"


# ─────────────────────────────────────────────────────────────────────
# Judge implementation
# ─────────────────────────────────────────────────────────────────────
class LocalPhiJudge(Judge):
    """v2.6 — local quantized LLM with deterministic stub fallback."""

    def __init__(self) -> None:
        self._attribution_head: AttributionHead | None = None

    def _attribution(self) -> AttributionHead:
        if self._attribution_head is None:
            self._attribution_head = AttributionHead()
        return self._attribution_head

    def _decide_mode(self) -> tuple[str, str | None]:
        """Return ``("real", path)`` | ``("stub", None)`` |
        ``("disabled", reason)``."""
        if _stub_forced():
            return "stub", None
        path = _model_path()
        if path is None:
            return "stub", None
        if not path.exists():
            return "disabled", f"AEGIS_JUDGE_MODEL_PATH={path} does not exist"
        llm = _load_real_phi(str(path))
        if llm is None:
            return "disabled", (
                "llama-cpp-python missing or model failed to load; run "
                "`uv pip install llama-cpp-python` and ensure the GGUF "
                "file path is valid."
            )
        return "real", str(path)

    @property
    def model_hash(self) -> str:
        path = _model_path()
        if path is not None and path.exists() and not _stub_forced():
            return _hash_model_file(str(path))
        return _stub_model_hash()

    def evaluate(self, summary: str) -> JudgeVerdict:
        return self.evaluate_full(summary, atv=None, inp=None)

    def evaluate_full(
        self, summary: str, *, atv: Any = None, inp: Any = None
    ) -> JudgeVerdict:
        t0 = time.perf_counter_ns()
        mode, info = self._decide_mode()

        decision: Literal["ALLOW", "BLOCK", "REQUIRE_APPROVAL"]
        confidence: float
        reason: str
        model_hash: str

        if mode == "stub":
            decision, confidence, reason = _stub_evaluate(
                summary, self._attribution(), atv, inp
            )
            model_hash = _stub_model_hash()
        elif mode == "real":
            assert info is not None
            attr_dict: dict[str, float] = {}
            if atv is not None:
                attr_v = self._attribution().evaluate_full(
                    summary, atv=atv, inp=inp
                )
                attr_dict = attr_v.subfield_attribution
            llm = _load_real_phi(info)
            decision, confidence, reason = _real_evaluate(llm, summary, attr_dict)
            model_hash = _hash_model_file(info)
        else:
            decision = "ALLOW"
            confidence = 0.0
            reason = f"local-phi disabled: {info}"
            model_hash = _stub_model_hash()

        elapsed_ms = (time.perf_counter_ns() - t0) / 1_000_000
        return JudgeVerdict(
            decision=decision,
            confidence=confidence,
            reason=reason,
            model_hash=model_hash,
            latency_ms=round(elapsed_ms, 3),
        )
