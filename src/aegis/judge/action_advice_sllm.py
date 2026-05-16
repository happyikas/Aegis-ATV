"""ActionAdvice sLLM brain (PR-ζ-head, v0.5.9).

Closes production gap #3 from the v0.5.6 self-audit. Until now
``compose_advice_heuristic`` was the only composer wired — fast,
deterministic, but unable to phrase the *reason* / *hint* fields
beyond a small template. This module adds an sLLM-driven composer
that uses the same backend as the firewall judge (HybridJudge /
LocalPhi / Haiku) to *enhance* the heuristic baseline:

* The heuristic baseline still runs first — it provides a fast,
  always-available fallback and a "seed" for the LLM call so the
  sLLM only has to refine the wording, not invent fields.
* If an LLM is available (LocalPhi GGUF present OR Anthropic key
  configured), call it with a tight prompt asking for ENHANCED
  ``reason`` + ``next_action_hint`` + ``alternative_tool`` as JSON.
* Parse the response defensively — markdown fences, trailing
  prose, missing fields all degrade gracefully back to the
  heuristic baseline.
* The returned ``ActionAdvice`` carries ``advisor_kind="sllm"`` +
  the model hash when sLLM enhancement succeeded;
  ``advisor_kind="heuristic"`` when we fell back (so audit /
  forensics can tell the two apart by record).

Design decisions
----------------

* **Heuristic-first, sLLM-enhanced**: cheap field generation
  (decision, confidence, cited_anomalies, recommended_advisors)
  stays heuristic — those fields are deterministic and tightly
  CI-tested. The sLLM only touches *prose* fields (``reason``,
  ``next_action_hint``, ``alternative_tool``). This keeps the
  decision-class invariants under deterministic-test coverage
  while still letting operators benefit from natural-language
  refinement.

* **No new LLM client**: the sLLM call piggybacks on the
  ``llm_call`` callable injected by the caller (or defaulted to
  the configured ``AEGIS_JUDGE_PROVIDER``). The Judge interface
  itself doesn't grow — we sidestep its verdict-shaped contract
  by going one level lower to the LLM-client surface that
  LocalPhi / Haiku already expose.

* **Opt-in by env**: ``AEGIS_ACTION_ADVICE_PROVIDER=sllm`` flips
  the umbrella :func:`compose_advice` (added below) to call this
  module's sLLM composer; default ``heuristic`` preserves v0.5.8
  behavior byte-for-byte.

* **Test-friendly**: the LLM call is parameterised
  (``llm_call`` is a callable), so unit tests pass a stub that
  returns canned JSON — no Anthropic / llama-cpp dependency
  needed for CI.

What this module does NOT do
----------------------------

* Re-derive the decision class. The heuristic baseline's
  ALLOW/BLOCK/REQUIRE_APPROVAL/DEFER stays authoritative — sLLM
  output can't lift a BLOCK to ALLOW. The patent's "sLLM
  understands the scene" claim is about prose enrichment, not
  about subverting the verdict gate.
* Re-derive cited_anomalies / cited_turns_rel / recommended_advisors.
  Those are traceability fields with hard contracts — heuristic
  alone produces them so audit can replay without the model.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections.abc import Callable
from dataclasses import replace
from typing import Any, Final

from aegis.judge.action_advice import (
    ActionAdvice,
    compose_advice_heuristic,
)

# Marker version → SHA3 hash → advisor_hash. Bump on any prompt or
# parser change so audit / replay can pin advice to the composer
# revision that produced it.
_SLLM_VERSION: Final[str] = "compose_advice_sllm_v1"
_SLLM_HASH: Final[str] = hashlib.sha3_256(_SLLM_VERSION.encode()).hexdigest()

# JSON keys the parser will accept from the LLM. Extra keys are
# silently ignored; missing keys fall back to the heuristic value.
_SLLM_FIELDS: Final[frozenset[str]] = frozenset(
    {"reason", "next_action_hint", "alternative_tool"}
)


# ──────────────────────────────────────────────────────────────────
# Prompt builder
# ──────────────────────────────────────────────────────────────────


def _build_prompt(baseline: ActionAdvice, *, current_tool: str = "") -> str:
    """Compose a tight, structured prompt for the sLLM enhancement.

    The heuristic ``baseline`` is included so the LLM has full
    context: cited anomalies, current decision, the heuristic's
    own wording. We ask for a JSON object with three optional
    prose fields — anything else is ignored.
    """
    anomalies = ", ".join(baseline.cited_anomalies) or "none"
    turns = ", ".join(str(t) for t in baseline.cited_turns_rel) or "none"
    return (
        "You are an agent-action advisor for a security firewall. "
        "Given the heuristic baseline below, produce a JSON object "
        "with up to three keys: 'reason' (one sentence explaining "
        "the decision in operator-friendly language), "
        "'next_action_hint' (concrete next step for the agent, or "
        "null), and 'alternative_tool' (a safer tool name to "
        "suggest, or null).\n"
        "\n"
        "Constraints:\n"
        "  - Do NOT change the decision verdict.\n"
        "  - Cite anomalies by name when relevant.\n"
        "  - Keep each field under 200 characters.\n"
        "  - Output ONLY the JSON object, no prose.\n"
        "\n"
        f"Heuristic baseline:\n"
        f"  decision: {baseline.decision}\n"
        f"  confidence: {baseline.confidence:.2f}\n"
        f"  reason: {baseline.reason}\n"
        f"  next_action_hint: {baseline.next_action_hint or 'null'}\n"
        f"  alternative_tool: {baseline.alternative_tool or 'null'}\n"
        f"  cited_anomalies: [{anomalies}]\n"
        f"  cited_turns_rel: [{turns}]\n"
        f"  current_tool: {current_tool!r}\n"
        "\n"
        "JSON response:"
    )


# ──────────────────────────────────────────────────────────────────
# Response parser — robust to markdown fences + trailing prose
# ──────────────────────────────────────────────────────────────────


_JSON_FENCE_RE: Final[re.Pattern[str]] = re.compile(
    r"```(?:json)?\s*([\s\S]*?)```", re.IGNORECASE,
)


def _extract_json_blob(text: str) -> str | None:
    """Pull a JSON object out of a possibly-noisy LLM response.

    Accepts:
      - plain JSON: ``{"reason": "..."}``
      - fenced JSON: ``\\`\\`\\`json\\n{...}\\n\\`\\`\\```
      - JSON followed by trailing prose
      - JSON preceded by a leading prose line

    Returns the raw JSON string (no parsing yet) or ``None`` if
    nothing JSON-looking is found.
    """
    if not text:
        return None
    # Fenced form first.
    m = _JSON_FENCE_RE.search(text)
    if m:
        return m.group(1).strip()
    # Otherwise look for the first { … } balanced span.
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    for i in range(start, len(text)):
        ch = text[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def _parse_sllm_response(
    response_text: str | None,
    *,
    baseline: ActionAdvice,
) -> tuple[ActionAdvice, bool]:
    """Parse the LLM response into a refined ActionAdvice.

    Returns ``(advice, used_sllm)``. ``used_sllm=True`` indicates a
    successful parse that materially changed at least one prose
    field; ``False`` means the LLM was silent / unparseable and the
    baseline is returned as-is.
    """
    if not response_text:
        return baseline, False
    blob = _extract_json_blob(response_text)
    if blob is None:
        return baseline, False
    try:
        parsed = json.loads(blob)
    except (json.JSONDecodeError, ValueError):
        return baseline, False
    if not isinstance(parsed, dict):
        return baseline, False

    # Pull fields with type + length guards.
    def _str_field(key: str, max_len: int = 400) -> str | None:
        v = parsed.get(key)
        if v is None:
            return None
        if not isinstance(v, str):
            return None
        s = v.strip()
        if not s or s.lower() == "null":
            return None
        return s[:max_len]

    new_reason = _str_field("reason", max_len=400) or baseline.reason
    new_hint = _str_field("next_action_hint", max_len=300)
    new_alt = _str_field("alternative_tool", max_len=100)

    # If the LLM returned nothing useful, treat it as a no-op.
    changed_reason = new_reason != baseline.reason
    changed_hint = new_hint != baseline.next_action_hint
    changed_alt = new_alt != baseline.alternative_tool
    if not (changed_reason or changed_hint or changed_alt):
        return baseline, False

    refined = replace(
        baseline,
        reason=new_reason,
        next_action_hint=new_hint if changed_hint else baseline.next_action_hint,
        alternative_tool=new_alt if changed_alt else baseline.alternative_tool,
        advisor_kind="sllm",
        advisor_hash=_SLLM_HASH,
        produced_at_ns=time.time_ns(),
    )
    return refined, True


# ──────────────────────────────────────────────────────────────────
# Default LLM-call adapter
# ──────────────────────────────────────────────────────────────────


def _default_llm_call(prompt: str) -> str | None:
    """Best-effort sLLM call using whatever the env says is configured.

    Returns the raw text response, or ``None`` when no LLM is
    available (dummy provider, missing model file, missing API
    key, import error). Caller treats ``None`` as "fall back to
    heuristic" — never raises.

    Side note: we intentionally don't pin to a specific
    provider. ``AEGIS_JUDGE_PROVIDER`` already selects what
    inference backend the runtime can reach; we honor that here
    so operators don't have to configure two separate provider
    knobs.
    """
    provider = os.environ.get("AEGIS_JUDGE_PROVIDER", "").lower().strip()

    if provider == "haiku":
        try:
            return _haiku_completion(prompt)
        except Exception:  # noqa: BLE001 — never raise from advisor
            return None

    if provider in ("local-phi", "phi", "local_phi"):
        try:
            return _local_phi_completion(prompt)
        except Exception:  # noqa: BLE001
            return None

    # Hybrid uses M13 + Phi + Haiku internally; for advice we pick
    # the Haiku path when an API key is set, else Phi when a GGUF
    # is reachable, else None. Falling back to None is fine —
    # caller returns the heuristic baseline.
    if provider == "hybrid":
        if os.environ.get("ANTHROPIC_API_KEY"):
            try:
                return _haiku_completion(prompt)
            except Exception:  # noqa: BLE001
                pass
        try:
            return _local_phi_completion(prompt)
        except Exception:  # noqa: BLE001
            return None

    # Dummy or unset → no real LLM, return None so caller uses heuristic.
    return None


def _haiku_completion(prompt: str) -> str | None:
    """Call Anthropic Haiku via the existing HaikuJudge's client.

    Lazy-imports so the action-advice module stays usable in
    environments without the anthropic SDK installed.
    """
    try:
        from anthropic import Anthropic
    except ImportError:
        return None
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None
    try:
        client = Anthropic(api_key=key)
        msg = client.messages.create(
            model="claude-3-5-haiku-20241022",
            max_tokens=400,
            temperature=0.0,    # deterministic
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception:  # noqa: BLE001
        return None
    # Anthropic returns a structured response; we want the text body.
    try:
        text = msg.content[0].text  # type: ignore[union-attr]
        return str(text) if text is not None else None
    except Exception:  # noqa: BLE001
        return None


def _local_phi_completion(prompt: str) -> str | None:
    """Call the local llama-cpp model used by LocalPhiJudge."""
    model_path = os.environ.get("AEGIS_JUDGE_MODEL_PATH", "").strip()
    if not model_path:
        return None
    try:
        from aegis.judge.local_phi import _load_real_phi
    except ImportError:
        return None
    llm = _load_real_phi(model_path)
    if llm is None:
        return None
    try:
        out = llm(
            prompt,
            max_tokens=400,
            temperature=0.0,
            stop=["```", "\n\n\n"],
        )
    except Exception:  # noqa: BLE001
        return None
    try:
        # llama-cpp returns dict-like {"choices": [{"text": "..."}]}.
        text = out["choices"][0]["text"]
        return str(text) if text is not None else None
    except (KeyError, IndexError, TypeError):
        return None


# ──────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────


def compose_advice_sllm(
    *,
    llm_call: Callable[[str], str | None] | None = None,
    **kwargs: Any,
) -> ActionAdvice:
    """Compose an ActionAdvice using the heuristic baseline +
    sLLM prose refinement.

    ``llm_call`` is a callable ``(prompt) -> response_text | None``.
    Default uses :func:`_default_llm_call` which dispatches based
    on ``AEGIS_JUDGE_PROVIDER``. Tests pass a stub that returns
    canned JSON.

    All other keyword arguments are passed through to
    :func:`compose_advice_heuristic` so the call sites stay
    interchangeable.

    Behavior:
      1. Run the heuristic composer to get the baseline.
      2. If no LLM available, return the baseline as-is.
      3. Build a structured prompt + call the LLM.
      4. Parse the response; on any failure, return the baseline.
      5. On success, return the refined advice with
         ``advisor_kind="sllm"`` and ``advisor_hash=<sLLM hash>``.

    Never raises — the firewall hot path can't tolerate an
    exception from the advisor.
    """
    baseline = compose_advice_heuristic(**kwargs)

    caller = llm_call if llm_call is not None else _default_llm_call
    try:
        prompt = _build_prompt(
            baseline, current_tool=kwargs.get("current_tool", ""),
        )
        response = caller(prompt)
    except Exception:  # noqa: BLE001 — never raise from advisor
        return baseline

    refined, used_sllm = _parse_sllm_response(response, baseline=baseline)
    if not used_sllm:
        return baseline
    return refined


def compose_advice(
    *,
    prefer_sllm: bool | None = None,
    llm_call: Callable[[str], str | None] | None = None,
    **kwargs: Any,
) -> ActionAdvice:
    """Umbrella composer — picks heuristic vs sLLM based on env.

    Selection rules (in order):
      1. Explicit ``prefer_sllm`` keyword wins.
      2. Otherwise, ``AEGIS_ACTION_ADVICE_PROVIDER=sllm`` →
         sLLM composer; anything else → heuristic.
      3. Default is heuristic (preserves v0.5.8 byte-for-byte
         behavior — opt-in upgrade).

    Use this instead of :func:`compose_advice_heuristic` /
    :func:`compose_advice_sllm` from new call sites so the
    provider switch is centralised.
    """
    if prefer_sllm is None:
        env = os.environ.get(
            "AEGIS_ACTION_ADVICE_PROVIDER", "",
        ).lower().strip()
        prefer_sllm = env == "sllm"
    if prefer_sllm:
        return compose_advice_sllm(llm_call=llm_call, **kwargs)
    return compose_advice_heuristic(**kwargs)


__all__ = [
    "compose_advice",
    "compose_advice_sllm",
]
