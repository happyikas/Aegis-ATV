"""Schema + loader tests for ``aegis.judge.rag_corpus``.

Three groups:

* ``TestSchema`` — validates the shipped ``policies/rag_corpus/`` corpus
  loads cleanly, has expected categories, and that no IDs collide.
* ``TestLoader`` — exercises the loader against synthetic corpora
  (missing dir, missing files, malformed JSON, schema violations).
* ``TestRender`` — verifies ``render_chunks_for_prompt`` respects the
  ``max_chars`` budget and never truncates mid-chunk.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from aegis.judge.rag_corpus import (
    RagChunk,
    categories_summary,
    default_corpus_dir,
    load_corpus,
    load_default_corpus,
    render_chunks_for_prompt,
    reset_corpus_cache,
)

# ── TestSchema ────────────────────────────────────────────────────────


class TestSchema:
    def setup_method(self) -> None:
        reset_corpus_cache()

    def test_default_corpus_loads(self) -> None:
        corpus = load_default_corpus()
        assert not corpus.is_empty, (
            "shipped corpus should have at least one chunk"
        )
        assert corpus.source_dir == default_corpus_dir()

    def test_default_corpus_has_all_categories(self) -> None:
        summary = categories_summary(load_default_corpus())
        assert summary["rule"] >= 20, (
            f"need ≥20 rule chunks, got {summary['rule']}"
        )
        assert summary["playbook"] >= 3, (
            f"need ≥3 playbook chunks, got {summary['playbook']}"
        )
        assert summary["baseline"] >= 1, (
            f"need ≥1 baseline chunk, got {summary['baseline']}"
        )

    def test_no_duplicate_ids(self) -> None:
        chunks = load_default_corpus().chunks
        ids = [c.id for c in chunks]
        assert len(ids) == len(set(ids)), (
            f"duplicate ids: {[i for i in ids if ids.count(i) > 1]}"
        )

    def test_chunk_ids_are_kebab_case(self) -> None:
        chunks = load_default_corpus().chunks
        for c in chunks:
            assert c.id == c.id.lower(), (
                f"chunk id {c.id!r} should be lowercase"
            )
            assert " " not in c.id, (
                f"chunk id {c.id!r} should not contain spaces"
            )

    def test_decisions_are_valid(self) -> None:
        for c in load_default_corpus().chunks:
            if c.decision is not None:
                assert c.decision in ("ALLOW", "BLOCK", "REQUIRE_APPROVAL")

    def test_content_under_size_budget(self) -> None:
        for c in load_default_corpus().chunks:
            assert len(c.content) <= 1200, (
                f"chunk {c.id!r} content {len(c.content)} chars exceeds "
                "1200-char budget; long chunks evict useful context"
            )

    def test_by_category_filters(self) -> None:
        corpus = load_default_corpus()
        rules = corpus.by_category("rule")
        playbooks = corpus.by_category("playbook")
        assert all(c.category == "rule" for c in rules)
        assert all(c.category == "playbook" for c in playbooks)
        assert len(rules) + len(playbooks) + len(
            corpus.by_category("baseline")
        ) == len(corpus.chunks)

    def test_by_id_lookup(self) -> None:
        corpus = load_default_corpus()
        c = corpus.by_id("rule-fs-destructive")
        assert c is not None
        assert c.category == "rule"
        assert corpus.by_id("does-not-exist") is None

    def test_by_tag_filter(self) -> None:
        corpus = load_default_corpus()
        cloud = corpus.by_tag("cloud")
        assert len(cloud) >= 5, "expected several cloud-tagged rules"
        for c in cloud:
            assert "cloud" in c.tags


# ── TestLoader ────────────────────────────────────────────────────────


class TestLoader:
    def test_missing_dir_returns_empty(self, tmp_path: Path) -> None:
        nope = tmp_path / "does_not_exist"
        corpus = load_corpus(nope)
        assert corpus.is_empty
        assert corpus.source_dir == nope

    def test_empty_files_return_empty(self, tmp_path: Path) -> None:
        for fn in ("rules.jsonl", "playbooks.jsonl", "baselines.jsonl"):
            (tmp_path / fn).write_text("", encoding="utf-8")
        corpus = load_corpus(tmp_path)
        assert corpus.is_empty

    def test_blank_and_comment_lines_skipped(self, tmp_path: Path) -> None:
        chunk = {
            "id": "rule-x", "category": "rule", "title": "x", "content": "y",
        }
        (tmp_path / "rules.jsonl").write_text(
            "\n# comment\n" + json.dumps(chunk) + "\n\n",
            encoding="utf-8",
        )
        corpus = load_corpus(tmp_path)
        assert len(corpus.chunks) == 1
        assert corpus.chunks[0].id == "rule-x"

    def test_invalid_json_raises(self, tmp_path: Path) -> None:
        (tmp_path / "rules.jsonl").write_text("{not json", encoding="utf-8")
        with pytest.raises(ValueError, match="invalid JSON"):
            load_corpus(tmp_path)

    def test_missing_required_field_raises(self, tmp_path: Path) -> None:
        bad = {"id": "x", "category": "rule", "title": "t"}  # missing content
        (tmp_path / "rules.jsonl").write_text(
            json.dumps(bad), encoding="utf-8"
        )
        with pytest.raises(ValueError, match="missing.*content"):
            load_corpus(tmp_path)

    def test_invalid_category_raises(self, tmp_path: Path) -> None:
        bad = {
            "id": "x", "category": "weird", "title": "t", "content": "c",
        }
        (tmp_path / "rules.jsonl").write_text(
            json.dumps(bad), encoding="utf-8"
        )
        with pytest.raises(ValueError, match="invalid category"):
            load_corpus(tmp_path)

    def test_category_mismatch_raises(self, tmp_path: Path) -> None:
        # 'rule' category in the playbooks file should fail.
        bad = {
            "id": "x", "category": "rule", "title": "t", "content": "c",
        }
        (tmp_path / "playbooks.jsonl").write_text(
            json.dumps(bad), encoding="utf-8"
        )
        with pytest.raises(ValueError, match="file expects 'playbook'"):
            load_corpus(tmp_path)

    def test_invalid_decision_raises(self, tmp_path: Path) -> None:
        bad = {
            "id": "x", "category": "rule", "title": "t", "content": "c",
            "decision": "MAYBE",
        }
        (tmp_path / "rules.jsonl").write_text(
            json.dumps(bad), encoding="utf-8"
        )
        with pytest.raises(ValueError, match="invalid decision"):
            load_corpus(tmp_path)

    def test_duplicate_ids_across_files_raise(self, tmp_path: Path) -> None:
        rule = {"id": "dup", "category": "rule", "title": "r", "content": "x"}
        playbook = {
            "id": "dup", "category": "playbook", "title": "p", "content": "y",
        }
        (tmp_path / "rules.jsonl").write_text(
            json.dumps(rule), encoding="utf-8"
        )
        (tmp_path / "playbooks.jsonl").write_text(
            json.dumps(playbook), encoding="utf-8"
        )
        with pytest.raises(ValueError, match="duplicate chunk id"):
            load_corpus(tmp_path)

    def test_invalid_tags_field_raises(self, tmp_path: Path) -> None:
        bad = {
            "id": "x", "category": "rule", "title": "t", "content": "c",
            "tags": "not-a-list",
        }
        (tmp_path / "rules.jsonl").write_text(
            json.dumps(bad), encoding="utf-8"
        )
        with pytest.raises(ValueError, match="tags must be a list"):
            load_corpus(tmp_path)


# ── TestRender ────────────────────────────────────────────────────────


class TestRender:
    def test_empty_returns_empty(self) -> None:
        assert render_chunks_for_prompt(()) == ""

    def test_single_chunk_renders_with_header(self) -> None:
        chunk = RagChunk(
            id="rule-x", category="rule",
            title="My rule", content="Body text.",
            policy_rule="rule:demo", decision="BLOCK",
        )
        out = render_chunks_for_prompt((chunk,))
        assert "[rule] My rule" in out
        assert "(rule:demo)" in out
        assert "→ BLOCK" in out
        assert "Body text." in out

    def test_max_chars_respected(self) -> None:
        chunks = tuple(
            RagChunk(
                id=f"id-{i}", category="rule",
                title=f"t{i}", content="X" * 500,
            )
            for i in range(5)
        )
        out = render_chunks_for_prompt(chunks, max_chars=1200)
        # 500-char body + ~30-char header → ~530 per chunk; 1200 budget
        # fits exactly 2 chunks.
        assert out.count("[rule]") == 2

    def test_first_chunk_always_included_even_if_oversize(self) -> None:
        big = RagChunk(
            id="big", category="rule",
            title="big", content="X" * 5000,
        )
        out = render_chunks_for_prompt((big,), max_chars=1000)
        assert out.count("[rule]") == 1
        assert "X" * 5000 in out  # never mid-chunk truncated

    def test_chunk_without_optional_fields(self) -> None:
        chunk = RagChunk(
            id="rule-y", category="playbook",
            title="Plain", content="No metadata.",
        )
        out = render_chunks_for_prompt((chunk,))
        assert "[playbook] Plain" in out
        assert "→" not in out
        assert "(rule:" not in out
