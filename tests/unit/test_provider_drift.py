"""Unit tests for PR-F: provider-drift detection + `aegis report --by-provider`.

Two surfaces under test:

1. ``_cmd_report_by_provider`` — groups audit records by provider,
   prints per-provider BLOCK rates, and runs the divergence advisor.
2. ``_compute_provider_drift`` — pure function, severity-multiplier
   based outlier detector across providers' BLOCK rates.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from tools import aegis_cli

# ── _compute_provider_drift — pure-function tests ─────────────────


def test_drift_silent_with_single_provider() -> None:
    """One provider can't diverge from itself — advisor stays silent."""
    by_provider = {
        "anthropic": {
            "n_total": 100, "n_block_destructive": 5, "n_block_poisoned": 0,
            "n_approval": 0, "n_safe": 95, "n_redundant": 0, "n_loop_aborted": 0,
        },
    }
    assert aegis_cli._compute_provider_drift(by_provider) == []


def test_drift_silent_with_small_sample() -> None:
    """Providers with fewer than min_calls_per_provider don't get
    compared (sample too small for meaningful divergence)."""
    by_provider = {
        "anthropic": {
            "n_total": 100, "n_block_destructive": 1, "n_block_poisoned": 0,
            "n_approval": 0, "n_safe": 99, "n_redundant": 0, "n_loop_aborted": 0,
        },
        "openai": {
            "n_total": 2,  # below default threshold of 5
            "n_block_destructive": 2, "n_block_poisoned": 0,
            "n_approval": 0, "n_safe": 0, "n_redundant": 0, "n_loop_aborted": 0,
        },
    }
    assert aegis_cli._compute_provider_drift(by_provider) == []


def test_drift_flags_3x_outlier() -> None:
    """When provider A blocks at 9% and provider B at 3%, A is 3× B
    → A is the high outlier, B is the low outlier (each flagged with
    the appropriate framing)."""
    by_provider = {
        "anthropic": {
            "n_total": 100, "n_block_destructive": 9, "n_block_poisoned": 0,
            "n_approval": 0, "n_safe": 91, "n_redundant": 0, "n_loop_aborted": 0,
        },
        "openai": {
            "n_total": 100, "n_block_destructive": 3, "n_block_poisoned": 0,
            "n_approval": 0, "n_safe": 97, "n_redundant": 0, "n_loop_aborted": 0,
        },
    }
    drift = aegis_cli._compute_provider_drift(by_provider)
    # anthropic blocks at 9% (3× the lowest peer's 3%) → high outlier
    assert any("anthropic" in line and "9.0%" in line for line in drift)
    # openai blocks at 3% (only 0.33× the highest peer) → low outlier
    assert any("openai" in line and "3.0%" in line for line in drift)


def test_drift_silent_at_2x_below_threshold() -> None:
    """Within-2× spread should NOT trigger (default threshold 3×)."""
    by_provider = {
        "anthropic": {
            "n_total": 100, "n_block_destructive": 6, "n_block_poisoned": 0,
            "n_approval": 0, "n_safe": 94, "n_redundant": 0, "n_loop_aborted": 0,
        },
        "openai": {
            "n_total": 100, "n_block_destructive": 3, "n_block_poisoned": 0,
            "n_approval": 0, "n_safe": 97, "n_redundant": 0, "n_loop_aborted": 0,
        },
    }
    assert aegis_cli._compute_provider_drift(by_provider) == []


def test_drift_flags_zero_block_outlier() -> None:
    """When peer providers block but one provider doesn't at all,
    the zero-block provider is the outlier (may be skipping safety)."""
    by_provider = {
        "anthropic": {
            "n_total": 100, "n_block_destructive": 6, "n_block_poisoned": 0,
            "n_approval": 0, "n_safe": 94, "n_redundant": 0, "n_loop_aborted": 0,
        },
        "openai": {
            "n_total": 100, "n_block_destructive": 0, "n_block_poisoned": 0,
            "n_approval": 0, "n_safe": 100, "n_redundant": 0, "n_loop_aborted": 0,
        },
    }
    drift = aegis_cli._compute_provider_drift(by_provider)
    assert any("openai BLOCKs at 0.0%" in line for line in drift)


def test_drift_silent_when_all_providers_zero_block() -> None:
    """Everyone has 0% BLOCK rate → nothing to flag, advisor stays silent."""
    by_provider = {
        "anthropic": {
            "n_total": 100, "n_block_destructive": 0, "n_block_poisoned": 0,
            "n_approval": 0, "n_safe": 100, "n_redundant": 0, "n_loop_aborted": 0,
        },
        "openai": {
            "n_total": 100, "n_block_destructive": 0, "n_block_poisoned": 0,
            "n_approval": 0, "n_safe": 100, "n_redundant": 0, "n_loop_aborted": 0,
        },
    }
    assert aegis_cli._compute_provider_drift(by_provider) == []


