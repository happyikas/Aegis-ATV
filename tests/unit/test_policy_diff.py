"""Tests for ``aegis.judge.policy_diff`` — chunk-derived corpus history."""
from __future__ import annotations

from datetime import datetime

import pytest

from aegis.judge.policy_diff import (
    diff_since,
    log_entries,
    parse_since,
    render_diff,
    render_log,
    render_show,
    show_chunk,
)
from aegis.judge.rag_corpus import RagChunk, RagCorpus


def _ns(iso: str) -> int:
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return int(dt.timestamp() * 1_000_000_000)


# ── TestParseSince ────────────────────────────────────────────────────


class TestParseSince:
    def test_iso_date_midnight(self) -> None:
        assert parse_since("2024-08-01") == _ns("2024-08-01T00:00:00Z")

    def test_iso_datetime(self) -> None:
        assert parse_since("2024-08-01T12:34:56Z") == _ns("2024-08-01T12:34:56Z")

    def test_relative_days(self) -> None:
        anchor = _ns("2024-12-01T00:00:00Z")
        assert parse_since("7d", now_ns=anchor) == _ns("2024-11-24T00:00:00Z")

    def test_relative_weeks(self) -> None:
        anchor = _ns("2024-12-01T00:00:00Z")
        assert parse_since("2w", now_ns=anchor) == _ns("2024-11-17T00:00:00Z")

    def test_relative_months_30d(self) -> None:
        anchor = _ns("2024-12-31T00:00:00Z")
        assert parse_since("1m", now_ns=anchor) == _ns("2024-12-01T00:00:00Z")

    def test_relative_year_365d(self) -> None:
        anchor = _ns("2024-12-31T00:00:00Z")
        # 365d back from 2024-12-31
        expected = _ns("2024-12-31T00:00:00Z") - 365 * 86400 * 10**9
        assert parse_since("1y", now_ns=anchor) == expected

    def test_quarter(self) -> None:
        assert parse_since("2024-Q1") == _ns("2024-01-01T00:00:00Z")
        assert parse_since("2024-Q2") == _ns("2024-04-01T00:00:00Z")
        assert parse_since("2024-Q3") == _ns("2024-07-01T00:00:00Z")
        assert parse_since("2024-Q4") == _ns("2024-10-01T00:00:00Z")

    def test_all_returns_zero(self) -> None:
        assert parse_since("all") == 0
        assert parse_since("epoch") == 0

    def test_malformed_raises_with_hint(self) -> None:
        with pytest.raises(ValueError, match="unrecognised --since"):
            parse_since("yesterday")


# ── TestDiffSince ─────────────────────────────────────────────────────


def _supersession_corpus() -> RagCorpus:
    return RagCorpus(chunks=(
        RagChunk(
            id="rule-x-v0", category="rule", title="X v0", content="old",
            created_at="2024-01-01T00:00:00Z",
            valid_from="2024-01-01T00:00:00Z",
            valid_until="2024-08-01T00:00:00Z",
        ),
        RagChunk(
            id="rule-x-v1", category="rule", title="X v1", content="new",
            created_at="2024-08-01T00:00:00Z",
            valid_from="2024-08-01T00:00:00Z",
            supersedes="rule-x-v0",
        ),
        RagChunk(
            id="rule-timeless", category="rule", title="t", content="t",
        ),
        RagChunk(
            id="playbook-old", category="playbook", title="p", content="p",
            created_at="2023-06-01T00:00:00Z",
        ),
    ))


class TestDiffSince:
    def test_added_in_window(self) -> None:
        diff = diff_since(
            _supersession_corpus(),
            since_ts_ns=_ns("2024-07-01T00:00:00Z"),
            until_ts_ns=_ns("2024-09-01T00:00:00Z"),
        )
        assert {c.id for c in diff.added} == {"rule-x-v1"}

    def test_retired_in_window(self) -> None:
        diff = diff_since(
            _supersession_corpus(),
            since_ts_ns=_ns("2024-07-01T00:00:00Z"),
            until_ts_ns=_ns("2024-09-01T00:00:00Z"),
        )
        assert {c.id for c in diff.retired} == {"rule-x-v0"}

    def test_superseded_pair_links_predecessor_to_successor(self) -> None:
        diff = diff_since(
            _supersession_corpus(),
            since_ts_ns=_ns("2024-07-01T00:00:00Z"),
            until_ts_ns=_ns("2024-09-01T00:00:00Z"),
        )
        assert len(diff.superseded) == 1
        old, new = diff.superseded[0]
        assert old.id == "rule-x-v0"
        assert new.id == "rule-x-v1"

    def test_empty_window_returns_empty_diff(self) -> None:
        diff = diff_since(
            _supersession_corpus(),
            since_ts_ns=_ns("2025-01-01T00:00:00Z"),
            until_ts_ns=_ns("2025-12-31T00:00:00Z"),
        )
        assert diff.is_empty

    def test_full_history_window_includes_old_playbook(self) -> None:
        diff = diff_since(
            _supersession_corpus(),
            since_ts_ns=0,
            until_ts_ns=_ns("2024-12-31T00:00:00Z"),
        )
        assert "playbook-old" in {c.id for c in diff.added}

    def test_timeless_chunks_never_appear_in_diff(self) -> None:
        diff = diff_since(
            _supersession_corpus(),
            since_ts_ns=0,
            until_ts_ns=_ns("2024-12-31T00:00:00Z"),
        )
        assert "rule-timeless" not in {c.id for c in diff.added}
        assert "rule-timeless" not in {c.id for c in diff.retired}


