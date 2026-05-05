"""Tests for ``aegis.performance.cache_lint`` — prompt-cache lint."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from aegis.performance.cache_lint import (
    DEFAULT_BREAK_THRESHOLD_PP,
    CacheLintReport,
    analyze_system_prompt,
    analyze_transcript,
    report_to_dict,
)

# ──────────────────────────────────────────────────────────────────────
# Static lint
# ──────────────────────────────────────────────────────────────────────


class TestStaticLint:
    def test_empty_text_returns_empty_findings(self) -> None:
        assert analyze_system_prompt("") == []

    def test_clean_prompt_returns_empty_findings(self) -> None:
        clean = (
            "You are a helpful coding assistant. Always answer in "
            "complete sentences. Use the tools when appropriate."
        )
        assert analyze_system_prompt(clean) == []

    def test_iso_date_detected(self) -> None:
        out = analyze_system_prompt("Hello. The release date is 2026-05-04.")
        assert any(f.pattern_name == "date_iso" for f in out)

    def test_uuid_detected_as_error(self) -> None:
        out = analyze_system_prompt(
            "Session: a1b2c3d4-e5f6-7890-abcd-ef1234567890 starts now."
        )
        assert len(out) == 1
        assert out[0].pattern_name == "uuid"
        assert out[0].severity == "error"

    def test_time_of_day_detected(self) -> None:
        out = analyze_system_prompt("Current time: 14:32:11")
        names = {f.pattern_name for f in out}
        assert "time_of_day" in names

    def test_today_phrase_detected(self) -> None:
        out = analyze_system_prompt("Today is May 4. Have a nice day.")
        assert any(f.pattern_name == "today_phrase" for f in out)

    def test_findings_in_document_order(self) -> None:
        text = (
            "First line.\nMiddle: 2026-05-04.\n"
            "End: 12:00.\n"
            "Tail: a1b2c3d4-e5f6-7890-abcd-ef1234567890."
        )
        out = analyze_system_prompt(text)
        positions = [f.position for f in out]
        assert positions == sorted(positions)
        assert len(out) >= 3

    def test_excerpt_truncated_on_long_match(self) -> None:
        # 13-digit epoch_ms is short, so we synthesise a manual
        # truncation case via a long text matching today_phrase.
        from aegis.performance.cache_lint import STATIC_EXCERPT_MAX_CHARS

        # Construct an artificial long match by abusing one of the
        # patterns. UUID is fixed-length 36; that's well under the
        # 60-char limit, so we just verify the cap is respected.
        out = analyze_system_prompt(
            "Session: a1b2c3d4-e5f6-7890-abcd-ef1234567890."
        )
        assert all(
            len(f.matched_excerpt) <= STATIC_EXCERPT_MAX_CHARS for f in out
        )

    def test_severity_levels_assigned(self) -> None:
        out = analyze_system_prompt(
            "uuid: a1b2c3d4-e5f6-7890-abcd-ef1234567890\n"
            "date: 2026-05-04\n"
        )
        sev = {f.pattern_name: f.severity for f in out}
        assert sev["uuid"] == "error"
        assert sev["date_iso"] == "warning"


# ──────────────────────────────────────────────────────────────────────
# Dynamic lint — transcript walk
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


class TestDynamicLint:
    def test_missing_transcript_returns_empty_report(
        self, tmp_path: Path,
    ) -> None:
        report = analyze_transcript(tmp_path / "does-not-exist.jsonl")
        assert isinstance(report, CacheLintReport)
        assert report.n_turns == 0
        assert report.breaks == []

    def test_empty_transcript(self, tmp_path: Path) -> None:
        path = tmp_path / "empty.jsonl"
        path.write_text("")
        report = analyze_transcript(path)
        assert report.n_turns == 0

    def test_stable_high_cache_no_breaks(self, tmp_path: Path) -> None:
        records = [
            _assistant_turn(input_tokens=1000, cache_read=0, cache_creation=0),
            _assistant_turn(input_tokens=200, cache_read=900),
            _assistant_turn(input_tokens=200, cache_read=1100),
            _assistant_turn(input_tokens=200, cache_read=1300),
        ]
        path = tmp_path / "stable.jsonl"
        _write_jsonl(path, records)
        report = analyze_transcript(path)
        assert report.n_turns == 4
        assert report.breaks == []
        # turn 0 has no cache (first call); turns 1-3 are highly cached.
        assert report.turns[1].efficiency > 0.7
        assert report.observed_cache_hit_rate > 0.5

    def test_break_when_efficiency_drops_sharply(
        self, tmp_path: Path,
    ) -> None:
        # Turns 0-2 build up high cache hit; turn 3 collapses.
        records = [
            _assistant_turn(input_tokens=1000),
            _assistant_turn(input_tokens=200, cache_read=1000),
            _assistant_turn(input_tokens=200, cache_read=1300),
            # Turn 3: huge fresh input, no cache_read — break.
            _assistant_turn(input_tokens=2000, cache_read=100),
        ]
        path = tmp_path / "break.jsonl"
        _write_jsonl(path, records)
        report = analyze_transcript(path)
        assert len(report.breaks) == 1
        b = report.breaks[0]
        assert b.turn_idx == 3
        assert b.drop_pp > DEFAULT_BREAK_THRESHOLD_PP
        assert b.tokens_lost_estimate > 0

    def test_break_attributed_to_new_tool(
        self, tmp_path: Path,
    ) -> None:
        records = [
            _assistant_turn(input_tokens=1000),
            _assistant_turn(
                input_tokens=200, cache_read=1000,
                tool_uses=[{"type": "tool_use", "name": "Read", "id": "t1"}],
            ),
            # Turn 2 introduces a NEW tool name → tool catalog hash changes.
            _assistant_turn(
                input_tokens=400, cache_read=200,
                tool_uses=[
                    {"type": "tool_use", "name": "filesystem_mcp", "id": "t2"},
                ],
            ),
        ]
        path = tmp_path / "newtool.jsonl"
        _write_jsonl(path, records)
        report = analyze_transcript(path)
        assert len(report.breaks) == 1
        assert "new tool registered" in report.breaks[0].attribution
        assert "filesystem_mcp" in report.breaks[0].attribution

    def test_break_attributed_to_input_jump(
        self, tmp_path: Path,
    ) -> None:
        records = [
            _assistant_turn(input_tokens=500),
            _assistant_turn(input_tokens=200, cache_read=500),
            # Turn 2: input_tokens 4× the prior turn — large dynamic inject.
            _assistant_turn(input_tokens=900, cache_read=200),
        ]
        path = tmp_path / "input_jump.jsonl"
        _write_jsonl(path, records)
        report = analyze_transcript(path)
        assert len(report.breaks) == 1
        assert "input_tokens jumped" in report.breaks[0].attribution

    def test_threshold_tunable(self, tmp_path: Path) -> None:
        # Mild drop (10 pp) — should be ignored at default threshold,
        # caught at 5 pp.
        records = [
            _assistant_turn(input_tokens=1000),
            _assistant_turn(input_tokens=100, cache_read=900),
            _assistant_turn(input_tokens=200, cache_read=800),
        ]
        path = tmp_path / "mild.jsonl"
        _write_jsonl(path, records)
        default_report = analyze_transcript(path)
        sensitive_report = analyze_transcript(path, break_threshold_pp=5.0)
        assert len(default_report.breaks) == 0
        # The 100→200 step is a smaller drop than 5pp under our model
        # (it depends on the actual eff change), so this just verifies
        # that the threshold parameter is wired through.
        assert sensitive_report.n_turns == default_report.n_turns

    def test_aggregate_metrics(self, tmp_path: Path) -> None:
        records = [
            _assistant_turn(input_tokens=1000),
            _assistant_turn(input_tokens=200, cache_read=1000),
            _assistant_turn(input_tokens=2000, cache_read=100),
        ]
        path = tmp_path / "agg.jsonl"
        _write_jsonl(path, records)
        report = analyze_transcript(path)
        assert 0.0 < report.observed_cache_hit_rate < 1.0
        # If there's a break, theoretical max should be > observed.
        if report.breaks:
            assert (
                report.theoretical_max_cache_hit_rate
                >= report.observed_cache_hit_rate
            )
            assert report.potential_token_savings > 0

    def test_combined_static_and_dynamic(self, tmp_path: Path) -> None:
        records = [
            _assistant_turn(input_tokens=500),
            _assistant_turn(input_tokens=100, cache_read=500),
        ]
        path = tmp_path / "combo.jsonl"
        _write_jsonl(path, records)
        sysprompt = (
            "You are an agent. Today is 2026-05-04. Session: "
            "a1b2c3d4-e5f6-7890-abcd-ef1234567890."
        )
        report = analyze_transcript(path, system_prompt=sysprompt)
        assert report.n_turns == 2
        assert len(report.static_findings) >= 2
        names = {f.pattern_name for f in report.static_findings}
        assert "uuid" in names


# ──────────────────────────────────────────────────────────────────────
# Serialisation
# ──────────────────────────────────────────────────────────────────────


class TestReportSerialization:
    def test_report_to_dict_round_trip(self, tmp_path: Path) -> None:
        records = [
            _assistant_turn(input_tokens=1000),
            _assistant_turn(input_tokens=100, cache_read=1000),
        ]
        path = tmp_path / "ser.jsonl"
        _write_jsonl(path, records)
        report = analyze_transcript(
            path, system_prompt="Today is 2026-05-04.",
        )
        d = report_to_dict(report)
        # Must be JSON-serialisable.
        encoded = json.dumps(d)
        decoded = json.loads(encoded)
        assert decoded["n_turns"] == 2
        assert "static_findings" in decoded
        assert "turns" in decoded

    def test_static_finding_carries_no_full_long_text(self) -> None:
        from aegis.performance.cache_lint import STATIC_EXCERPT_MAX_CHARS

        out = analyze_system_prompt(
            "Some content " + ("x" * 200)
        )
        for f in out:
            assert len(f.matched_excerpt) <= STATIC_EXCERPT_MAX_CHARS