def test_drift_excludes_no_provider_bucket() -> None:
    """The (no-provider) bucket is missing-data, not a real provider —
    must not participate in divergence calculations."""
    by_provider = {
        "anthropic": {
            "n_total": 100, "n_block_destructive": 5, "n_block_poisoned": 0,
            "n_approval": 0, "n_safe": 95, "n_redundant": 0, "n_loop_aborted": 0,
        },
        "(no-provider)": {
            "n_total": 100, "n_block_destructive": 50, "n_block_poisoned": 0,
            "n_approval": 0, "n_safe": 50, "n_redundant": 0, "n_loop_aborted": 0,
        },
    }
    # Only one real provider → no comparison possible.
    assert aegis_cli._compute_provider_drift(by_provider) == []


def test_drift_three_providers_two_inline_one_outlier() -> None:
    """Realistic 3-provider scenario — two close to median, one
    clearly off. The outlier alone is flagged."""
    by_provider = {
        "anthropic": {
            "n_total": 100, "n_block_destructive": 5, "n_block_poisoned": 0,
            "n_approval": 0, "n_safe": 95, "n_redundant": 0, "n_loop_aborted": 0,
        },
        "openai": {
            "n_total": 100, "n_block_destructive": 4, "n_block_poisoned": 0,
            "n_approval": 0, "n_safe": 96, "n_redundant": 0, "n_loop_aborted": 0,
        },
        "google-gemini": {
            "n_total": 100, "n_block_destructive": 18, "n_block_poisoned": 0,
            "n_approval": 0, "n_safe": 82, "n_redundant": 0, "n_loop_aborted": 0,
        },
    }
    drift = aegis_cli._compute_provider_drift(by_provider)
    # google-gemini is the outlier (18% vs ~5% median)
    assert any("google-gemini" in line for line in drift)
    # anthropic + openai are close to median, NOT flagged as high outliers
    high_outliers = [line for line in drift if "× the cross-provider" in line]
    assert all("google-gemini" in line for line in high_outliers)


# ── _cmd_report_by_provider — integration ─────────────────────────


def _write_provider_audit_log(tmp_path: Path) -> Path:
    """A 16-record fixture — 8 anthropic (1 BLOCK), 8 openai (4 BLOCK)
    + a (no-provider) Claude Code-style record. Designed so the
    drift advisor fires (openai BLOCKs at 50%, anthropic at 12.5%)."""
    audit_path = tmp_path / "audit.jsonl"
    base_ns = 1_700_000_000_000_000_000
    records: list[dict] = []

    for i in range(8):
        records.append({
            "ts_ns": base_ns + i * 1_000_000_000,
            "tool": "Bash",
            "aid": f"sess-anthropic-{i}",
            "decision": "BLOCK" if i == 0 else "ALLOW",
            "reason": "rule:cloud_destructive" if i == 0 else "",
            "provider": "anthropic-claude",
        })

    for i in range(8):
        records.append({
            "ts_ns": base_ns + (8 + i) * 1_000_000_000,
            "tool": "Bash",
            "aid": f"sess-openai-{i}",
            "decision": "BLOCK" if i < 4 else "ALLOW",
            "reason": "rule:cloud_destructive" if i < 4 else "",
            "provider": "openai-gpt-4o",
        })

    # No-provider record (Claude Code track) — must NOT participate
    # in the drift calculation.
    records.append({
        "ts_ns": base_ns + 100_000_000_000,
        "tool": "Read",
        "aid": "claude-code-sess",
        "decision": "ALLOW",
        "reason": "",
    })

    audit_path.write_text(
        "\n".join(json.dumps(r) for r in records) + "\n"
    )
    return audit_path


