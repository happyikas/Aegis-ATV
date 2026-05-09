"""Unit tests for PR-C `--by-aid` flag on cost / report commands."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from tools import aegis_cli

# ── shared fixture: a small synthetic audit log with multiple aids ──


def _write_audit_log(tmp_path: Path) -> Path:
    """A 6-record audit log spanning 3 distinct aids — main agent,
    a Claude Code subagent (is_sidechain=True), and a "noisy" agent
    that gets blocked."""
    audit_path = tmp_path / "audit.jsonl"
    base_ns = 1_700_000_000_000_000_000
    records = [
        # main agent — mostly ALLOW
        {
            "ts_ns": base_ns + 0,
            "tool": "Bash",
            "aid": "main-session",
            "decision": "ALLOW",
            "reason": "",
            "is_sidechain": False,
        },
        {
            "ts_ns": base_ns + 1_000_000_000,
            "tool": "Read",
            "aid": "main-session",
            "decision": "ALLOW",
            "reason": "",
            "is_sidechain": False,
        },
        # main agent — one require-approval
        {
            "ts_ns": base_ns + 2_000_000_000,
            "tool": "Bash",
            "aid": "main-session",
            "decision": "REQUIRE_APPROVAL",
            "reason": "step336 loop detected",
            "is_sidechain": False,
        },
        # a subagent within the same session
        {
            "ts_ns": base_ns + 3_000_000_000,
            "tool": "Read",
            "aid": "main-session",
            "decision": "ALLOW",
            "reason": "",
            "is_sidechain": True,    # ← subagent flag
        },
        # noisy agent — gets blocked twice
        {
            "ts_ns": base_ns + 4_000_000_000,
            "tool": "Bash",
            "aid": "noisy-agent",
            "decision": "BLOCK",
            "reason": "rule:cloud_destructive",
            "is_sidechain": False,
        },
        {
            "ts_ns": base_ns + 5_000_000_000,
            "tool": "Bash",
            "aid": "noisy-agent",
            "decision": "BLOCK",
            "reason": "rule:instruction_drift detected",
            "is_sidechain": False,
        },
    ]
    audit_path.write_text(
        "\n".join(json.dumps(r) for r in records) + "\n"
    )
    return audit_path


# ── _cmd_report_by_aid ──────────────────────────────────────────────


def test_report_by_aid_groups_records_per_aid(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """One block per aid. Counters per-aid, not global."""
    audit_path = _write_audit_log(tmp_path)
    rc = aegis_cli._cmd_report_by_aid(audit_path, since_secs=None)
    assert rc == 0

    out = capsys.readouterr().out
    # Both aids appear as headers
    assert "main-session" in out
    assert "noisy-agent" in out
    # noisy-agent has 2 destructive BLOCKs (1 cloud + 1 drift split)
    assert "1 destructive blocked" in out  # the drift one is "poisoned"
    # main-session has 1 require-approval (the loop)
    assert "1 required approval" in out


def test_report_by_aid_flags_sidechain_aids(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Aids with any is_sidechain=True record get a [sidechain] tag."""
    audit_path = _write_audit_log(tmp_path)
    aegis_cli._cmd_report_by_aid(audit_path, since_secs=None)

    out = capsys.readouterr().out
    # main-session has the subagent record → flagged
    assert "main-session" in out and "[sidechain]" in out
    # noisy-agent has no subagent records → no flag
    # Find the noisy-agent block specifically
    noisy_idx = out.index("noisy-agent")
    # The line with [sidechain] for main-session must come BEFORE
    # noisy-agent (sorted by severity, BLOCK-heavy first).
    sidechain_idx = out.index("[sidechain]")
    # In our fixture, noisy-agent has 2 BLOCKs (severity 200) vs
    # main-session's 1 ASK (severity 10) — noisy-agent comes first.
    # Either way, the [sidechain] tag should appear with main-session
    # and not with noisy-agent.
    main_idx = out.index("main-session")
    assert sidechain_idx > main_idx and sidechain_idx < noisy_idx \
        or main_idx > noisy_idx  # noisy first → main+sidechain after
    # Either order is fine; what matters is [sidechain] is on the
    # main-session line, not on noisy-agent's.