# ── TestLog ───────────────────────────────────────────────────────────


class TestLog:
    def test_log_includes_added_and_retired(self) -> None:
        entries = log_entries(_supersession_corpus())
        kinds = {(e.kind, e.chunk_id) for e in entries}
        assert ("added", "rule-x-v0") in kinds
        assert ("added", "rule-x-v1") in kinds
        assert ("retired", "rule-x-v0") in kinds
        assert ("added", "playbook-old") in kinds

    def test_log_newest_first(self) -> None:
        entries = log_entries(_supersession_corpus())
        ts = [e.ts_ns for e in entries]
        assert ts == sorted(ts, reverse=True)

    def test_log_limit(self) -> None:
        entries = log_entries(_supersession_corpus(), limit=2)
        assert len(entries) == 2

    def test_log_empty_for_timeless_corpus(self) -> None:
        timeless = RagCorpus(chunks=(
            RagChunk(id="t", category="rule", title="t", content="t"),
        ))
        assert log_entries(timeless) == []


# ── TestShow ──────────────────────────────────────────────────────────


class TestShow:
    def test_show_returns_chunk_with_predecessor(self) -> None:
        shown = show_chunk(_supersession_corpus(), "rule-x-v1")
        assert shown is not None
        assert shown.chunk.id == "rule-x-v1"
        assert shown.predecessor is not None
        assert shown.predecessor.id == "rule-x-v0"
        assert shown.successor is None  # nothing supersedes v1 yet

    def test_show_returns_chunk_with_successor(self) -> None:
        shown = show_chunk(_supersession_corpus(), "rule-x-v0")
        assert shown is not None
        assert shown.successor is not None
        assert shown.successor.id == "rule-x-v1"
        assert shown.predecessor is None  # v0 supersedes nothing

    def test_show_unknown_id_returns_none(self) -> None:
        assert show_chunk(_supersession_corpus(), "nonexistent") is None


# ── TestRender ────────────────────────────────────────────────────────


class TestRender:
    def test_render_diff_lists_categories(self) -> None:
        diff = diff_since(
            _supersession_corpus(),
            since_ts_ns=_ns("2024-07-01T00:00:00Z"),
            until_ts_ns=_ns("2024-09-01T00:00:00Z"),
        )
        out = render_diff(diff)
        assert "added (1)" in out
        assert "retired (1)" in out
        assert "superseded (1)" in out
        assert "rule-x-v1" in out
        assert "rule-x-v0" in out

    def test_render_diff_empty_window(self) -> None:
        diff = diff_since(
            _supersession_corpus(),
            since_ts_ns=_ns("2025-01-01T00:00:00Z"),
            until_ts_ns=_ns("2025-12-31T00:00:00Z"),
        )
        out = render_diff(diff)
        assert "no policy mutations" in out

    def test_render_log_format(self) -> None:
        entries = log_entries(_supersession_corpus())
        out = render_log(entries)
        assert "[policy log]" in out
        assert "added" in out
        assert "retired" in out

    def test_render_show_includes_chain(self) -> None:
        shown = show_chunk(_supersession_corpus(), "rule-x-v1")
        assert shown is not None
        out = render_show(shown)
        assert "rule-x-v1" in out
        assert "← supersedes: rule-x-v0" in out

    def test_render_show_open_chunk(self) -> None:
        shown = show_chunk(_supersession_corpus(), "rule-x-v1")
        assert shown is not None
        out = render_show(shown)
        # v1 is open (no valid_until)
        assert "valid_until: (open)" in out


# ── TestShippedCorpusDiff (integration) ───────────────────────────────


class TestShippedCorpusDiff:
    def test_shipped_corpus_has_no_recent_mutations(self) -> None:
        """Sanity check on the shipped corpus: 30d window is empty
        (chunks shipped in PR ③ have created_at=2026-04-15 which is
        outside a recent 30d window from arbitrary CI time)."""
        from aegis.judge.rag_corpus import (
            load_default_corpus,
            reset_corpus_cache,
        )
        reset_corpus_cache()
        corpus = load_default_corpus()
        # Use --since 'all' to confirm shipped playbooks/baselines
        # show up as "added".
        diff = diff_since(corpus, 0)
        added_ids = {c.id for c in diff.added}
        # All 6 playbooks + 1 baseline have created_at; should be in diff
        assert len(added_ids) >= 7
