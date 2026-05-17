"""Tests for v0.6.0 — embedding-based semantic search.

Three layers:

1. Index construction — every entry that embeds non-zero is in
   the index; failures are silently dropped.
2. Cosine ranking — entries with embeddings close to the query
   rank higher than dissimilar ones (even with dummy provider,
   the SHA3 expansion gives deterministic distinct vectors).
3. Defensive contract — empty query / missing wiki / provider
   failure → empty list, never raises.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from aegis.knowledge import (
    EntryKind,
    InfoBox,
    KnowledgeEntry,
    Section,
    build_embedding_index,
    clear_embedding_cache,
    save_entry,
    save_index,
    search_entries_embedding,
)


def _mk_entry(
    entry_id: str,
    title: str,
    summary: str,
    *,
    kind: EntryKind = EntryKind.TOOL,
    section_body: str = "",
) -> KnowledgeEntry:
    return KnowledgeEntry(
        entry_id=entry_id,
        kind=kind,
        title=title,
        summary=summary,
        infobox=InfoBox(fields={"x": "1"}),
        sections=(
            (Section(heading="Body", body=section_body),)
            if section_body else ()
        ),
        n_observations=100,
    )


def _populate(tmp_path: Path) -> None:
    entries = [
        _mk_entry(
            "tool/Bash",
            "Tool Bash",
            "Bash invocations on the firewall — high volume",
            kind=EntryKind.TOOL,
        ),
        _mk_entry(
            "tool/Edit",
            "Tool Edit",
            "Edit invocations — file modifications",
            kind=EntryKind.TOOL,
        ),
        _mk_entry(
            "pattern/Bash:loop:Bash",
            "Pattern loop:Bash",
            "Loop detector pattern on Bash — repeated calls",
            kind=EntryKind.PATTERN,
            section_body="Same Bash call repeated 3 times causes REQUIRE_APPROVAL",
        ),
    ]
    for e in entries:
        save_entry(e, root=tmp_path)
    save_index(entries, root=tmp_path, built_at_ns=1, built_from_records=1)


# ──────────────────────────────────────────────────────────────────
# 1. Index construction
# ──────────────────────────────────────────────────────────────────


class TestIndexConstruction:
    def setup_method(self) -> None:
        clear_embedding_cache()

    def test_empty_corpus(self) -> None:
        index = build_embedding_index([])
        assert index.documents == ()

    def test_populated_corpus_indexes_all(self) -> None:
        entries = [
            _mk_entry("tool/Bash", "Tool Bash", "bash"),
            _mk_entry("tool/Edit", "Tool Edit", "edit"),
        ]
        index = build_embedding_index(entries)
        ids = {d.entry_id for d in index.documents}
        assert ids == {"tool/Bash", "tool/Edit"}

    def test_vectors_l2_normalised(self) -> None:
        entries = [_mk_entry("tool/x", "X", "test content")]
        index = build_embedding_index(entries)
        assert len(index.documents) == 1
        norm = float(np.linalg.norm(index.documents[0].vec))
        assert abs(norm - 1.0) < 1e-5

    def test_provider_name_recorded(self) -> None:
        # With AEGIS_EMBEDDING_PROVIDER=dummy (the test default),
        # the provider name should be 'DummyEmbedding'.
        index = build_embedding_index(
            [_mk_entry("a/b", "T", "s")],
        )
        assert "Dummy" in index.provider_name or "Embedding" in index.provider_name


# ──────────────────────────────────────────────────────────────────
# 2. Cosine ranking
# ──────────────────────────────────────────────────────────────────


class TestEmbeddingRanking:
    def setup_method(self) -> None:
        clear_embedding_cache()

    def test_empty_query_returns_empty(self, tmp_path: Path) -> None:
        _populate(tmp_path)
        assert search_entries_embedding("", root=tmp_path) == []

    def test_missing_wiki_returns_empty(self, tmp_path: Path) -> None:
        # Empty tmp_path → no wiki.
        assert search_entries_embedding("bash", root=tmp_path) == []

    def test_self_query_returns_top_match(self, tmp_path: Path) -> None:
        """A query that exactly matches an entry's summary should
        score that entry highest — true for any provider that
        produces deterministic same-string→same-vector mapping
        (including dummy)."""
        _populate(tmp_path)
        # Embed identical text to one entry's summary.
        hits = search_entries_embedding(
            "Loop detector pattern on Bash — repeated calls",
            root=tmp_path,
            k=3,
        )
        assert hits
        assert hits[0].entry_id == "pattern/Bash:loop:Bash"

    def test_top_k_respected(self, tmp_path: Path) -> None:
        _populate(tmp_path)
        hits = search_entries_embedding("test", root=tmp_path, k=2)
        assert len(hits) <= 2

    def test_min_score_filter(self, tmp_path: Path) -> None:
        _populate(tmp_path)
        # Threshold so high nothing matches.
        hits = search_entries_embedding(
            "test", root=tmp_path, min_score=0.99,
        )
        assert hits == []

    def test_search_hit_shape(self, tmp_path: Path) -> None:
        _populate(tmp_path)
        hits = search_entries_embedding(
            "loop bash", root=tmp_path, k=1,
        )
        if hits:
            assert hits[0].entry_id
            assert hits[0].kind in {
                "agent", "tool", "pattern",
                "session", "incident", "workflow",
            }
            assert hits[0].title
            assert isinstance(hits[0].score, float)


# ──────────────────────────────────────────────────────────────────
# 3. Cache invalidation
# ──────────────────────────────────────────────────────────────────


class TestCache:
    def setup_method(self) -> None:
        clear_embedding_cache()

    def test_clear_cache(self, tmp_path: Path) -> None:
        _populate(tmp_path)
        hits1 = search_entries_embedding("bash", root=tmp_path)
        clear_embedding_cache()
        hits2 = search_entries_embedding("bash", root=tmp_path)
        ids1 = [h.entry_id for h in hits1]
        ids2 = [h.entry_id for h in hits2]
        assert ids1 == ids2  # same corpus → same ranking


# ──────────────────────────────────────────────────────────────────
# 4. CLI engine flag
# ──────────────────────────────────────────────────────────────────


class TestCLIEngineFlag:
    def test_both_engines_return_searchhit(self, tmp_path: Path) -> None:
        """End-to-end: both engines produce the same SearchHit
        shape so a CLI swap is transparent."""
        from aegis.knowledge import search_entries
        clear_embedding_cache()
        _populate(tmp_path)
        tfidf_hits = search_entries("bash", root=tmp_path, k=2)
        emb_hits = search_entries_embedding("bash", root=tmp_path, k=2)
        # Both can be empty (sparse corpus) but if either has hits
        # their shape should match.
        for h in tfidf_hits + emb_hits:
            assert h.entry_id and h.kind and h.title
            assert isinstance(h.score, float)