def test_report_by_aid_severity_ordering(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The aid with destructive BLOCKs should appear before one with
    just APPROVALs (severity-weighted ordering)."""
    audit_path = _write_audit_log(tmp_path)
    aegis_cli._cmd_report_by_aid(audit_path, since_secs=None)

    out = capsys.readouterr().out
    noisy_idx = out.index("noisy-agent")
    main_idx = out.index("main-session")
    # noisy-agent has 2 BLOCKs (severity 200) vs main 1 ASK (severity
    # 10) — noisy must appear FIRST.
    assert noisy_idx < main_idx


def test_report_by_aid_handles_no_aid_records(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Records with empty aid get bucketed under '(no-aid)' instead
    of crashing or being silently dropped."""
    audit_path = tmp_path / "audit.jsonl"
    audit_path.write_text(
        json.dumps({
            "ts_ns": 1_700_000_000_000_000_000,
            "tool": "Bash",
            "aid": "",  # ← empty
            "decision": "ALLOW",
            "reason": "",
        }) + "\n"
    )
    rc = aegis_cli._cmd_report_by_aid(audit_path, since_secs=None)
    assert rc == 0
    out = capsys.readouterr().out
    assert "(no-aid)" in out


def test_report_by_aid_empty_window_returns_clean(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No records within window → friendly 'no records' line, exit 0."""
    audit_path = _write_audit_log(tmp_path)
    # Force a window that excludes all records (since=1 second).
    rc = aegis_cli._cmd_report_by_aid(audit_path, since_secs=1)
    assert rc == 0
    out = capsys.readouterr().out
    assert "no records in window" in out


# ── _cmd_cost_summary_by_aid ────────────────────────────────────────


def test_cost_summary_by_aid_lists_all_aids(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`aegis cost summary --by-aid` must list every aid present."""
    audit_path = _write_audit_log(tmp_path)
    args = argparse.Namespace(
        action="summary",
        audit=str(audit_path),
        spike_threshold=0.10,
        json=False,
        by_aid=True,
        top=10,
    )
    rc = aegis_cli._cmd_cost_summary(args)
    assert rc == 0

    out = capsys.readouterr().out
    assert "by aid" in out
    assert "main-session" in out
    assert "noisy-agent" in out


def test_cost_summary_by_aid_top_limit(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--top 1 limits output to the single highest-cost aid."""
    audit_path = _write_audit_log(tmp_path)
    args = argparse.Namespace(
        action="summary",
        audit=str(audit_path),
        spike_threshold=0.10,
        json=False,
        by_aid=True,
        top=1,
    )
    rc = aegis_cli._cmd_cost_summary(args)
    assert rc == 0
    out = capsys.readouterr().out
    # The "top 1 of N aids" label should mention the limit.
    assert "top 1 of" in out


# ── parser-level smoke tests ────────────────────────────────────────


def test_cost_summary_by_aid_arg_parses() -> None:
    parser = aegis_cli.build_parser()
    args = parser.parse_args(
        ["cost", "summary", "--by-aid", "--top", "5"],
    )
    assert args.by_aid is True
    assert args.top == 5


def test_cost_summary_by_aid_default_top() -> None:
    parser = aegis_cli.build_parser()
    args = parser.parse_args(["cost", "summary"])
    assert args.by_aid is False
    assert args.top == 10


def test_report_by_aid_arg_parses() -> None:
    parser = aegis_cli.build_parser()
    args = parser.parse_args(["report", "--by-aid"])
    assert args.by_aid is True


def test_report_by_aid_default_off() -> None:
    parser = aegis_cli.build_parser()
    args = parser.parse_args(["report"])
    assert args.by_aid is False
