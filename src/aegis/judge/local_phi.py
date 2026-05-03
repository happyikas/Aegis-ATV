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
        # ``llama_cpp`` is the optional ``aegis-mvp[local-llm]`` extra; CI
        # doesn't install it (mypy reports import-not-found) but local
        # dev with the extra installed has the symbol available
        # (mypy then reports unused-ignore). Suppress both.
        from llama_cpp import Llama  # type: ignore[import-not-found,unused-ignore]
    except ImportError:
        return None
    try:
        return Llama(
            model_path=model_path_str,
            n_ctx=2048,        # ample for our few-shot prompt (~600 toks)
            seed=42,
            n_threads=4,       # 4 CPU threads is the M1/M2 sweet spot
            n_gpu_layers=-1,   # offload all layers to GPU (Metal/CUDA);
                               # silently degrades to CPU if llama-cpp
                               # was built without GPU support. ~4–8×
                               # speedup on M1 vs CPU-only.
            verbose=False,
        )
    except Exception:  # noqa: BLE001 - downstream returns None for any load failure
        return None


def _real_evaluate(
    llm: Any,
    summary: str,
    attribution_dict: dict[str, float],
    rag_block: str = "",
) -> tuple[Literal["ALLOW", "BLOCK", "REQUIRE_APPROVAL"], float, str]:
    """Run real Phi-4-mini-q4 with deterministic flags.

    Prompt format embeds the M13 attribution dict + (optional) RAG
    block of similar past cases so the LM has the structured signal
    alongside the text summary. Output is a JSON line we parse for
    {decision, reason}.
    """
    prompt = _build_prompt(summary, attribution_dict, rag_block=rag_block)
    out = llm(
        prompt,
        # 200 tokens accommodates two distinct output styles we see in
        # the wild: Llama-3.2-1B emits raw JSON in <30 tokens; Phi-3.5-
        # mini wraps in markdown ```json fences and prepends a blank
        # line plus a longer reason. We stop at ``` (closes the
        # markdown block) or `}\n` (closes the raw JSON line) — the
        # parser then tolerates either form.
        max_tokens=200,
        temperature=0.0,      # greedy → deterministic
        top_k=1,
        top_p=1.0,
        repeat_penalty=1.0,
        stop=["```\n", "}\n\n", "}}"],
    )
    text = (out.get("choices", [{}])[0].get("text") or "").strip()
    return _parse_real_decode(text)


def _build_rag_block(
    atv: Any, inp: Any, summary: str,
) -> str:
    """Retrieve similar past cases via the BGE-derived agent_state slice.

    Returns an empty string when:
    * the case memory is empty (no npz / `aegis case-memory build` not
      run yet);
    * BGE-local is not the active embedding provider (the
      ``agent_state_embedding`` slice is then SHA3 noise — meaningless
      cosines);
    * any retrieval error occurs (the prompt builder degrades silently
      to "no RAG block" rather than crashing the firewall).

    The returned string is already trimmed by
    :func:`format_cases_for_prompt` — drops in directly between rubric
    and format example.
    """
    try:
        from aegis.config import settings
        from aegis.judge.case_memory import (
            format_cases_for_prompt,
            load_default_memory,
        )
        from aegis.schema import SLICE_AGENT_STATE_EMBEDDING

        # RAG only adds value when BGE provides a real semantic signal.
        if settings.aegis_embedding_provider != "bge-local":
            return ""

        memory = load_default_memory()
        if memory.is_empty:
            return ""
        if atv is None:
            return ""

        import numpy as np

        query = np.asarray(atv[SLICE_AGENT_STATE_EMBEDDING], dtype=np.float32)
        if query.size != memory.dim:
            return ""

        cases = memory.search(query, k=3)
        return format_cases_for_prompt(cases)
    except Exception:  # noqa: BLE001 — RAG must never block judge
        return ""


_RUBRIC = """\
Rules (apply in order):
1. BLOCK if the call clearly destroys data, exfiltrates credentials,
   or modifies security-critical infrastructure.
2. REQUIRE_APPROVAL if the call is high-impact but recoverable
   (database mutation, deploy, system config change).
3. ALLOW otherwise — read-only, build/test, ordinary file edits, etc.
4. When the M13 attribution top score is below 0.30, default to ALLOW
   unless the command text shows a clear destructive verb.
"""

# A SINGLE format example that doesn't leak content patterns the model
# might copy verbatim. Empirically Llama-3.2-1B at greedy-decode tends
# to regurgitate the first few-shot example's content, so we keep
# the example minimal and use a rubric for the actual judgment.
_FORMAT_EXAMPLE = (
    'JSON format: {"decision": "ALLOW|BLOCK|REQUIRE_APPROVAL", '
    '"reason": "<one short sentence>"}'
)


