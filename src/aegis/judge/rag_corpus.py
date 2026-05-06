"""RAG corpus loader — JSONL chunks under ``policies/rag_corpus/``.

Phase 1 of the RAG-grounded sLLM judge: this module **only** loads and
validates the corpus. It does NOT compute embeddings or perform
retrieval — that is the responsibility of ``aegis.judge.rag_retrieval``
(PR 2).

Schema (``policies/rag_corpus/README.md`` is authoritative):

* ``id``        — unique stable identifier across all three files.
* ``category``  — one of ``rule`` / ``playbook`` / ``baseline``.
* ``title``     — short heading.
* ``content``   — body text the model sees.
* ``tags``      — optional list of strings.
* ``policy_rule`` — optional cross-reference (``rule:fs_destructive`` …).
* ``decision``  — optional ``ALLOW`` / ``BLOCK`` / ``REQUIRE_APPROVAL``.

Behavioural contract:

* Loader is **stdlib-only** — no embedding, no model, no I/O outside
  reading three JSONL files.
* Returns a frozen ``RagCorpus`` even when the directory is missing
  (``RagCorpus(chunks=())``) — never raises on a missing repo.
* Duplicate IDs across the three files are a hard error
  (``ValueError``); within a single file pytest catches it via the
  schema test.
"""
from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Literal

ChunkCategory = Literal["rule", "playbook", "baseline"]
ChunkDecision = Literal["ALLOW", "BLOCK", "REQUIRE_APPROVAL"]

_VALID_CATEGORIES: frozenset[str] = frozenset({"rule", "playbook", "baseline"})
_VALID_DECISIONS: frozenset[str] = frozenset(
    {"ALLOW", "BLOCK", "REQUIRE_APPROVAL"}
)
_FILES: tuple[tuple[str, ChunkCategory], ...] = (
    ("rules.jsonl", "rule"),
    ("playbooks.jsonl", "playbook"),
    ("baselines.jsonl", "baseline"),
)


@dataclass(frozen=True)
class RagChunk:
    id: str
    category: ChunkCategory
    title: str
    content: str
    tags: tuple[str, ...] = ()
    policy_rule: str | None = None
    decision: ChunkDecision | None = None

    def render_for_prompt(self) -> str:
        """Return the chunk in the form the model sees in-context."""
        head = f"[{self.category}] {self.title}"
        if self.policy_rule:
            head += f"  ({self.policy_rule})"
        if self.decision:
            head += f"  → {self.decision}"
        return f"{head}\n{self.content}"


@dataclass(frozen=True)
class RagCorpus:
    chunks: tuple[RagChunk, ...] = ()
    source_dir: Path | None = None

    @property
    def is_empty(self) -> bool:
        return not self.chunks

    def by_id(self, chunk_id: str) -> RagChunk | None:
        for c in self.chunks:
            if c.id == chunk_id:
                return c
        return None

    def by_category(self, category: ChunkCategory) -> tuple[RagChunk, ...]:
        return tuple(c for c in self.chunks if c.category == category)

    def by_tag(self, tag: str) -> tuple[RagChunk, ...]:
        return tuple(c for c in self.chunks if tag in c.tags)


def _validate_chunk(raw: dict[str, object], src: Path) -> RagChunk:
    for required in ("id", "category", "title", "content"):
        if required not in raw or not isinstance(raw[required], str):
            raise ValueError(
                f"{src}: missing or non-string field {required!r} in {raw}"
            )
    cid = str(raw["id"])
    category = str(raw["category"])
    if category not in _VALID_CATEGORIES:
        raise ValueError(
            f"{src}: chunk {cid!r} has invalid category {category!r}; "
            f"expected one of {sorted(_VALID_CATEGORIES)}"
        )
    decision = raw.get("decision")
    if decision is not None and (
        not isinstance(decision, str) or decision not in _VALID_DECISIONS
    ):
        raise ValueError(
            f"{src}: chunk {cid!r} has invalid decision {decision!r}"
        )
    tags_field = raw.get("tags", [])
    if not isinstance(tags_field, list) or any(
        not isinstance(t, str) for t in tags_field
    ):
        raise ValueError(
            f"{src}: chunk {cid!r} tags must be a list of strings"
        )
    policy_rule = raw.get("policy_rule")
    if policy_rule is not None and not isinstance(policy_rule, str):
        raise ValueError(
            f"{src}: chunk {cid!r} policy_rule must be a string or null"
        )
    return RagChunk(
        id=cid,
        category=category,  # type: ignore[arg-type]
        title=str(raw["title"]),
        content=str(raw["content"]),
        tags=tuple(tags_field),
        policy_rule=policy_rule,
        decision=decision,  # type: ignore[arg-type]
    )


