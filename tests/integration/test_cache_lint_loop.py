"""Tests for ``aegis.performance.cache_lint_loop`` — closed-loop
verification of the prompt-cache lint."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from aegis.performance.cache_lint import (
    CacheLintReport,
    StaticLintFinding,
    analyze_transcript,
)
from aegis.performance.cache_lint_loop import (
    ComparisonReport,
    compare_reports,
    compare_transcripts,
    comparison_to_dict,
    project_fix,
    projected_fix_to_dict,
)

# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────


def _assistant_turn(
    *,
    input_tokens: int,
    cache_read: int = 0,
    cache_creation: int = 0,
    output_tokens: int = 200,
    tool_uses: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    content: list[dict[str, Any]] = []
    if tool_uses:
        content.extend(tool_uses)
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": content,
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_read_input_tokens": cache_read,
                "cache_creation_input_tokens": cache_creation,
            },
        },
    }


def _write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")


def _bad_session() -> list[dict[str, Any]]:
    """Build a 5-turn session with one cache break at turn 3."""
    return [
        _assistant_turn(input_tokens=1000, cache_creation=1000),
        _assistant_turn(input_tokens=200, cache_read=1000),
        _assistant_turn(input_tokens=200, cache_read=1100),
        # Break: massive fresh input.
        _assistant_turn(input_tokens=2000, cache_read=200, cache_creation=1800),
        _assistant_turn(input_tokens=200, cache_read=1900),
    ]


def _good_session() -> list[dict[str, Any]]:
    """Same task, but the break has been fixed (no large fresh input
    spike at turn 3) — cache stays high throughout."""
    return [
        _assistant_turn(input_tokens=1000, cache_creation=1000),
        _assistant_turn(input_tokens=200, cache_read=1000),
        _assistant_turn(input_tokens=200, cache_read=1100),
        _assistant_turn(input_tokens=200, cache_read=1200),
        _assistant_turn(input_tokens=200, cache_read=1300),
    ]


# ──────────────────────────────────────────────────────────────────────
# project_fix — projection consistency
# ──────────────────────────────────────────────────────────────────────


class TestProjectFix:
    def test_empty_report_no_actions(self) -> None:
        rpt = CacheLintReport(n_turns=0)
        p = project_fix(rpt)
        assert p.breaks_to_address == 0
        assert p.static_findings_to_address == 0
        assert p.projected_token_savings == 0
        assert any("No actionable" in note for note in p.notes)

    def test_breaks_propagate_to_projection(
        self, tmp_path: Path,
    ) -> None:
        path = tmp_path / "bad.jsonl"
        _write_jsonl(path, _bad_session())
        rpt = analyze_transcript(path)
        p = project_fix(rpt)
        assert p.breaks_to_address == len(rpt.breaks)
        assert p.projected_token_savings == rpt.potential_token_savings
        assert (
            p.projected_cache_hit_rate
            == rpt.theoretical_max_cache_hit_rate
        )

    def test_static_findings_split_by_severity(self) -> None:
        rpt = CacheLintReport(
            n_turns=0,
            static_findings=[
                StaticLintFinding(
                    position=0, pattern_name="uuid",
                    matched_excerpt="abc-123-def-456",
                    severity="error", suggestion="fix me",
                ),
                StaticLintFinding(
                    position=20, pattern_name="date_iso",
                    matched_excerpt="2026-05-04",
                    severity="warning", suggestion="move below",
                ),
                StaticLintFinding(
                    position=40, pattern_name="epoch_ms",
                    matched_excerpt="1714834331000",
                    severity="info", suggestion="check",
                ),
            ],
        )
        p = project_fix(rpt)
        assert p.error_severity_findings == 1
        assert p.warning_severity_findings == 1
        # info-level isn't bucketed but still counted in total.
        assert p.static_findings_to_address == 3

    def test_to_dict_serialises(self) -> None:
        rpt = CacheLintReport(n_turns=0)
        d = projected_fix_to_dict(project_fix(rpt))
        json.dumps(d)  # round-trip
        assert "projected_cache_hit_rate" in d


# ──────────────────────────────────────────────────────────────────────
# compare_reports
# ──────────────────────────────────────────────────────────────────────


class TestCompare:
    def test_compare_resolves_break(
        self, tmp_path: Path,
    ) -> None:
        before_path = tmp_path / "before.jsonl"
        after_path = tmp_path / "after.jsonl"
        _write_jsonl(before_path, _bad_session())
        _write_jsonl(after_path, _good_session())

        cmp = compare_transcripts(
            before_path=before_path, after_path=after_path,
        )
        assert isinstance(cmp, ComparisonReport)
        assert len(cmp.before.breaks) >= 1
        assert len(cmp.after.breaks) == 0
        assert cmp.cache_hit_rate_delta > 0          # improved
        assert len(cmp.breaks_resolved) == len(cmp.before.breaks)
        assert cmp.token_savings_realised > 0

    def test_realisation_rate_close_to_one_when_fully_fixed(
        self, tmp_path: Path,
    ) -> None:
        before_path = tmp_path / "before.jsonl"
        after_path = tmp_path / "after.jsonl"
        _write_jsonl(before_path, _bad_session())
        _write_jsonl(after_path, _good_session())

        cmp = compare_transcripts(
            before_path=before_path, after_path=after_path,
        )
        # Realisation rate should be ≥ 0.9 — most of the projected
        # savings actually showed up.
        assert cmp.realisation_rate >= 0.9

    def test_no_change_when_same_transcript(
        self, tmp_path: Path,
    ) -> None:
        path = tmp_path / "same.jsonl"
        _write_jsonl(path, _bad_session())
        cmp = compare_transcripts(before_path=path, after_path=path)
        assert cmp.cache_hit_rate_delta == 0.0
        assert cmp.breaks_resolved == []
        assert len(cmp.breaks_persisting) == len(cmp.before.breaks)

    def test_regression_detected(self, tmp_path: Path) -> None:
        # Reverse the order — "after" is the broken one. New breaks
        # appear, persisting=0, resolved=0.
        before_path = tmp_path / "before.jsonl"
        after_path = tmp_path / "after.jsonl"
        _write_jsonl(before_path, _good_session())
        _write_jsonl(after_path, _bad_session())

        cmp = compare_transcripts(
            before_path=before_path, after_path=after_path,
        )
        assert cmp.cache_hit_rate_delta < 0
        assert len(cmp.new_breaks) >= 1
        assert cmp.token_savings_realised < 0

    def test_static_findings_diff(self) -> None:
        before = CacheLintReport(
            n_turns=0,
            static_findings=[
                StaticLintFinding(
                    position=0, pattern_name="uuid",
                    matched_excerpt="aaaa-bbbb",
                    severity="error", suggestion="x",
                ),
                StaticLintFinding(
                    position=10, pattern_name="date_iso",
                    matched_excerpt="2026-05-04",
                    severity="warning", suggestion="y",
                ),
            ],
        )
        after = CacheLintReport(
            n_turns=0,
            static_findings=[
                # uuid removed; date_iso still there with a different
                # date string (signature includes excerpt → counts as
                # different finding, so it's both resolved AND new).
                StaticLintFinding(
                    position=10, pattern_name="date_iso",
                    matched_excerpt="2026-05-05",
                    severity="warning", suggestion="y",
                ),
            ],
        )
        cmp = compare_reports(before=before, after=after)
        names_resolved = {f.pattern_name for f in cmp.static_findings_resolved}
        assert "uuid" in names_resolved
        # date_iso w/ original excerpt resolved; new excerpt added.
        assert any(
            f.pattern_name == "date_iso"
            and f.matched_excerpt == "2026-05-04"
            for f in cmp.static_findings_resolved
        )
        assert any(
            f.pattern_name == "date_iso"
            and f.matched_excerpt == "2026-05-05"
            for f in cmp.new_static_findings
        )


# ──────────────────────────────────────────────────────────────────────
# Serialisation
# ──────────────────────────────────────────────────────────────────────


class TestSerialisation:
    def test_comparison_to_dict_round_trip(
        self, tmp_path: Path,
    ) -> None:
        before_path = tmp_path / "before.jsonl"
        after_path = tmp_path / "after.jsonl"
        _write_jsonl(before_path, _bad_session())
        _write_jsonl(after_path, _good_session())

        cmp = compare_transcripts(
            before_path=before_path, after_path=after_path,
        )
        d = comparison_to_dict(cmp)
        encoded = json.dumps(d)
        decoded = json.loads(encoded)
        assert "realisation_rate" in decoded
        assert "before" in decoded
        assert "after" in decoded
        assert (
            decoded["after"]["observed_cache_hit_rate"]
            > decoded["before"]["observed_cache_hit_rate"]
        )

    def test_persisting_break_signature_match(
        self, tmp_path: Path,
    ) -> None:
        # A break that PERSISTS across before/after: same turn, same
        # attribution prefix → counted as persisting, not resolved-+-new.
        before_path = tmp_path / "before.jsonl"
        after_path = tmp_path / "after.jsonl"
        _write_jsonl(before_path, _bad_session())
        _write_jsonl(after_path, _bad_session())

        cmp = compare_transcripts(
            before_path=before_path, after_path=after_path,
        )
        assert len(cmp.breaks_resolved) == 0
        assert len(cmp.new_breaks) == 0
        assert len(cmp.breaks_persisting) == len(cmp.before.breaks)
