"""Step340 RAG case memory — nearest-neighbour retrieval for the sLLM prompt.

The Llama-1B Solo Free judge struggles to *reason* about novel patterns
(verified empirically in the PR #21 dogfood: 1/4 hand-curated cases).
What 1B-class models CAN do reliably is **pattern-match** — if you show
them the verdict on similar past calls, they parrot the right answer.

This module implements the retrieval half of step340 RAG: given the
current call's BGE embedding (the 768-D ``agent_state_embedding`` slice
of the ATV that PR #22 made semantic), find the top-K most similar
labelled past cases. The :class:`LocalPhiJudge` prompt builder then
injects them as in-context examples next to the rubric.

Architecture
------------
The case memory is a frozen ``.npz`` archive shipped alongside the
firewall:

* ``embeddings``    — ``(N, 768)`` float32, L2-normalised
* ``texts``         — array of `str`, the agent_state_text for each case
* ``labels``        — array of `str` ∈ {ALLOW, BLOCK, REQUIRE_APPROVAL}
* ``reasons``       — array of `str`, the verdict reason
* ``meta``          — single-element dict: provenance + build timestamp

Retrieval is a single matmul (``N × 768 @ 768``) — for the seed memory
of ~245 cases this is sub-millisecond on M1 CPU. Memory is cached at
process scope so repeated retrievals don't re-load the npz.

Sources
-------
Two ways to build the memory, matching the M13 trainer:

1. **Synthetic seed** (default) — embeds the 7-category corpus from
   :mod:`aegis.burnin.m13_data`. Same data the M13 trainer uses, so
   M13 + RAG vote on the same evidence.
2. **Shadow log** — embeds (label, text) pairs from
   ``~/.aegis/shadow.jsonl``. As real Burn-in Shadow data accrues,
   rebuilding the memory makes RAG suggestions more relevant to the
   user's actual workflow.

Both paths require a configured BGE encoder (``aegis-mvp[local-llm]``
+ ``aegis pull-model --model bge-base-en``). Without BGE, embeddings
are SHA3 noise — meaningless cosines, RAG would hallucinate.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import numpy as np

DEFAULT_CASE_MEMORY_PATH: Path = (
    Path(__file__).resolve().parents[3] / "models" / "case_memory_v1.npz"
)


@dataclass(frozen=True)
class RetrievedCase:
    """One nearest-neighbour result for the prompt block."""

    text: str
    label: str
    reason: str
    similarity: float   # cosine in [-1, 1], practically [0, 1] for normalised vecs


class CaseMemory:
    """Frozen labelled-case index with cosine top-K retrieval.

    Public surface:

    * :meth:`load` / :meth:`save` — npz round-trip.
    * :meth:`search` — top-K nearest neighbours by cosine similarity.
    * :meth:`build_from_corpus` — staticmethod that converts a
      :mod:`aegis.burnin.m13_data` corpus into a memory by embedding
      each example.

    Empty memories are valid: :meth:`search` returns ``[]``. This lets
    the RAG prompt-builder degrade silently when the npz is missing.
    """

    def __init__(
        self,
        embeddings: np.ndarray,
        texts: np.ndarray,
        labels: np.ndarray,
        reasons: np.ndarray,
        meta: dict[str, Any] | None = None,
    ) -> None:
        if embeddings.ndim != 2:
            raise ValueError(
                f"embeddings must be 2-D, got {embeddings.shape}"
            )
        n = embeddings.shape[0]
        if not (n == len(texts) == len(labels) == len(reasons)):
            raise ValueError(
                f"length mismatch: emb={n} texts={len(texts)} "
                f"labels={len(labels)} reasons={len(reasons)}"
            )
        self.embeddings = embeddings.astype(np.float32, copy=False)
        self.texts = np.asarray(texts, dtype=object)
        self.labels = np.asarray(labels, dtype=object)
        self.reasons = np.asarray(reasons, dtype=object)
        self.meta = dict(meta or {})

    # ── shape ───────────────────────────────────────────────────────
    @property
    def n(self) -> int:
        return int(self.embeddings.shape[0])

    @property
    def dim(self) -> int:
        # Read the second axis even when N=0 so callers can validate
        # query-vs-memory dim compat against an empty memory.
        if self.embeddings.ndim < 2:
            return 0
        return int(self.embeddings.shape[1])

    @property
    def is_empty(self) -> bool:
        return self.n == 0

    # ── persistence ────────────────────────────────────────────────
    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(
            path,
            embeddings=self.embeddings,
            texts=self.texts,
            labels=self.labels,
            reasons=self.reasons,
            meta=np.array([json.dumps(self.meta)], dtype=object),
        )

    @classmethod
    def load(cls, path: Path) -> CaseMemory:
        data = np.load(path, allow_pickle=True)
        meta_raw = data["meta"][0] if "meta" in data else "{}"
        try:
            meta = json.loads(meta_raw) if isinstance(meta_raw, str) else {}
        except json.JSONDecodeError:
            meta = {}
        return cls(
            embeddings=data["embeddings"],
            texts=data["texts"],
            labels=data["labels"],
            reasons=data["reasons"],
            meta=meta,
        )

    @classmethod
    def empty(cls, dim: int = 768) -> CaseMemory:
        """Construct an empty memory with the given embedding dim.

        Used as a no-op fallback when the npz is absent — search()
        returns []  and the prompt builder skips the RAG block.
        """
        return cls(
            embeddings=np.zeros((0, dim), dtype=np.float32),
            texts=np.array([], dtype=object),
            labels=np.array([], dtype=object),
            reasons=np.array([], dtype=object),
            meta={"empty": True},
        )

    # ── retrieval ──────────────────────────────────────────────────
    def search(
        self, query: np.ndarray, *, k: int = 3, min_similarity: float = 0.30,
    ) -> list[RetrievedCase]:
        """Top-K nearest neighbours by cosine similarity.

        Both the stored embeddings and the query are expected to be
        L2-normalised — BGELocalEmbedding emits unit vectors, and the
        memory builder also re-normalises before saving. With both
        normalised, ``cos = a @ b`` is a single matmul.

        ``min_similarity`` filters out vacuous neighbours so we don't
        inject noise — for a 768-D BGE embedding, < 0.30 cosine is
        roughly orthogonal (no semantic overlap).
        """
        if self.is_empty:
            return []
        if query.shape != (self.dim,):
            raise ValueError(
                f"query shape {query.shape} != memory dim {self.dim}"
            )
        # Defensive normalisation — caller may pass unnormalised vectors.
        norm = float(np.linalg.norm(query))
        q = query if norm == 0 else query / norm
        sims = self.embeddings @ q.astype(self.embeddings.dtype)
        order = np.argsort(-sims)[:k]
        out: list[RetrievedCase] = []
        for i in order:
            sim = float(sims[i])
            if sim < min_similarity:
                continue
            out.append(RetrievedCase(
                text=str(self.texts[i]),
                label=str(self.labels[i]),
                reason=str(self.reasons[i]),
                similarity=sim,
            ))
        return out

    # ── construction from corpus ───────────────────────────────────
    @classmethod
    def build_from_corpus(
        cls, corpus: list[Any],
        *, embed_provider: Any,
        meta: dict[str, Any] | None = None,
    ) -> CaseMemory:
        """Embed each example's agent-state text and stack into a memory.

        ``corpus`` is a list of objects that expose ``inp.agent_state_text``,
        ``inp.tool_name``, ``inp.tool_args_json``, ``label`` (str), and
        an optional ``reason`` (defaults to ``"<category>: <tool>"``).
        Designed to consume :class:`aegis.burnin.m13_data.LabeledExample`
        directly.

        ``embed_provider`` is anything with ``.embed(text, dim) -> np.ndarray``
        — typically :class:`aegis.atv.embeddings.BGELocalEmbedding`. We
        embed the concatenation ``"<state> | tool=<tool> args=<args>"``
        so the memory captures both intent and surface form.
        """
        if not corpus:
            return cls.empty(dim=768)

        texts: list[str] = []
        labels: list[str] = []
        reasons: list[str] = []
        embs: list[np.ndarray] = []

        for ex in corpus:
            inp = ex.inp
            text = (
                f"{inp.agent_state_text or '(no state)'}  | "
                f"tool={inp.tool_name}  args={inp.tool_args_json}"
            )
            label = ex.label
            reason = (
                getattr(ex, "reason", None)
                or f"{getattr(ex, 'category', 'corpus')}: tool={inp.tool_name}"
            )
            v = embed_provider.embed(text, 768)
            v = np.asarray(v, dtype=np.float32).ravel()
            n = float(np.linalg.norm(v))
            if n > 0:
                v = v / n
            texts.append(text[:240])    # cap to keep npz compact
            labels.append(str(label))
            reasons.append(str(reason)[:160])
            embs.append(v.astype(np.float32))

        full_meta = {
            "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "n": len(corpus),
            "source": "build_from_corpus",
            **(meta or {}),
        }
        return cls(
            embeddings=np.stack(embs),
            texts=np.array(texts, dtype=object),
            labels=np.array(labels, dtype=object),
            reasons=np.array(reasons, dtype=object),
            meta=full_meta,
        )


# ─────────────────────────────────────────────────────────────────────
# Process-scope cache
# ─────────────────────────────────────────────────────────────────────
@lru_cache(maxsize=2)
def load_default_memory() -> CaseMemory:
    """Load the canonical ``models/case_memory_v1.npz`` (or empty fallback).

    Cached at process scope so the npz is mmap-loaded once per Claude
    Code session, not per tool call. ``reset_memory_cache`` lets tests
    swap the file underneath.
    """
    if DEFAULT_CASE_MEMORY_PATH.exists():
        try:
            return CaseMemory.load(DEFAULT_CASE_MEMORY_PATH)
        except Exception:  # noqa: BLE001 — corrupt file → empty fallback
            return CaseMemory.empty()
    return CaseMemory.empty()


def reset_memory_cache() -> None:
    """Test helper — drop cached memory so a freshly-written npz is read."""
    load_default_memory.cache_clear()


# ─────────────────────────────────────────────────────────────────────
# Prompt formatting
# ─────────────────────────────────────────────────────────────────────
def format_cases_for_prompt(
    cases: list[RetrievedCase], *, max_chars: int = 600,
) -> str:
    """Render retrieved cases as a single-line-per-case block.

    Format chosen for Llama-1B specifically: it's known to copy from
    the *first* in-context example, so we put the most-similar case
    first and trim overly-long text + reason fields. The
    ``max_chars`` cap keeps the prompt bounded — adding 20 cases'
    worth of context would push the prompt past the 1B model's
    effective context window.
    """
    if not cases:
        return ""
    lines = ["Similar past cases (most-similar first):"]
    used = len(lines[0])
    for c in cases:
        line = (
            f"- [cos={c.similarity:.2f}] "
            f"{c.text[:120]} → {c.label} ({c.reason[:60]})"
        )
        if used + len(line) > max_chars:
            break
        lines.append(line)
        used += len(line) + 1
    return "\n".join(lines) + "\n"


__all__ = [
    "CaseMemory",
    "DEFAULT_CASE_MEMORY_PATH",
    "RetrievedCase",
    "format_cases_for_prompt",
    "load_default_memory",
    "reset_memory_cache",
]
