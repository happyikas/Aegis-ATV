"""Unit tests for `aegis report --by-aid-and-provider` (Gap A).

Cross-grouping for multi-agent + multi-LLM OpenClaw deployments.
The single-dimension --by-aid view collapses across providers; this
view keeps both dimensions visible so an operator can see "Agent A
on Claude vs Agent A on GPT" side-by-side.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from tools import aegis_cli


def _write_audit(tmp_path: Path, records: list[dict]) -> Path:
    path = tmp_path / "audit.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    return path


def _rec(
    *, aid: str, provider: str | None = None, decision: str = "ALLOW",
    reason: str = "", ts_ns: int = 1_700_000_000_000_000_000,
) -> dict:
    out: dict = {
        "ts_ns": ts_ns,
        "tool": "Bash",
        "aid": aid,
        "decision": decision,
        "reason": reason,
    }
    if provider is not None:
        out["provider"] = provider
    return out


# ── grouping: pair-wise (aid × provider) ─────────────────────────


def test_groups_records_per_aid_and_provider_pair(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Records with the same aid but different providers are kept
    in separate sub-rows (the whole point of this view)."""
    records = [
        _rec(aid="agent-a", provider="anthropic-claude", ts_ns=1),
        _rec(aid="agent-a", provider="anthropic-claude", ts_ns=2),
        _rec(aid="agent-a", provider="openai-gpt-4o", ts_ns=3),
        _rec(aid="agent-b", provider="local-llama-3.1-8b", ts_ns=4),
    ]
    audit_path = _write_audit(tmp_path, records)

    rc = aegis_cli._cmd_report_by_aid_and_provider(
        audit_path, since_secs=None,
    )
    assert rc == 0
    out = capsys.readouterr().out

    # Both aids present
    assert "agent-a" in out
    assert "agent-b" in out
    # All three providers present
    assert "anthropic-claude" in out
    assert "openai-gpt-4o" in out
    assert "local-llama-3.1-8b" in out
    # agent-a is "across 2 providers"
    assert "across 2 providers" in out
    # agent-b is "across 1 provider"
    assert "across 1 provider" in out