def _read_jsonl(path: Path, expected_category: ChunkCategory) -> list[RagChunk]:
    if not path.is_file():
        return []
    out: list[RagChunk] = []
    for ln, raw_line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"{path}:{ln}: invalid JSON: {exc.msg}"
            ) from exc
        chunk = _validate_chunk(obj, path)
        if chunk.category != expected_category:
            raise ValueError(
                f"{path}:{ln}: chunk {chunk.id!r} has category "
                f"{chunk.category!r} but file expects "
                f"{expected_category!r}"
            )
        out.append(chunk)
    return out


def load_corpus(corpus_dir: Path) -> RagCorpus:
    """Load all three JSONL files under ``corpus_dir`` and return a
    frozen ``RagCorpus``.

    Missing directory → empty corpus (not an error). Missing individual
    files → that section just contributes zero chunks. JSON / schema
    errors raise ``ValueError`` with file-line context.
    """
    chunks: list[RagChunk] = []
    if corpus_dir.is_dir():
        for filename, category in _FILES:
            chunks.extend(_read_jsonl(corpus_dir / filename, category))

    seen: dict[str, str] = {}
    for c in chunks:
        if c.id in seen:
            raise ValueError(
                f"duplicate chunk id {c.id!r} (also in category "
                f"{seen[c.id]!r}); ids must be unique across all files"
            )
        seen[c.id] = c.category

    return RagCorpus(chunks=tuple(chunks), source_dir=corpus_dir)


def default_corpus_dir() -> Path:
    """Repo-relative ``policies/rag_corpus/``. Resolves from this file."""
    return (Path(__file__).resolve().parents[3] / "policies" / "rag_corpus")


@lru_cache(maxsize=1)
def load_default_corpus() -> RagCorpus:
    """Cached convenience wrapper for the default corpus path."""
    return load_corpus(default_corpus_dir())


def reset_corpus_cache() -> None:
    """Test helper — clear the lru_cache so policies/ edits take effect."""
    load_default_corpus.cache_clear()


def render_chunks_for_prompt(
    chunks: Sequence[RagChunk], *, max_chars: int = 2000,
) -> str:
    """Concatenate chunks into a prompt block, capped at ``max_chars``.

    Trimming policy: include whole chunks until adding the next would
    overflow. Never truncate mid-chunk — the model finds half-sentences
    confusing.
    """
    if not chunks:
        return ""
    pieces: list[str] = []
    used = 0
    for c in chunks:
        rendered = c.render_for_prompt()
        addition = len(rendered) + 2  # account for "\n\n"
        if used + addition > max_chars and pieces:
            break
        pieces.append(rendered)
        used += addition
    return "\n\n".join(pieces)


def categories_summary(corpus: RagCorpus) -> dict[str, int]:
    """Used by `aegis report` and the test_rag_corpus schema test."""
    out: dict[str, int] = {"rule": 0, "playbook": 0, "baseline": 0}
    for c in corpus.chunks:
        out[c.category] = out.get(c.category, 0) + 1
    return out


__all__: tuple[str, ...] = (
    "RagChunk",
    "RagCorpus",
    "ChunkCategory",
    "ChunkDecision",
    "load_corpus",
    "load_default_corpus",
    "default_corpus_dir",
    "reset_corpus_cache",
    "render_chunks_for_prompt",
    "categories_summary",
)


# Silence unused-import warnings for re-exports of typing aliases
_ = (Iterable, field)