def _build_prompt(
    summary: str,
    attribution: dict[str, float],
    rag_block: str = "",
) -> str:
    """Build the prompt for the local sLLM.

    Tuned for Llama-3.2-1B-Instruct-Q4_K_M (Solo Free default):

    * **Rubric over examples.** 1B-class models at greedy-decode copy
      the first few-shot example's content verbatim instead of
      reasoning. We replace the few-shot block with a 4-rule rubric +
      a single format-only example.
    * **Single-line JSON.** ``stop=["\\n"]`` etc. forces termination at
      the first newline so we don't read past the JSON object.
    * **Top attribution embedded.** The M13 head's top-5 contributors
      give the model a structured prior — same signal step340 already
      computed, so the model isn't re-deriving from raw text.
    * **RAG block (optional).** When the case memory is loaded and BGE
      embeddings are configured, the most-similar past cases are
      injected as labelled in-context examples. This is the
      patent's step340 RAG hook — empirically the single biggest
      Llama-1B accuracy lift, since 1B-class models pattern-match
      reliably even when they can't reason.

    Greedy decoding (temperature=0, top_k=1) + this prompt = bit-
    deterministic output for the same (summary, attribution, rag) tuple.
    """
    top = sorted(attribution.items(), key=lambda kv: -kv[1])[:5]
    attr_lines = ", ".join(f"{name}: {score:.2f}" for name, score in top)
    rag = f"{rag_block}\n" if rag_block else ""
    return (
        "You are AegisData's local sLLM judge for AI agent tool calls.\n"
        f"{_RUBRIC}\n"
        f"{rag}"
        f"{_FORMAT_EXAMPLE}\n\n"
        "Tool call to classify:\n"
        f"  summary: {summary}\n"
        f"  top M13 attribution: {attr_lines or '(none)'}\n\n"
        "Respond with one line of JSON. JSON: "
    )


def _strip_markdown_fence(text: str) -> str:
    """Pull the first JSON-looking object out of markdown-wrapped output.

    Phi-3.5-mini at greedy-decode emits things like::

        \\n\\n```json
        {"decision": "BLOCK", "reason": "..."}
        ```

    while Llama-3.2-1B emits raw JSON. Strip the fence + leading
    whitespace + an optional ``json`` tag, then return the first
    line that starts with ``{`` so the regex parser below finds it.
    """
    if "```" not in text:
        return text.strip()
    # Pull out the first fenced block — `json` tag optional.
    import re
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        return m.group(1).strip()
    # No closing fence (we may have stopped on ```\n earlier) — strip
    # opening fence and let the line-walker below find the JSON.
    cleaned = text.replace("```json", "").replace("```", "").strip()
    return cleaned


