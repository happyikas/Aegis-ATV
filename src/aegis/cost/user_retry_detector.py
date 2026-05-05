"""User retry detector — surface "the agent failed and the user
is asking again" as a forensic signal.

When a Claude Code session ends mid-task, the user often types a
follow-up that's semantically close to the original prompt
("try again", "this didn't work — fix it", "you missed X").
That's a strong inefficiency signal: the agent burned tokens but
didn't actually solve the user's problem.

Detection
---------

Simple by default — Jaccard similarity over tokenised words. Cheap,
deterministic, no LLM dependency. If the operator opted into BGE
local embeddings (`AEGIS_EMBEDDING_PROVIDER=bge-local` from PR #25),
we use cosine over the BGE 768-D vector — semantically richer.

Default Jaccard threshold = 0.5 (reasonable starting point that
catches "redo this" / "try again with X" but not unrelated prompts).
Operators can tune via constructor arg.

Privacy
-------

Default: only `prompt_hash` (16-char SHA3) + `prompt_size_bytes`
+ `similarity` + `is_retry` flag land in the audit chain. The raw
prompt text never persists.

Opt-in: ``AEGIS_USER_PROMPT_CAPTURE_PREVIEW=1`` records the first
80 chars of the prompt — useful for debug, off by default.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

# Method-aware defaults — BGE-cosine and Jaccard have different
# similarity-score distributions. Same-but-paraphrased prompts
# typically hit ~0.85 under BGE (semantic similarity is high even
# with word-level edits) but only ~0.5 under Jaccard (which is
# strict word-overlap). A single 0.5 threshold under-triggers on
# Jaccard borderline cases and over-triggers spectacularly under
# BGE — so we pick the threshold AFTER the method is known.
DEFAULT_JACCARD_THRESHOLD: float = 0.5
DEFAULT_BGE_THRESHOLD: float = 0.85

# Backwards compatibility — preserve the public constant name.
# Reads as "the Jaccard default" since that's the unconditional
# fallback. Code that explicitly imports DEFAULT_RETRY_THRESHOLD
# (e.g., tests) keeps working unchanged.
DEFAULT_RETRY_THRESHOLD: float = DEFAULT_JACCARD_THRESHOLD

PREVIEW_ENV: str = "AEGIS_USER_PROMPT_CAPTURE_PREVIEW"
PREVIEW_MAX_CHARS: int = 80

# Cheap ASCII-friendly tokeniser. Good enough for English / code text.
_WORD_RE = re.compile(r"\w+", re.UNICODE)


@dataclass
class RetryEvidence:
    """Per-prompt retry evaluation. ``is_retry`` is the operator-level
    signal; ``similarity`` + ``threshold`` + ``method`` give the
    forensic decomposition."""

    prompt_hash: str
    prompt_size_bytes: int
    prev_prompt_hash: str | None
    similarity: float = 0.0
    is_retry: bool = False
    threshold: float = DEFAULT_RETRY_THRESHOLD
    method: str = "jaccard"
    preview: str | None = None


def _stable_hash(s: str, length: int = 16) -> str:
    return hashlib.sha3_256(s.encode("utf-8")).hexdigest()[:length]


def _jaccard(a: str, b: str) -> float:
    """|A ∩ B| / |A ∪ B| over case-folded word tokens."""
    if not a or not b:
        return 0.0
    sa = {t.lower() for t in _WORD_RE.findall(a)}
    sb = {t.lower() for t in _WORD_RE.findall(b)}
    if not sa and not sb:
        return 0.0
    union = sa | sb
    if not union:
        return 0.0
    return len(sa & sb) / len(union)


def _last_user_prompt(transcript_path: Path) -> str:
    """Return the most recent user-message text content in the
    transcript. Empty string if none / unreadable.

    Excludes the *current* prompt — Claude Code writes the
    incoming prompt to the transcript BEFORE firing the
    UserPromptSubmit hook, so the current message is the LAST
    user turn. We want the one BEFORE that.
    """
    if not transcript_path or not transcript_path.is_file():
        return ""
    try:
        lines = transcript_path.read_text(
            encoding="utf-8"
        ).splitlines()
    except OSError:
        return ""
    user_texts: list[str] = []
    for raw in lines:
        if not raw.strip():
            continue
        try:
            ev = json.loads(raw)
        except json.JSONDecodeError:
            continue
        kind = ev.get("type") or ev.get("role") or ""
        if kind not in ("user", "human"):
            continue
        msg = ev.get("message") if isinstance(ev.get("message"), dict) else {}
        # Real Claude Code: message.content (str or list-of-blocks).
        # Test fixtures: top-level content (str).
        content = msg.get("content") or ev.get("content")
        if isinstance(content, list):
            text_parts = [
                blk.get("text", "")
                for blk in content
                if isinstance(blk, dict) and blk.get("type") == "text"
            ]
            text = " ".join(text_parts)
        elif isinstance(content, str):
            text = content
        else:
            text = ""
        if text.strip():
            user_texts.append(text.strip())
    # Penultimate = the prompt BEFORE the current one.
    if len(user_texts) >= 2:
        return user_texts[-2]
    return ""


def _bge_cosine(a: str, b: str) -> float | None:
    """If BGE local embedding is available (PR #25 wiring), use 768-D
    cosine. Returns ``None`` when BGE isn't configured so callers can
    fall back to Jaccard."""
    try:
        from aegis.config import settings

        if settings.aegis_embedding_provider != "bge-local":
            return None
        from aegis.atv.embeddings import get_provider

        provider = get_provider()
        va = provider.embed(a, 768)
        vb = provider.embed(b, 768)
        # cosine
        import numpy as np

        na = float(np.linalg.norm(va))
        nb = float(np.linalg.norm(vb))
        if na == 0 or nb == 0:
            return 0.0
        return float(np.dot(va, vb) / (na * nb))
    except Exception:  # noqa: BLE001
        return None


def detect_user_retry(
    *,
    current_prompt: str,
    transcript_path: Path | None,
    threshold: float | None = None,
    use_bge: bool | None = None,
) -> RetryEvidence:
    """Compare ``current_prompt`` to the previous user prompt in the
    transcript. Returns RetryEvidence with similarity + flag.

    ``threshold``:
      - ``None`` (default) — pick the threshold AFTER the method is
        known: 0.85 for BGE-cosine, 0.5 for Jaccard. This avoids the
        scale mismatch where a 0.5 threshold over-triggers under
        BGE (semantic similarity ~0.85 is the norm for unrelated
        same-domain prompts) and under-triggers under Jaccard
        (word-overlap can be very low for paraphrased retries).
      - explicit float — used regardless of method. Use this when
        you've calibrated a threshold against your specific deployment.

    ``use_bge``:
      - ``True``  → force BGE (raises if unconfigured)
      - ``False`` → force Jaccard
      - ``None``  → auto: BGE if available, else Jaccard
    """
    prompt_hash = _stable_hash(current_prompt or "")
    prompt_size = len(current_prompt.encode("utf-8")) if current_prompt else 0

    prev = ""
    if transcript_path is not None:
        prev = _last_user_prompt(transcript_path)

    prev_hash = _stable_hash(prev) if prev else None

    method: str
    similarity: float = 0.0
    if not current_prompt or not prev:
        method = "jaccard"
        similarity = 0.0
    else:
        bge_score: float | None = None
        if use_bge is not False:
            bge_score = _bge_cosine(current_prompt, prev)
        if bge_score is not None:
            method = "bge_cosine"
            similarity = max(0.0, min(1.0, bge_score))
        else:
            method = "jaccard"
            similarity = _jaccard(current_prompt, prev)

    # Auto-pick threshold by method when caller didn't override.
    if threshold is None:
        threshold = (
            DEFAULT_BGE_THRESHOLD if method == "bge_cosine"
            else DEFAULT_JACCARD_THRESHOLD
        )

    is_retry = similarity >= threshold

    preview: str | None = None
    if (
        os.environ.get(PREVIEW_ENV, "0") in ("1", "true", "True", "yes")
        and current_prompt
    ):
        preview = (
            current_prompt[:PREVIEW_MAX_CHARS]
            + ("…" if len(current_prompt) > PREVIEW_MAX_CHARS else "")
        )

    return RetryEvidence(
        prompt_hash=prompt_hash,
        prompt_size_bytes=prompt_size,
        prev_prompt_hash=prev_hash,
        similarity=similarity,
        is_retry=is_retry,
        threshold=threshold,
        method=method,
        preview=preview,
    )


def to_audit_record(
    aid: str, evidence: RetryEvidence, *, ts_ns: int | None = None,
) -> dict[str, Any]:
    """Wrap into the audit chain shape used by other hook records."""
    import time
    return {
        "ts_ns": ts_ns or time.time_ns(),
        "tool": "(user_prompt)",
        "aid": aid,
        "hook": "UserPromptSubmit",
        "mode": "local",
        "explain": {"user_retry": asdict(evidence)},
    }