def test_call_counts_per_pair(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The (aid, provider) cell counts equal the input record counts
    for that pair."""
    records = (
        [_rec(aid="agent-a", provider="anthropic-claude")] * 3
        + [_rec(aid="agent-a", provider="openai-gpt-4o")] * 7
        + [_rec(aid="agent-b", provider="local-llama")] * 5
    )
    for i, r in enumerate(records):
        r["ts_ns"] = 1_700_000_000_000_000_000 + i * 1_000_000_000
    audit_path = _write_audit(tmp_path, records)

    aegis_cli._cmd_report_by_aid_and_provider(
        audit_path, since_secs=None,
    )
    out = capsys.readouterr().out
    # agent-a total = 10 across 2 providers
    assert "agent-a" in out and "10 calls across 2" in out
    # agent-b total = 5 across 1 provider
    assert "agent-b" in out and "5 calls across 1" in out


def test_buckets_no_aid_and_no_provider(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Records with empty aid go to '(no-aid)'; empty provider
    goes to '(no-provider)'. Both can co-exist."""
    records = [
        _rec(aid="", provider="anthropic-claude"),  # no-aid
        _rec(aid="agent-a", provider=None),  # no-provider
        _rec(aid="", provider=None),  # both empty
    ]
    audit_path = _write_audit(tmp_path, records)
    aegis_cli._cmd_report_by_aid_and_provider(
        audit_path, since_secs=None,
    )
    out = capsys.readouterr().out
    assert "(no-aid)" in out
    assert "(no-provider)" in out


def test_severity_orders_high_block_aid_first(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The aid with the highest aggregate severity (across all its
    providers) appears first."""
    records = (
        # agent-a: 10 ALLOWs across two providers — low severity
        [_rec(aid="agent-a", provider="claude")] * 5
        + [_rec(aid="agent-a", provider="gpt")] * 5
        # agent-b: 3 BLOCKs on a single provider — much higher severity
        + [
            _rec(
                aid="agent-b", provider="local-llama",
                decision="BLOCK", reason="rule:cloud_destructive",
            )
        ] * 3
    )
    for i, r in enumerate(records):
        r["ts_ns"] = 1_700_000_000_000_000_000 + i * 1_000_000_000
    audit_path = _write_audit(tmp_path, records)
    aegis_cli._cmd_report_by_aid_and_provider(
        audit_path, since_secs=None,
    )
    out = capsys.readouterr().out
    b_idx = out.index("agent-b")
    a_idx = out.index("agent-a")
    # agent-b (3 BLOCKs = severity 300) before agent-a (10 ALLOWs = severity 10)
    assert b_idx < a_idx


def test_empty_window_returns_clean(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    audit_path = _write_audit(tmp_path, [
        _rec(aid="agent-a", provider="claude"),
    ])
    rc = aegis_cli._cmd_report_by_aid_and_provider(
        audit_path, since_secs=1,
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "no records in window" in out


# ── per-aid drift advisor ────────────────────────────────────────


def test_per_agent_drift_banner_fires_on_3x_divergence(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """When a single agent uses 2+ providers and BLOCK rates diverge
    by >=3x, the advisor banner surfaces."""
    records = (
        # agent-a on claude: 5 BLOCKs out of 10 = 50% BLOCK
        [
            _rec(
                aid="agent-a", provider="claude",
                decision="BLOCK", reason="rule:x",
            )
        ] * 5
        + [_rec(aid="agent-a", provider="claude")] * 5
        # agent-a on gpt: 1 BLOCK out of 10 = 10% BLOCK (5x divergence)
        + [
            _rec(
                aid="agent-a", provider="gpt",
                decision="BLOCK", reason="rule:x",
            )
        ]
        + [_rec(aid="agent-a", provider="gpt")] * 9
    )
    for i, r in enumerate(records):
        r["ts_ns"] = 1_700_000_000_000_000_000 + i * 1_000_000_000
    audit_path = _write_audit(tmp_path, records)
    aegis_cli._cmd_report_by_aid_and_provider(
        audit_path, since_secs=None,
    )
    out = capsys.readouterr().out
    assert "Per-agent provider-divergence advisor" in out
    # The advisor line names the agent
    assert "agent-a" in out


def test_per_agent_drift_silent_on_close_rates(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Within-2x spread should NOT trigger (default threshold 3x)."""
    records = (
        # agent-a on claude: 2 BLOCKs out of 10 = 20%
        [
            _rec(
                aid="agent-a", provider="claude",
                decision="BLOCK", reason="rule:x",
            )
        ] * 2
        + [_rec(aid="agent-a", provider="claude")] * 8
        # agent-a on gpt: 1 BLOCK out of 10 = 10% (2x — not enough)
        + [
            _rec(
                aid="agent-a", provider="gpt",
                decision="BLOCK", reason="rule:x",
            )
        ]
        + [_rec(aid="agent-a", provider="gpt")] * 9
    )
    for i, r in enumerate(records):
        r["ts_ns"] = 1_700_000_000_000_000_000 + i * 1_000_000_000
    audit_path = _write_audit(tmp_path, records)
    aegis_cli._cmd_report_by_aid_and_provider(
        audit_path, since_secs=None,
    )
    out = capsys.readouterr().out
    assert "Per-agent provider-divergence advisor" not in out


def test_per_agent_drift_zero_block_outlier(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """When peers BLOCK but one provider doesn't, the zero-block one
    is the suspicious side (peers enforce safety, this one doesn't)."""
    records = (
        # agent-a on claude: 3 BLOCKs out of 10
        [
            _rec(
                aid="agent-a", provider="claude",
                decision="BLOCK", reason="rule:x",
            )
        ] * 3
        + [_rec(aid="agent-a", provider="claude")] * 7
        # agent-a on gpt: 0 BLOCKs out of 10
        + [_rec(aid="agent-a", provider="gpt")] * 10
    )
    for i, r in enumerate(records):
        r["ts_ns"] = 1_700_000_000_000_000_000 + i * 1_000_000_000
    audit_path = _write_audit(tmp_path, records)
    aegis_cli._cmd_report_by_aid_and_provider(
        audit_path, since_secs=None,
    )
    out = capsys.readouterr().out
    assert "Per-agent provider-divergence advisor" in out
    assert "gpt" in out and "0.0%" in out


def test_drift_excludes_no_provider_bucket(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Drift advisor only compares real providers; the (no-provider)
    bucket (Claude Code-style records) doesn't participate."""
    records = (
        # agent-a (no-provider): all ALLOWs
        [_rec(aid="agent-a", provider=None)] * 10
        # agent-a on claude: high BLOCK rate
        + [
            _rec(
                aid="agent-a", provider="claude",
                decision="BLOCK", reason="rule:x",
            )
        ] * 6
        + [_rec(aid="agent-a", provider="claude")] * 4
    )
    for i, r in enumerate(records):
        r["ts_ns"] = 1_700_000_000_000_000_000 + i * 1_000_000_000
    audit_path = _write_audit(tmp_path, records)
    aegis_cli._cmd_report_by_aid_and_provider(
        audit_path, since_secs=None,
    )
    out = capsys.readouterr().out
    # Only one real provider for agent-a → no advisor banner.
    assert "Per-agent provider-divergence advisor" not in out


def test_drift_silent_when_aid_uses_one_provider(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Single-provider agents can't drift — advisor stays silent."""
    records = [_rec(aid="agent-a", provider="claude")] * 10
    for i, r in enumerate(records):
        r["ts_ns"] = 1_700_000_000_000_000_000 + i * 1_000_000_000
    audit_path = _write_audit(tmp_path, records)
    aegis_cli._cmd_report_by_aid_and_provider(
        audit_path, since_secs=None,
    )
    out = capsys.readouterr().out
    assert "Per-agent provider-divergence advisor" not in out


# ── routing: cmd_report dispatches to this view correctly ───────


def test_cmd_report_routes_via_explicit_flag(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    audit_path = _write_audit(tmp_path, [
        _rec(aid="agent-a", provider="claude"),
    ])
    args = argparse.Namespace(
        audit=str(audit_path),
        since=None,
        verbose=False,
        explain=None,
        json=False,
        by_aid=False,
        by_channel=False,
        by_provider=False,
        by_aid_and_provider=True,  # ← explicit
    )
    rc = aegis_cli.cmd_report(args)
    assert rc == 0
    out = capsys.readouterr().out
    assert "by aid × provider" in out


def test_cmd_report_routes_via_combined_flags(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Passing BOTH --by-aid and --by-provider implicitly routes to
    the cross-grouping view (more discoverable than the explicit
    --by-aid-and-provider flag)."""
    audit_path = _write_audit(tmp_path, [
        _rec(aid="agent-a", provider="claude"),
    ])
    args = argparse.Namespace(
        audit=str(audit_path),
        since=None,
        verbose=False,
        explain=None,
        json=False,
        by_aid=True,         # ← combined
        by_channel=False,
        by_provider=True,    # ← combined
        by_aid_and_provider=False,
    )
    rc = aegis_cli.cmd_report(args)
    assert rc == 0
    out = capsys.readouterr().out
    assert "by aid × provider" in out


def test_cmd_report_by_aid_alone_routes_to_by_aid_view(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Single --by-aid flag still routes to the original by-aid view
    (regression check — this should NOT accidentally hit the new
    cross-grouping path)."""
    audit_path = _write_audit(tmp_path, [
        _rec(aid="agent-a", provider="claude"),
    ])
    args = argparse.Namespace(
        audit=str(audit_path),
        since=None,
        verbose=False,
        explain=None,
        json=False,
        by_aid=True,
        by_channel=False,
        by_provider=False,
        by_aid_and_provider=False,
    )
    rc = aegis_cli.cmd_report(args)
    assert rc == 0
    out = capsys.readouterr().out
    # The plain by-aid view does NOT have "× provider" in its title.
    assert "× provider" not in out
    assert "by aid" in out


# ── parser smoke ────────────────────────────────────────────────


def test_arg_parses() -> None:
    parser = aegis_cli.build_parser()
    args = parser.parse_args(["report", "--by-aid-and-provider"])
    assert args.by_aid_and_provider is True


def test_default_off() -> None:
    parser = aegis_cli.build_parser()
    args = parser.parse_args(["report"])
    assert args.by_aid_and_provider is False