def test_report_by_provider_groups_per_provider(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    audit_path = _write_provider_audit_log(tmp_path)
    rc = aegis_cli._cmd_report_by_provider(audit_path, since_secs=None)
    assert rc == 0

    out = capsys.readouterr().out
    assert "anthropic-claude" in out
    assert "openai-gpt-4o" in out
    assert "(no-provider)" in out


def test_report_by_provider_shows_block_rate(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Each provider header shows its BLOCK rate as a percentage."""
    audit_path = _write_provider_audit_log(tmp_path)
    aegis_cli._cmd_report_by_provider(audit_path, since_secs=None)
    out = capsys.readouterr().out
    # anthropic-claude: 1/8 = 12.5%
    assert "12.5%" in out
    # openai-gpt-4o: 4/8 = 50.0%
    assert "50.0%" in out


def test_report_by_provider_drift_advisor_fires(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """openai BLOCKs at 50%, anthropic at 12.5% — 4× divergence,
    above the 3× threshold → advisor surfaces."""
    audit_path = _write_provider_audit_log(tmp_path)
    aegis_cli._cmd_report_by_provider(audit_path, since_secs=None)
    out = capsys.readouterr().out
    assert "Provider-divergence advisor" in out
    assert "openai-gpt-4o" in out


def test_report_by_provider_severity_orders_high_block_first(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """openai (4 BLOCKs) must appear before anthropic (1 BLOCK) under
    severity ordering."""
    audit_path = _write_provider_audit_log(tmp_path)
    aegis_cli._cmd_report_by_provider(audit_path, since_secs=None)
    out = capsys.readouterr().out
    openai_idx = out.index("openai-gpt-4o")
    anthropic_idx = out.index("anthropic-claude")
    assert openai_idx < anthropic_idx


def test_report_by_provider_empty_window(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    audit_path = _write_provider_audit_log(tmp_path)
    rc = aegis_cli._cmd_report_by_provider(audit_path, since_secs=1)
    assert rc == 0
    out = capsys.readouterr().out
    assert "no records in window" in out


def test_report_by_provider_no_drift_when_only_one_provider(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """One provider means no inter-provider comparison — advisor banner
    must NOT appear (which would be a false positive)."""
    audit_path = tmp_path / "audit.jsonl"
    base_ns = 1_700_000_000_000_000_000
    records = [
        {
            "ts_ns": base_ns + i * 1_000_000_000,
            "tool": "Bash",
            "aid": f"sess-{i}",
            "decision": "ALLOW",
            "reason": "",
            "provider": "anthropic-claude",
        }
        for i in range(10)
    ]
    audit_path.write_text(
        "\n".join(json.dumps(r) for r in records) + "\n"
    )
    aegis_cli._cmd_report_by_provider(audit_path, since_secs=None)
    out = capsys.readouterr().out
    assert "Provider-divergence advisor" not in out


# ── _extract_audit_fields surfaces provider ───────────────────────


def test_extract_audit_fields_includes_provider() -> None:
    rec = {
        "ts_ns": 1, "tool": "Bash", "aid": "x",
        "decision": "ALLOW", "reason": "",
        "provider": "openai-gpt-4o",
    }
    out = aegis_cli._extract_audit_fields(rec)
    assert out["provider"] == "openai-gpt-4o"


def test_extract_audit_fields_provider_default_empty() -> None:
    rec = {
        "ts_ns": 1, "tool": "Bash", "aid": "x",
        "decision": "ALLOW", "reason": "",
    }
    out = aegis_cli._extract_audit_fields(rec)
    assert out["provider"] == ""


def test_extract_audit_fields_provider_from_sidecar_payload() -> None:
    """Sidecar audit records nest provider under payload.header."""
    rec = {
        "payload": {
            "header": {
                "decision": "BLOCK",
                "tool_name": "shell",
                "provider": "google-gemini-1.5",
                "timestamp_ns": 1,
            },
        },
    }
    out = aegis_cli._extract_audit_fields(rec)
    assert out["provider"] == "google-gemini-1.5"


# ── parser smoke ──────────────────────────────────────────────────


def test_report_by_provider_arg_parses() -> None:
    parser = aegis_cli.build_parser()
    args = parser.parse_args(["report", "--by-provider"])
    assert args.by_provider is True


def test_report_by_provider_default_off() -> None:
    parser = aegis_cli.build_parser()
    args = parser.parse_args(["report"])
    assert args.by_provider is False


def test_report_by_provider_routes_through_cmd_report(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """End-to-end: ``cmd_report`` short-circuits to the by-provider
    helper when --by-provider is set, doesn't fall through to the
    default aggregate view."""
    audit_path = _write_provider_audit_log(tmp_path)
    args = argparse.Namespace(
        audit=str(audit_path),
        since=None,
        verbose=False,
        explain=None,
        json=False,
        by_aid=False,
        by_channel=False,
        by_provider=True,
    )
    rc = aegis_cli.cmd_report(args)
    assert rc == 0
    out = capsys.readouterr().out
    assert "by provider" in out
    assert "Agent Risk Report" in out
