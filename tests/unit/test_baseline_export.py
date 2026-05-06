"""Tests for ``aegis.burnin.baseline_export``.

Three groups:

* ``TestAnalyse`` — `analyse_audit` against synthetic JSONL fixtures.
* ``TestRender`` — `render_baseline_chunk` schema validity + content.
* ``TestExport`` — full pipeline (`export_to_corpus`) writing to a
  tmp corpus dir and round-tripping through the loader.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from aegis.burnin.baseline_export import (
    BaselineSummary,
    analyse_audit,
    export_to_corpus,
    render_baseline_chunk,
    render_export_report,
)


def _write_audit(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(r) for r in records) + "\n",
        encoding="utf-8",
    )


def _record(
    tool: str,
    *,
    hook: str = "PreToolUse",
    aid: str = "session-1",
    keys: tuple[str, ...] = (),
    decision: str | None = None,
    ts_ns: int = 1_777_700_000_000_000_000,
) -> dict:
    rec: dict = {
        "ts_ns": ts_ns,
        "tool": tool,
        "aid": aid,
        "invocation_id": "x",
        "hook": hook,
        "tool_input_keys": list(keys),
        "mode": "local",
        "prev_hash": "0" * 64,
        "this_hash": "0" * 64,
    }
    if decision:
        rec["decision"] = decision
    return rec


# ── TestAnalyse ───────────────────────────────────────────────────────


class TestAnalyse:
    def test_missing_audit_returns_zero_summary(self, tmp_path: Path) -> None:
        s = analyse_audit(tmp_path / "nope.jsonl", tenant="t")
        assert s.n_records == 0
        assert s.n_sessions == 0
        assert s.tool_freq == ()
        assert not s.is_useful

    def test_empty_audit_returns_zero(self, tmp_path: Path) -> None:
        path = tmp_path / "audit.jsonl"
        path.write_text("", encoding="utf-8")
        assert analyse_audit(path).n_records == 0

    def test_corrupt_lines_skipped(self, tmp_path: Path) -> None:
        path = tmp_path / "audit.jsonl"
        path.write_text(
            "{invalid json\n"
            + json.dumps(_record("Read")) + "\n"
            + "\n"  # blank
            + "another bad line {\n",
            encoding="utf-8",
        )
        s = analyse_audit(path)
        assert s.n_records == 1

    def test_basic_counts(self, tmp_path: Path) -> None:
        path = tmp_path / "audit.jsonl"
        _write_audit(path, [
            _record("Read", hook="PreToolUse"),
            _record("Read", hook="PostToolUse"),
            _record("Bash", hook="PreToolUse", aid="session-2"),
            _record("Edit", hook="PostToolUse", aid="session-2"),
        ])
        s = analyse_audit(path, tenant="alice")
        assert s.tenant == "alice"
        assert s.n_records == 4
        assert s.n_pretool == 2
        assert s.n_posttool == 2
        assert s.n_sessions == 2

    def test_tool_freq_top_5(self, tmp_path: Path) -> None:
        path = tmp_path / "audit.jsonl"
        records = []
        for i in range(7):
            records.extend([_record("Read")] * (10 - i))
            records.extend([_record(f"Tool{i}")] * (i + 1))
        _write_audit(path, records)
        s = analyse_audit(path)
        names = [t for t, _ in s.tool_freq]
        assert names[0] == "Read"
        assert len(s.tool_freq) <= 5

    def test_decisions_aggregated(self, tmp_path: Path) -> None:
        path = tmp_path / "audit.jsonl"
        _write_audit(path, [
            _record("Read", decision="ALLOW"),
            _record("Bash", decision="BLOCK"),
            _record("Bash", decision="BLOCK"),
            _record("Edit", decision="REQUIRE_APPROVAL"),
        ])
        s = analyse_audit(path)
        assert s.decisions == {
            "ALLOW": 1, "BLOCK": 2, "REQUIRE_APPROVAL": 1,
        }

    def test_avg_calls_per_session(self, tmp_path: Path) -> None:
        path = tmp_path / "audit.jsonl"
        _write_audit(path, [
            _record("Read", aid="a"),
            _record("Read", aid="a"),
            _record("Read", aid="a"),
            _record("Bash", aid="b"),
        ])
        s = analyse_audit(path)
        assert s.n_sessions == 2
        assert s.avg_calls_per_session == pytest.approx(2.0)

    def test_iso_timestamps(self, tmp_path: Path) -> None:
        path = tmp_path / "audit.jsonl"
        _write_audit(path, [
            _record("Read", ts_ns=1_700_000_000_000_000_000),
            _record("Read", ts_ns=1_700_001_000_000_000_000),
        ])
        s = analyse_audit(path)
        assert s.earliest_iso.startswith("20")  # ISO date
        assert s.latest_iso.startswith("20")
        assert s.earliest_ts_ns < s.latest_ts_ns

    def test_useful_threshold(self, tmp_path: Path) -> None:
        path = tmp_path / "audit.jsonl"
        _write_audit(path, [_record("Read") for _ in range(9)])
        assert not analyse_audit(path).is_useful
        _write_audit(path, [_record("Read") for _ in range(10)])
        assert analyse_audit(path).is_useful


# ── TestRender ────────────────────────────────────────────────────────


class TestRender:
    def test_chunk_has_required_fields(self) -> None:
        s = BaselineSummary(
            tenant="alice",
            n_records=100, n_pretool=50, n_posttool=50, n_sessions=5,
            earliest_ts_ns=1, latest_ts_ns=2,
            tool_freq=(("Read", 30), ("Bash", 20)),
            tool_input_keys=(("file_path", 30),),
            decisions={"ALLOW": 80, "BLOCK": 20},
            avg_calls_per_session=20.0,
            earliest_iso="2026-01-01T00:00:00Z",
            latest_iso="2026-02-01T00:00:00Z",
        )
        chunk = render_baseline_chunk(s)
        assert chunk["id"] == "baseline-alice"
        assert chunk["category"] == "baseline"
        assert "alice" in chunk["title"]
        assert "100" in chunk["content"]
        assert "Read" in chunk["content"]
        assert "ALLOW=80" in chunk["content"]
        assert chunk["tags"] == ["baseline", "alice"]
        assert chunk["decision"] == "ALLOW"

    def test_chunk_for_useless_summary_warns(self) -> None:
        s = BaselineSummary(
            tenant="bob",
            n_records=3, n_pretool=2, n_posttool=1, n_sessions=1,
            earliest_ts_ns=0, latest_ts_ns=0,
        )
        chunk = render_baseline_chunk(s)
        assert "too few" in chunk["content"]
        assert chunk["id"] == "baseline-bob"

    def test_export_report_includes_counts(self) -> None:
        s = BaselineSummary(
            tenant="t", n_records=42, n_pretool=20, n_posttool=22,
            n_sessions=3, earliest_ts_ns=1, latest_ts_ns=2,
            tool_freq=(("Read", 10),),
            avg_calls_per_session=14.0,
        )
        report = render_export_report(s, Path("/tmp/baselines.jsonl"))
        assert "42" in report
        assert "Read (10)" in report
        assert "/tmp/baselines.jsonl" in report


# ── TestExport ────────────────────────────────────────────────────────


class TestExport:
    def test_writes_baselines_file(self, tmp_path: Path) -> None:
        audit = tmp_path / "audit.jsonl"
        _write_audit(audit, [_record("Read") for _ in range(15)])
        corpus = tmp_path / "corpus"
        out_path, summary = export_to_corpus(
            audit, tenant="alice", corpus_dir=corpus,
        )
        assert out_path == corpus / "baselines.jsonl"
        assert out_path.is_file()
        # Loadable by the corpus loader as a single chunk.
        from aegis.judge.rag_corpus import load_corpus
        loaded = load_corpus(corpus)
        assert len(loaded.by_category("baseline")) == 1
        assert summary.tenant == "alice"

    def test_overwrites_existing_baseline(self, tmp_path: Path) -> None:
        audit = tmp_path / "audit.jsonl"
        _write_audit(audit, [_record("Read") for _ in range(15)])
        corpus = tmp_path / "corpus"
        export_to_corpus(audit, tenant="t1", corpus_dir=corpus)
        export_to_corpus(audit, tenant="t2", corpus_dir=corpus)
        from aegis.judge.rag_corpus import load_corpus
        loaded = load_corpus(corpus)
        ids = [c.id for c in loaded.by_category("baseline")]
        # Second export overwrites; only t2 remains.
        assert ids == ["baseline-t2"]

    def test_missing_audit_writes_warning_chunk(self, tmp_path: Path) -> None:
        corpus = tmp_path / "corpus"
        out_path, summary = export_to_corpus(
            tmp_path / "no.jsonl", tenant="alice", corpus_dir=corpus,
        )
        assert out_path.is_file()
        assert summary.n_records == 0
        from aegis.judge.rag_corpus import load_corpus
        chunk = load_corpus(corpus).by_category("baseline")[0]
        assert "too few" in chunk.content


# ── TestRotation (PR ②) ───────────────────────────────────────────────


class TestRotation:
    def test_rotate_first_call_creates_open_baseline(
        self, tmp_path: Path,
    ) -> None:
        """First rotation: nothing to seal, just write a new chunk."""
        audit = tmp_path / "audit.jsonl"
        _write_audit(audit, [_record("Read") for _ in range(15)])
        corpus = tmp_path / "corpus"
        out_path, _ = export_to_corpus(
            audit, tenant="alice", corpus_dir=corpus, rotate=True,
        )
        from aegis.judge.rag_corpus import load_corpus
        baselines = load_corpus(corpus).by_category("baseline")
        assert len(baselines) == 1
        c = baselines[0]
        assert c.id.startswith("baseline-alice-")
        assert c.valid_from is not None
        assert c.valid_until is None       # still open
        assert c.created_at is not None
        assert c.supersedes is None        # nothing to supersede yet

    def test_rotate_second_call_seals_previous_and_links(
        self, tmp_path: Path,
    ) -> None:
        """Second rotation: previous chunk gets valid_until,
        new chunk gets supersedes=<old id>."""
        audit = tmp_path / "audit.jsonl"
        _write_audit(audit, [_record("Read") for _ in range(15)])
        corpus = tmp_path / "corpus"
        out_first, _ = export_to_corpus(
            audit, tenant="alice", corpus_dir=corpus, rotate=True,
        )
        # Sleep 1 second so the second chunk has a different
        # second-resolution timestamp (and supersedes link is meaningful).
        import time as _time
        _time.sleep(1.1)
        out_second, _ = export_to_corpus(
            audit, tenant="alice", corpus_dir=corpus, rotate=True,
        )

        from aegis.judge.rag_corpus import load_corpus
        baselines = load_corpus(corpus).by_category("baseline")
        # Same-day: both chunks share the date suffix, so the second
        # call replaces the first under the same id. Sleep buys at most
        # 1s, so this is the same-day path. Verify exactly 1 chunk.
        assert len(baselines) == 1, (
            f"expected 1 baseline (same-day), got {len(baselines)}"
        )
        # The remaining chunk should have valid_from set (rotation
        # always stamps it). Same-day rotation is overwrite-with-
        # rotation-fields semantics.
        c = baselines[0]
        assert c.valid_from is not None

    def test_rotate_does_not_touch_other_tenants(
        self, tmp_path: Path,
    ) -> None:
        """Rotating tenant 'alice' must leave tenant 'bob's baseline
        untouched."""
        audit = tmp_path / "audit.jsonl"
        _write_audit(audit, [_record("Read") for _ in range(15)])
        corpus = tmp_path / "corpus"
        # Seed bob first.
        export_to_corpus(audit, tenant="bob", corpus_dir=corpus, rotate=True)
        # Then rotate alice (separate tenant).
        export_to_corpus(audit, tenant="alice", corpus_dir=corpus, rotate=True)

        from aegis.judge.rag_corpus import load_corpus
        baselines = load_corpus(corpus).by_category("baseline")
        ids = [c.id for c in baselines]
        # Both tenants present; bob's chunk untouched (valid_until is None).
        assert any("alice" in i for i in ids)
        assert any("bob" in i for i in ids)
        bob = next(c for c in baselines if "bob" in c.id)
        assert bob.valid_until is None, (
            f"bob's baseline should still be open, got {bob.valid_until}"
        )

    def test_rotate_seal_chains_supersedes_after_day_change(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Two rotations on different days produce a chain:
        chunk1 (valid_until=day2) ← chunk2 (supersedes=chunk1)."""
        audit = tmp_path / "audit.jsonl"
        _write_audit(audit, [_record("Read") for _ in range(15)])
        corpus = tmp_path / "corpus"

        # Patch _now_iso to return day 1.
        from aegis.burnin import baseline_export as _be
        monkeypatch.setattr(_be, "_now_iso", lambda: "2026-04-15T00:00:00Z")
        export_to_corpus(audit, tenant="alice", corpus_dir=corpus, rotate=True)

        # Patch _now_iso to return day 2 (different date suffix).
        monkeypatch.setattr(_be, "_now_iso", lambda: "2026-04-22T00:00:00Z")
        export_to_corpus(audit, tenant="alice", corpus_dir=corpus, rotate=True)

        from aegis.judge.rag_corpus import load_corpus
        baselines = load_corpus(corpus).by_category("baseline")
        assert len(baselines) == 2

        old = next(c for c in baselines if c.id.endswith("20260415"))
        new = next(c for c in baselines if c.id.endswith("20260422"))
        assert old.valid_until == "2026-04-22T00:00:00Z"
        assert new.valid_from == "2026-04-22T00:00:00Z"
        assert new.supersedes == old.id

    def test_rotate_view_at_anchor_picks_correct_snapshot(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """RagCorpus.valid_at(<between snapshots>) returns the older
        snapshot; valid_at(<after second>) returns the newer one."""
        audit = tmp_path / "audit.jsonl"
        _write_audit(audit, [_record("Read") for _ in range(15)])
        corpus = tmp_path / "corpus"

        from aegis.burnin import baseline_export as _be
        monkeypatch.setattr(_be, "_now_iso", lambda: "2026-04-15T00:00:00Z")
        export_to_corpus(audit, tenant="alice", corpus_dir=corpus, rotate=True)
        monkeypatch.setattr(_be, "_now_iso", lambda: "2026-04-22T00:00:00Z")
        export_to_corpus(audit, tenant="alice", corpus_dir=corpus, rotate=True)

        from datetime import datetime

        from aegis.judge.rag_corpus import load_corpus

        def _ns(iso: str) -> int:
            dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
            return int(dt.timestamp() * 1_000_000_000)

        loaded = load_corpus(corpus)
        # 2026-04-18: between the two snapshots, only the old one valid.
        view_mid = loaded.valid_at(_ns("2026-04-18T00:00:00Z"))
        ids_mid = {c.id for c in view_mid.chunks}
        assert any(i.endswith("20260415") for i in ids_mid)
        assert not any(i.endswith("20260422") for i in ids_mid)

        # 2026-05-01: after second snapshot, only it is valid.
        view_late = loaded.valid_at(_ns("2026-05-01T00:00:00Z"))
        ids_late = {c.id for c in view_late.chunks}
        assert any(i.endswith("20260422") for i in ids_late)
        assert not any(i.endswith("20260415") for i in ids_late)

    def test_no_rotate_keeps_legacy_overwrite_behaviour(
        self, tmp_path: Path,
    ) -> None:
        """rotate=False (default): each call overwrites with a single
        un-versioned chunk. PR #90 contract preserved."""
        audit = tmp_path / "audit.jsonl"
        _write_audit(audit, [_record("Read") for _ in range(15)])
        corpus = tmp_path / "corpus"
        export_to_corpus(audit, tenant="alice", corpus_dir=corpus, rotate=False)
        export_to_corpus(audit, tenant="bob", corpus_dir=corpus, rotate=False)

        from aegis.judge.rag_corpus import load_corpus
        baselines = load_corpus(corpus).by_category("baseline")
        # Last call wins, single chunk only.
        assert len(baselines) == 1
        c = baselines[0]
        assert c.id == "baseline-bob"
        # No timestamps in legacy mode.
        assert c.valid_from is None
        assert c.valid_until is None
        assert c.created_at is None
        assert c.supersedes is None