def _parse_real_decode(
    text: str,
) -> tuple[Literal["ALLOW", "BLOCK", "REQUIRE_APPROVAL"], float, str]:
    """Robustly extract decision + reason from the local sLLM's output.

    Handles four output styles observed in the wild:

    * Llama-3.2-1B raw JSON line
    * Phi-3.5-mini markdown-fenced ``\\n\\n```json\\n{...}\\n``` ``
    * Free-form prose containing the decision keyword
    * Empty / unparseable (last-resort low-conf ALLOW)

    Phi-3.5 in particular ALWAYS prepends ``\\n\\n``, then a markdown
    fence — :func:`_strip_markdown_fence` lifts the inner JSON out
    so the regex below treats both styles uniformly.

    Recovery cases:

    1. **Clean JSON line** — ``{"decision":"BLOCK","reason":"..."}``. Parsed
       directly, confidence 0.7 (LM is committing).
    2. **Markdown-fenced JSON** — ``\\n\\n\\`\\`\\`json\\n{...}\\n\\`\\`\\``` (Phi-3.5).
       Stripped before regex, then parsed directly.
    3. **Unterminated JSON** — ``{"decision":"BLOCK","reason":"..."``. We
       close the brace and retry.
    4. **No JSON / freeform** — model emitted prose. Falls back to substring
       match for the three decision keywords. Confidence 0.5.
    """
    import json
    import re

    # Strip markdown fences before regex extraction. No-op when the
    # output is raw JSON (Llama-1B style).
    text = _strip_markdown_fence(text)

    # Case 1 + 3: try to find a JSON object, completing it if needed.
    match = re.search(r"\{[^{}]*\}?", text)
    if match:
        candidate = match.group(0)
        if not candidate.rstrip().endswith("}"):
            candidate = candidate + "}"
        try:
            payload = json.loads(candidate)
            decision_raw = str(payload.get("decision", "")).upper().strip()
            if decision_raw in {"ALLOW", "BLOCK", "REQUIRE_APPROVAL"}:
                decision: Literal["ALLOW", "BLOCK", "REQUIRE_APPROVAL"] = (
                    decision_raw  # type: ignore[assignment]
                )
                reason = str(payload.get("reason") or "local-phi: " + decision_raw)
                return decision, 0.7, reason[:200]
        except (json.JSONDecodeError, ValueError):
            pass

    # Case 3: substring fallback.
    upper = text.upper()
    if "REQUIRE_APPROVAL" in upper or "REQUIRE APPROVAL" in upper:
        return "REQUIRE_APPROVAL", 0.5, f"local-phi (parsed): {text[:80]}"
    if "BLOCK" in upper:
        return "BLOCK", 0.5, f"local-phi (parsed): {text[:80]}"
    if "ALLOW" in upper:
        return "ALLOW", 0.5, f"local-phi (parsed): {text[:80]}"
    # Last resort: low-confidence ALLOW so the hybrid combiner escalates.
    return "ALLOW", 0.0, f"local-phi unparseable: {text[:80]}"


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

        # Daemon fast-path — bypass _decide_mode() entirely when the
        # sidecar daemon (PR #30) is reachable. _decide_mode() calls
        # _load_real_phi() to verify the GGUF can load, which is the
        # exact cold-load cost the daemon is meant to eliminate. If
        # the daemon is up, we know a real LLM is already loaded and
        # can serve us — skip the redundant in-process verification.
        if atv is not None and not _stub_forced():
            from aegis.judge.llm_daemon import DaemonClient

            client = DaemonClient()
            if client.is_running():
                # Compute attribution + RAG in-process (cheap), then
                # round-trip the LLM call to the daemon.
                #
                # NOTE: Local names here intentionally do NOT shadow the
                # in-process branch's ``attr_dict`` / ``reason`` (which
                # are declared below at "if mode == ...:" time). On
                # daemon-success we return early; on daemon-fail we
                # fall through and the in-process branch re-derives
                # what it needs.
                daemon_attr_v = self._attribution().evaluate_full(
                    summary, atv=atv, inp=inp,
                )
                daemon_attr_dict = daemon_attr_v.subfield_attribution
                daemon_rag_block = _build_rag_block(atv, inp, summary)
                daemon_resp = client.evaluate(
                    summary, daemon_attr_dict, daemon_rag_block,
                )
                if daemon_resp is not None:
                    daemon_reason = daemon_resp.reason
                    if daemon_rag_block:
                        daemon_reason = f"{daemon_reason}  [+RAG]"
                    daemon_reason = f"{daemon_reason}  [daemon]"
                    elapsed_ms = (time.perf_counter_ns() - t0) / 1_000_000
                    return JudgeVerdict(
                        decision=daemon_resp.decision,  # type: ignore[arg-type]
                        confidence=daemon_resp.confidence,
                        reason=daemon_reason,
                        model_hash=daemon_resp.model_hash,
                        latency_ms=round(elapsed_ms, 3),
                    )
                # Daemon advertised but call failed — fall through to
                # the slower-but-correct in-process path below.

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
            # Step340 RAG: retrieve similar past cases when BGE +
            # case memory are configured. Empty string falls back to
            # the no-RAG prompt (i.e., bit-identical to PR #21).
            rag_block = _build_rag_block(atv, inp, summary)

            # Try the long-running daemon first — it has the GGUF
            # already loaded, eliminating the per-subprocess cold
            # load (2 s for Llama-1B, 6.5 s for Phi-3.5 — the latter
            # exceeds Claude Code's 5 s hook timeout). When the
            # daemon isn't running or fails, silently fall back to
            # in-process loading; behaviour is bit-identical to
            # pre-PR-#30.
            from aegis.judge.llm_daemon import DaemonClient

            client = DaemonClient()
            daemon_resp = client.evaluate(summary, attr_dict, rag_block)
            if daemon_resp is not None:
                decision = daemon_resp.decision  # type: ignore[assignment]
                confidence = daemon_resp.confidence
                reason = daemon_resp.reason
                if rag_block:
                    reason = f"{reason}  [+RAG]"
                # Attribute the speed-up: daemon-served reasons get a
                # marker so dogfood + audit can distinguish daemon
                # vs in-process verdicts.
                reason = f"{reason}  [daemon]"
                model_hash = daemon_resp.model_hash
            else:
                llm = _load_real_phi(info)
                decision, confidence, reason = _real_evaluate(
                    llm, summary, attr_dict, rag_block=rag_block,
                )
                if rag_block:
                    reason = f"{reason}  [+RAG]"
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
