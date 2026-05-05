"""Tests for the 4th security-hardening PR — two items bundled:

1. **Dashboard `--redact`** — `aegis status --performance --redact`
   produces a PerformanceSummary safe to share in support tickets:
   absolute dollars zeroed, timestamps day-quantized, audit path
   replaced with a SHA3 prefix. Counts and ratios (which are what
   actually diagnose inefficiency) are kept verbatim.

2. **Method-aware retry threshold** — `detect_user_retry` now
   picks the threshold AFTER the similarity method is known:
   0.85 for BGE-cosine, 0.5 for Jaccard. Eliminates the
   over-trigger / under-trigger asymmetry from the previous
   single-threshold design.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from aegis.cost.user_retry_detector import (
    DEFAULT_BGE_THRESHOLD,
    DEFAULT_JACCARD_THRESHOLD,
    DEFAULT_RETRY_THRESHOLD,
    detect_user_retry,
)
from aegis.performance.dashboard import (
    PerformanceSummary,
    build_performance_summary,
    redact_summary,
    summary_to_dict,
)

# ──────────────────────────────────────────────────────────────────────
# Item #6 — Dashboard --redact
# ──────────────────────────────────────────────────────────────────────


def _stop_record(
    *, aid: str, ts_ns: int, dollars: float = 0.50, hit_rate: float = 0.70,
) -> dict[str, Any]:
    return {
        "ts_ns": ts_ns,
        "aid": aid,
        "tool": "(stop)",
        "hook": "Stop",
        "explain": {
            "session_retrospective": {
                "aid": aid,
                "cumulative_billed_dollars": dollars,
                "cache_hit_rate": hit_rate,
                "input_tokens_total": 1000.0,
                "output_tokens_total": 500.0,
                "cache_read_tokens_total": 2000.0,
                "cache_creation_tokens_total": 100.0,
                "n_tool_success": 8,
                "n_tool_failure": 0,
                "n_backtracks": 0,
                "n_redundant": 0,
                "n_is_error": 0,
            },
        },
    }


def _write_audit(tmp_path: Path) -> Path:
    audit = tmp_path / "audit.jsonl"
    with audit.open("w", encoding="utf-8") as fh:
        # Two sessions on different days
        fh.write(json.dumps(
            _stop_record(aid="sess-1", ts_ns=1_730_000_000_000_000_000),
        ) + "\n")
        fh.write(json.dumps(
            _stop_record(aid="sess-2", ts_ns=1_730_086_400_000_000_000),
        ) + "\n")
    return audit


class TestDashboardRedact:
    def test_dollars_zeroed(self, tmp_path: Path) -> None:
        audit = _write_audit(tmp_path)
        summary = build_performance_summary(audit)
        assert summary.cumulative_billed_dollars > 0   # baseline
        redacted = redact_summary(summary)
        assert redacted.cumulative_billed_dollars == 0.0
        assert redacted.avg_session_billed_dollars == 0.0

    def test_token_counts_kept(self, tmp_path: Path) -> None:
        # Token totals don't on their own reveal spend — redaction
        # KEEPS them so the dashboard remains useful for inefficiency
        # diagnosis.
        audit = _write_audit(tmp_path)
        summary = build_performance_summary(audit)
        redacted = redact_summary(summary)
        assert redacted.total_input_tokens == summary.total_input_tokens
        assert redacted.total_cache_read_tokens == summary.total_cache_read_tokens
        assert redacted.weighted_cache_hit_rate == summary.weighted_cache_hit_rate

    def test_audit_path_hashed(self, tmp_path: Path) -> None:
        audit = _write_audit(tmp_path)
        summary = build_performance_summary(audit)
        redacted = redact_summary(summary)
        # Original path on disk → not in redacted form.
        assert str(audit) not in redacted.audit_path
        assert redacted.audit_path.startswith("sha3:")

    def test_timestamps_quantized_to_day(self, tmp_path: Path) -> None:
        audit = _write_audit(tmp_path)
        summary = build_performance_summary(audit)
        redacted = redact_summary(summary)
        day_ns = 86_400 * 1_000_000_000
        # Both quantised to start-of-day.
        assert redacted.earliest_session_ts_ns % day_ns == 0
        assert redacted.latest_session_ts_ns % day_ns == 0

    def test_redaction_idempotent(self, tmp_path: Path) -> None:
        audit = _write_audit(tmp_path)
        summary = build_performance_summary(audit)
        once = redact_summary(summary)
        twice = redact_summary(once)
        assert summary_to_dict(once) == summary_to_dict(twice)

    def test_empty_audit_redact_safe(self, tmp_path: Path) -> None:
        # Redacting an empty summary mustn't crash.
        empty = PerformanceSummary(audit_path="")
        red = redact_summary(empty)
        assert red.cumulative_billed_dollars == 0.0
        assert red.audit_path == ""

    def test_n_sessions_kept(self, tmp_path: Path) -> None:
        audit = _write_audit(tmp_path)
        summary = build_performance_summary(audit)
        red = redact_summary(summary)
        assert red.n_sessions == summary.n_sessions   # 2


# ──────────────────────────────────────────────────────────────────────
# Item #7 — Method-aware retry threshold
# ──────────────────────────────────────────────────────────────────────


def _write_transcript(tmp_path: Path, prompts: list[str]) -> Path:
    p = tmp_path / "transcript.jsonl"
    with p.open("w", encoding="utf-8") as fh:
        for prompt in prompts:
            fh.write(json.dumps({
                "type": "user",
                "message": {"role": "user", "content": prompt},
            }) + "\n")
    return p


class TestMethodAwareThreshold:
    def test_constants_exposed(self) -> None:
        # Public API: explicit method-specific constants + a
        # backwards-compatible alias.
        assert DEFAULT_JACCARD_THRESHOLD == 0.5
        assert DEFAULT_BGE_THRESHOLD == 0.85
        # Backwards-compat alias points at Jaccard default.
        assert DEFAULT_RETRY_THRESHOLD == DEFAULT_JACCARD_THRESHOLD

    def test_auto_picks_jaccard_threshold_when_no_bge(
        self, tmp_path: Path,
    ) -> None:
        # No BGE provider → Jaccard. threshold=None should auto-pick 0.5.
        path = _write_transcript(tmp_path, [
            "fix the bug in login.py",
            "fix the bug in login.py please",
        ])
        ev = detect_user_retry(
            current_prompt="fix the bug in login.py please",
            transcript_path=path,
            threshold=None,
        )
        assert ev.method == "jaccard"
        assert ev.threshold == DEFAULT_JACCARD_THRESHOLD

    def test_explicit_threshold_honoured(self, tmp_path: Path) -> None:
        path = _write_transcript(tmp_path, ["a", "b"])
        ev = detect_user_retry(
            current_prompt="b",
            transcript_path=path,
            threshold=0.99,
        )
        # The explicit value wins regardless of method.
        assert ev.threshold == 0.99

    def test_threshold_default_is_jaccard_when_unset(
        self, tmp_path: Path,
    ) -> None:
        # Calling without threshold (using default) must NOT pin to 0.5
        # — it must auto-pick. Under no BGE config, that means Jaccard
        # 0.5.
        path = _write_transcript(tmp_path, [
            "audit my code please",
            "audit my code please now",
        ])
        ev = detect_user_retry(
            current_prompt="audit my code please now",
            transcript_path=path,
        )
        # Should be Jaccard (no BGE), threshold = jaccard default.
        assert ev.method == "jaccard"
        assert ev.threshold == DEFAULT_JACCARD_THRESHOLD

    def test_jaccard_retry_detected_with_overlap(
        self, tmp_path: Path,
    ) -> None:
        # Word-overlap above 0.5 → flagged as retry.
        path = _write_transcript(tmp_path, [
            "fix the bug in login.py and tests",
            "fix the bug in login.py please",
        ])
        ev = detect_user_retry(
            current_prompt="fix the bug in login.py please",
            transcript_path=path,
        )
        assert ev.is_retry is True

    def test_jaccard_no_retry_when_unrelated(
        self, tmp_path: Path,
    ) -> None:
        path = _write_transcript(tmp_path, [
            "fix the bug in login.py",
            "explain quantum entanglement to me",
        ])
        ev = detect_user_retry(
            current_prompt="explain quantum entanglement to me",
            transcript_path=path,
        )
        assert ev.is_retry is False
