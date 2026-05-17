"""Tests for v0.5.21 — TF-IDF semantic search over wiki entries.

Three layers:

1. Index construction — token IDF, doc TF, L2 norms.
2. Query → ranking — relevant entries score higher than unrelated.
3. Edge cases — empty index, empty query, no shared terms.
"""

from __future__ import annotations

from pathlib import Path

from aegis.knowledge import (
    EntryKind,
    InfoBox,
    KnowledgeEntry,
    Section,
    build_tfidf_index,
    clear_search_cache,
    save_entry,
    save_index,
    search_entries,
)
from aegis.knowledge.search import _tokenise

# ──────────────────────────────────────────────────────────────────
# Tokeniser
# ──────────────────────────────────────────────────────────────────


class TestTokeniser:
    def test_basic_split(self) -> None:
        assert _tokenise("Hello World") == ["hello", "world"]

    def test_stopwords_dropped(self) -> None:
        # "the" should not appear; "agent" should.
        tokens = _tokenise("the agent and the tool")
        assert "the" not in tokens
        assert "and" not in tokens
        assert "agent" in tokens
        assert "tool" in tokens

    def test_preserves_colon_and_dash(self) -> None:
        tokens = _tokenise("loop:Bash and rule-fired")
        assert "loop:bash" in tokens
        assert "rule-fired" in tokens

    def test_drops_single_chars_and_numbers(self) -> None:
        tokens = _tokenise("a b 123 longer")
        assert tokens == ["longer"]

    def test_empty(self) -> None:
        assert _tokenise("") == []


# ──────────────────────────────────────────────────────────────────
# Index construction
# ──────────────────────────────────────────────────────────────────


def _mk_entry(
    entry_id: str,
    title: str,
    summary: str,
    *,
    kind: EntryKind = EntryKind.TOOL,
    section_body: str = "",
    tags: tuple[str, ...] = (),
) -> KnowledgeEntry:
    return KnowledgeEntry(
        entry_id=entry_id,
        kind=kind,
        title=title,
        summary=summary,
        infobox=InfoBox(fields={"sample": "value"}),
        sections=(
            (Section(heading="Body", body=section_body),)
            if section_body else ()
        ),
        tags=tags,
        n_observations=100,
    )


class TestIndexConstruction:
    def test_empty_corpus(self) -> None:
        index = build_tfidf_index([])
        assert index.n_documents == 0
        assert index.documents == ()
        assert index.idf == {}

    def test_single_document(self) -> None:
        entry = _mk_entry("tool/Bash", "Tool Bash", "bash invocations")
        index = build_tfidf_index([entry])
        assert index.n_documents == 1
        assert len(index.documents) == 1
        # "bash" should appear in the doc's TF vector.
        assert "bash" in index.documents[0].tf

    def test_idf_decreases_with_corpus_frequency(self) -> None:
        common = _mk_entry("a/1", "T", "bash bash bash")
        rare = _mk_entry("a/2", "T", "obscure-token")
        index = build_tfidf_index([common, rare, _mk_entry("a/3", "T", "bash again")])
        # "bash" appears in 2 of 3 docs, "obscure-token" in 1 — IDF
        # of obscure should be higher.
        assert index.idf["obscure-token"] > index.idf["bash"]


# ──────────────────────────────────────────────────────────────────
# Search ranking
# ──────────────────────────────────────────────────────────────────


def _populate_wiki(tmp_path: Path) -> None:
    entries = [
        _mk_entry(
            "tool/Bash",
            "Tool Bash",
            "Bash invocations on the firewall — high volume, frequent loops",
            kind=EntryKind.TOOL,
            tags=("high-volume",),
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
            "Loop detector pattern on Bash — repeated calls trigger approval",
            kind=EntryKind.PATTERN,
            section_body="Same Bash call repeated 3 times causes REQUIRE_APPROVAL",
        ),
        _mk_entry(
            "incident/foo/blk1",
            "Incident on Bash",
            "BLOCK due to dangerous-pattern rule violation",
            kind=EntryKind.INCIDENT,
        ),
        _mk_entry(
            "agent/quiet-bot",
            "Agent quiet-bot",
            "Agent with cost-divergence escalations on Bash",
            kind=EntryKind.AGENT,
        ),
    ]
    for e in entries:
        save_entry(e, root=tmp_path)
    save_index(entries, root=tmp_path, built_at_ns=1, built_from_records=1)


class TestSearchRanking:
    def setup_method(self) -> None:
        clear_search_cache()

    def test_empty_query_returns_empty(self, tmp_path: Path) -> None:
        _populate_wiki(tmp_path)
        assert search_entries("", root=tmp_path) == []
        assert search_entries("   ", root=tmp_path) == []

    def test_missing_wiki_returns_empty(self, tmp_path: Path) -> None:
        # No wiki built.
        assert search_entries("bash", root=tmp_path) == []

    def test_relevant_entry_ranks_top(self, tmp_path: Path) -> None:
        _populate_wiki(tmp_path)
        hits = search_entries("loop bash", root=tmp_path, k=5)
        assert len(hits) > 0
        # Pattern entry mentions "loop" + "Bash" most explicitly.
        top_ids = [h.entry_id for h in hits[:2]]
        assert "pattern/Bash:loop:Bash" in top_ids

    def test_distinct_topics_separated(self, tmp_path: Path) -> None:
        _populate_wiki(tmp_path)
        edit_hits = search_entries("edit modifications", root=tmp_path)
        assert edit_hits[0].entry_id == "tool/Edit"

    def test_unseen_tokens_no_match(self, tmp_path: Path) -> None:
        _populate_wiki(tmp_path)
        hits = search_entries(
            "completely-unrelated-quantum-flux",
            root=tmp_path,
        )
        assert hits == []

    def test_top_k_respected(self, tmp_path: Path) -> None:
        _populate_wiki(tmp_path)
        hits = search_entries("bash", root=tmp_path, k=2)
        assert len(hits) <= 2

    def test_min_score_filter(self, tmp_path: Path) -> None:
        _populate_wiki(tmp_path)
        # Very high floor → drops noisy matches.
        hits_strict = search_entries(
            "bash", root=tmp_path, min_score=0.99,
        )
        hits_loose = search_entries(
            "bash", root=tmp_path, min_score=0.0,
        )
        assert len(hits_strict) <= len(hits_loose)

    def test_search_returns_metadata(self, tmp_path: Path) -> None:
        _populate_wiki(tmp_path)
        hits = search_entries("loop bash", root=tmp_path, k=1)
        assert hits[0].kind in {"agent", "tool", "pattern", "session",
                                "incident", "workflow"}
        assert hits[0].title
        assert hits[0].score > 0


# ──────────────────────────────────────────────────────────────────
# Cache invalidation
# ──────────────────────────────────────────────────────────────────


class TestCacheInvalidation:
    def setup_method(self) -> None:
        clear_search_cache()

    def test_clear_cache_forces_rebuild(self, tmp_path: Path) -> None:
        _populate_wiki(tmp_path)
        hits1 = search_entries("bash", root=tmp_path)
        clear_search_cache()
        hits2 = search_entries("bash", root=tmp_path)
        # Same results because the corpus didn't change.
        ids1 = [h.entry_id for h in hits1]
        ids2 = [h.entry_id for h in hits2]
        assert ids1 == ids2
