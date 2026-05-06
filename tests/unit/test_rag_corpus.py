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
    RagCorpus,
    categories_summary,
    chunks_valid_at,
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


# ── TestValidityWindows (PR #94) ──────────────────────────────────────


def _ns(iso: str) -> int:
    """Test helper: ISO → nanoseconds."""
    from datetime import datetime
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return int(dt.timestamp() * 1_000_000_000)


class TestValidityWindows:
    def test_chunk_with_no_window_is_always_valid(self) -> None:
        c = RagChunk(id="x", category="rule", title="t", content="c")
        assert c.is_valid_at(_ns("2020-01-01T00:00:00Z"))
        assert c.is_valid_at(_ns("2030-01-01T00:00:00Z"))

    def test_valid_from_is_inclusive(self) -> None:
        c = RagChunk(
            id="x", category="rule", title="t", content="c",
            valid_from="2024-08-01T00:00:00Z",
        )
        assert not c.is_valid_at(_ns("2024-07-31T23:59:59Z"))
        assert c.is_valid_at(_ns("2024-08-01T00:00:00Z"))      # inclusive
        assert c.is_valid_at(_ns("2024-09-01T00:00:00Z"))

    def test_valid_until_is_exclusive(self) -> None:
        c = RagChunk(
            id="x", category="rule", title="t", content="c",
            valid_until="2024-08-01T00:00:00Z",
        )
        assert c.is_valid_at(_ns("2024-07-31T23:59:59Z"))
        assert not c.is_valid_at(_ns("2024-08-01T00:00:00Z"))   # exclusive
        assert not c.is_valid_at(_ns("2024-09-01T00:00:00Z"))

    def test_window_both_bounds(self) -> None:
        c = RagChunk(
            id="x", category="rule", title="t", content="c",
            valid_from="2024-01-01T00:00:00Z",
            valid_until="2024-12-31T00:00:00Z",
        )
        assert not c.is_valid_at(_ns("2023-12-31T23:59:59Z"))
        assert c.is_valid_at(_ns("2024-06-15T00:00:00Z"))
        assert not c.is_valid_at(_ns("2024-12-31T00:00:00Z"))

    def test_corpus_valid_at_filters_chunks(self) -> None:
        old = RagChunk(
            id="rule-v0", category="rule", title="v0", content="old",
            valid_from="2024-01-01T00:00:00Z",
            valid_until="2024-08-01T00:00:00Z",
        )
        new = RagChunk(
            id="rule-v1", category="rule", title="v1", content="new",
            valid_from="2024-08-01T00:00:00Z",
            supersedes="rule-v0",
        )
        timeless = RagChunk(
            id="rule-t", category="rule", title="t", content="timeless",
        )
        corpus = RagCorpus(chunks=(old, new, timeless))

        # 2024-06: old + timeless are valid, new isn't yet.
        view = corpus.valid_at(_ns("2024-06-01T00:00:00Z"))
        assert {c.id for c in view.chunks} == {"rule-v0", "rule-t"}

        # 2025-01: new + timeless are valid; old has expired.
        view = corpus.valid_at(_ns("2025-01-01T00:00:00Z"))
        assert {c.id for c in view.chunks} == {"rule-v1", "rule-t"}

        # 2023-12: only timeless (old not in effect yet).
        view = corpus.valid_at(_ns("2023-12-31T00:00:00Z"))
        assert {c.id for c in view.chunks} == {"rule-t"}

    def test_chunks_valid_at_module_helper(self) -> None:
        old = RagChunk(
            id="a", category="rule", title="t", content="c",
            valid_until="2024-01-01T00:00:00Z",
        )
        live = RagChunk(id="b", category="rule", title="t", content="c")
        out = chunks_valid_at(
            (old, live), ts_ns=_ns("2024-06-01T00:00:00Z"),
        )
        assert [c.id for c in out] == ["b"]

    def test_corpus_valid_at_default_now_uses_current_time(self) -> None:
        # Future-only chunk: shouldn't show up at "now".
        future = RagChunk(
            id="future", category="rule", title="f", content="f",
            valid_from="2999-01-01T00:00:00Z",
        )
        timeless = RagChunk(
            id="timeless", category="rule", title="t", content="t",
        )
        corpus = RagCorpus(chunks=(future, timeless))
        view = corpus.valid_at()  # default → now()
        assert {c.id for c in view.chunks} == {"timeless"}

    def test_loader_accepts_valid_window_fields(self, tmp_path: Path) -> None:
        chunk = {
            "id": "rule-x", "category": "rule",
            "title": "t", "content": "c",
            "valid_from": "2024-08-01T00:00:00Z",
            "valid_until": "2025-08-01T00:00:00Z",
            "supersedes": "rule-x-v0",
        }
        (tmp_path / "rules.jsonl").write_text(
            json.dumps(chunk), encoding="utf-8",
        )
        corpus = load_corpus(tmp_path)
        c = corpus.chunks[0]
        assert c.valid_from == "2024-08-01T00:00:00Z"
        assert c.valid_until == "2025-08-01T00:00:00Z"
        assert c.supersedes == "rule-x-v0"

    def test_loader_rejects_malformed_timestamp(self, tmp_path: Path) -> None:
        chunk = {
            "id": "rule-x", "category": "rule",
            "title": "t", "content": "c",
            "valid_from": "yesterday",  # not ISO 8601
        }
        (tmp_path / "rules.jsonl").write_text(
            json.dumps(chunk), encoding="utf-8",
        )
        with pytest.raises(ValueError, match="not a valid ISO 8601"):
            load_corpus(tmp_path)

    def test_loader_rejects_naive_timestamp(self, tmp_path: Path) -> None:
        chunk = {
            "id": "rule-x", "category": "rule",
            "title": "t", "content": "c",
            "valid_from": "2024-08-01T00:00:00",   # missing tz
        }
        (tmp_path / "rules.jsonl").write_text(
            json.dumps(chunk), encoding="utf-8",
        )
        with pytest.raises(ValueError, match="must include timezone"):
            load_corpus(tmp_path)

    def test_loader_rejects_inverted_window(self, tmp_path: Path) -> None:
        chunk = {
            "id": "rule-x", "category": "rule",
            "title": "t", "content": "c",
            "valid_from": "2024-08-01T00:00:00Z",
            "valid_until": "2024-01-01T00:00:00Z",  # before valid_from
        }
        (tmp_path / "rules.jsonl").write_text(
            json.dumps(chunk), encoding="utf-8",
        )
        with pytest.raises(ValueError, match="must be strictly after"):
            load_corpus(tmp_path)

    def test_loader_rejects_non_string_supersedes(self, tmp_path: Path) -> None:
        chunk = {
            "id": "rule-x", "category": "rule",
            "title": "t", "content": "c",
            "supersedes": 42,
        }
        (tmp_path / "rules.jsonl").write_text(
            json.dumps(chunk), encoding="utf-8",
        )
        with pytest.raises(ValueError, match="supersedes"):
            load_corpus(tmp_path)

    def test_shipped_corpus_loads_under_validity_filter(self) -> None:
        """The 38 shipped chunks have no validity windows yet — they
        must remain visible under valid_at(now)."""
        reset_corpus_cache()
        corpus = load_default_corpus()
        n_before = len(corpus.chunks)
        view = corpus.valid_at()
        assert len(view.chunks) == n_before
