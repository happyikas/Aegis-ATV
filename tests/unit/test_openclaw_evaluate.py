"""Unit tests for PR-D: OpenClaw multi-channel attribution.

Two surfaces under test:

1. ``OpenClawEvaluateRequest`` + ``_build_atv_from_openclaw`` —
   adapter that converts the plugin's flat request shape into the
   sidecar's internal ATVInput model.
2. ``aegis report --by-channel`` CLI view — groups audit records by
   the ``channel`` field stamped at evaluation time.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aegis.api.evaluate import (
    OpenClawEvaluateRequest,
    _build_atv_from_openclaw,
)
from aegis.schema import ATVHeader, ATVInput
from tools import aegis_cli

# ── _build_atv_from_openclaw — adapter shape correctness ────────────


def test_adapter_maps_basic_fields() -> None:
    req = OpenClawEvaluateRequest(
        tool_name="shell",
        tool_input={"command": "ls -la"},
        tenant_id="acme",
        session_id="sess-1",
        invocation_id="inv-1",
    )
    inp = _build_atv_from_openclaw(req)

    assert isinstance(inp, ATVInput)
    assert isinstance(inp.header, ATVHeader)
    assert inp.tool_name == "shell"
    # tool_args_json is canonical JSON (sort_keys=True)
    assert json.loads(inp.tool_args_json) == {"command": "ls -la"}
    assert inp.header.tenant_id == "acme"
    assert inp.header.aid == "sess-1"
    # invocation_id flows into trace_id so audit / forensic chain matches
    assert inp.header.trace_id == "inv-1"


def test_adapter_carries_channel_and_provider() -> None:
    """The two PR-D fields must round-trip into ATVHeader so the
    audit record's ``inp.header.model_dump()`` (in step360) picks
    them up automatically — no extra wiring required at signing
    time."""
    req = OpenClawEvaluateRequest(
        tool_name="fs.write",
        tool_input={"path": "/tmp/x"},
        tenant_id="acme",
        channel="telegram",
        provider="anthropic-claude-3-5-sonnet",
    )
    inp = _build_atv_from_openclaw(req)
    assert inp.header.channel == "telegram"
    assert inp.header.provider == "anthropic-claude-3-5-sonnet"


def test_adapter_omits_channel_when_absent() -> None:
    """Claude Code track has no channel; the adapter must leave the
    field as None so audit records stay clean."""
    req = OpenClawEvaluateRequest(
        tool_name="shell",
        tool_input={"command": "ls"},
        tenant_id="claude-code",
    )
    inp = _build_atv_from_openclaw(req)
    assert inp.header.channel is None
    assert inp.header.provider is None


def test_adapter_synthesizes_aid_when_missing() -> None:
    """OpenClaw plugins that don't track session_id / invocation_id
    still get a usable aid so per-aid audit grouping works."""
    req = OpenClawEvaluateRequest(
        tool_name="shell",
        tool_input={"command": "echo hi"},
    )
    inp = _build_atv_from_openclaw(req)
    # Falls back to a stable string instead of crashing or empty.
    assert inp.header.aid == "openclaw-default"
    # Trace ID is randomised hex (a uuid hex is 32 chars).
    assert len(inp.header.trace_id) == 32


def test_adapter_preserves_user_prompt_as_plan_text() -> None:
    req = OpenClawEvaluateRequest(
        tool_name="shell",
        tool_input={"command": "ls"},
        user_prompt="Find me the most-recently-modified .ts file",
    )
    inp = _build_atv_from_openclaw(req)
    assert "most-recently-modified" in inp.plan_text


def test_adapter_truncates_long_user_prompt() -> None:
    """plan_text caps at 500 chars to keep the audit/embed surfaces bounded."""
    long_prompt = "A" * 2000
    req = OpenClawEvaluateRequest(
        tool_name="shell",
        tool_input={},
        user_prompt=long_prompt,
    )
    inp = _build_atv_from_openclaw(req)
    assert len(inp.plan_text) == 500


# ── ATVHeader schema accepts new fields ─────────────────────────────


def test_atvheader_channel_provider_default_none() -> None:
    """Round-trip via Pydantic: channel + provider default to None
    and remain so unless explicitly set."""
    h = ATVHeader(
        trace_id="t", span_id="s", tenant_id="x", aid="y", timestamp_ns=0,
    )
    assert h.channel is None
    assert h.provider is None


def test_atvheader_channel_provider_round_trip() -> None:
    h = ATVHeader(
        trace_id="t", span_id="s", tenant_id="x", aid="y", timestamp_ns=0,
        channel="discord", provider="openai-gpt-4o",
    )
    out = h.model_dump()
    assert out["channel"] == "discord"
    assert out["provider"] == "openai-gpt-4o"


# ── _cmd_report_by_channel ──────────────────────────────────────────


def _write_channel_audit_log(tmp_path: Path) -> Path:
    """A 5-record fixture spanning 3 channels — telegram (BLOCK-heavy),
    discord (clean), and (no-channel) (Claude Code track with no
    channel field)."""
    audit_path = tmp_path / "audit.jsonl"
    base_ns = 1_700_000_000_000_000_000
    records = [
        # telegram — gets BLOCKed twice
        {
            "ts_ns": base_ns + 0,
            "tool": "Bash",
            "aid": "tg-user-42",
            "decision": "BLOCK",
            "reason": "rule:cloud_destructive",
            "channel": "telegram",
            "provider": "anthropic-claude",
        },
        {
            "ts_ns": base_ns + 1_000_000_000,
            "tool": "Bash",
            "aid": "tg-user-42",
            "decision": "BLOCK",
            "reason": "step336 loop detected",
            "channel": "telegram",
            "provider": "anthropic-claude",
        },
        # discord — clean ALLOWs only
        {
            "ts_ns": base_ns + 2_000_000_000,
            "tool": "Read",
            "aid": "dc-channel-7",
            "decision": "ALLOW",
            "reason": "",
            "channel": "discord",
            "provider": "openai-gpt-4o",
        },
        {
            "ts_ns": base_ns + 3_000_000_000,
            "tool": "Edit",
            "aid": "dc-channel-7",
            "decision": "ALLOW",
            "reason": "",
            "channel": "discord",
            "provider": "openai-gpt-4o",
        },
        # No channel field (e.g., Claude Code track record)
        {
            "ts_ns": base_ns + 4_000_000_000,
            "tool": "Bash",
            "aid": "claude-code-sess",
            "decision": "ALLOW",
            "reason": "",
        },
    ]
    audit_path.write_text(
        "\n".join(json.dumps(r) for r in records) + "\n"
    )
    return audit_path


def test_report_by_channel_groups_records_per_channel(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    audit_path = _write_channel_audit_log(tmp_path)
    rc = aegis_cli._cmd_report_by_channel(audit_path, since_secs=None)
    assert rc == 0

    out = capsys.readouterr().out
    assert "telegram" in out
    assert "discord" in out
    # Records without channel bucket under "(no-channel)"
    assert "(no-channel)" in out


def test_report_by_channel_severity_orders_telegram_first(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """telegram has 2 BLOCKs (severity 200) → must appear before
    discord (2 ALLOWs, severity 2)."""
    audit_path = _write_channel_audit_log(tmp_path)
    aegis_cli._cmd_report_by_channel(audit_path, since_secs=None)

    out = capsys.readouterr().out
    tg_idx = out.index("telegram")
    dc_idx = out.index("discord")
    assert tg_idx < dc_idx


def test_report_by_channel_counts_match_per_channel(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    audit_path = _write_channel_audit_log(tmp_path)
    aegis_cli._cmd_report_by_channel(audit_path, since_secs=None)

    out = capsys.readouterr().out
    # telegram: 2 BLOCKs (1 destructive + 1 loop)
    # The loop one is REQUIRE_APPROVAL with "loop" in reason actually...
    # Let me re-check the fixture: both are BLOCK, with one having
    # "step336 loop" in reason. The current logic only looks for "loop"
    # in REQUIRE_APPROVAL paths. So both BLOCKs count as destructive.
    assert "2 destructive blocked" in out


def test_report_by_channel_empty_window(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    audit_path = _write_channel_audit_log(tmp_path)
    rc = aegis_cli._cmd_report_by_channel(audit_path, since_secs=1)
    assert rc == 0
    out = capsys.readouterr().out
    assert "no records in window" in out


# ── parser smoke ────────────────────────────────────────────────────


def test_report_by_channel_arg_parses() -> None:
    parser = aegis_cli.build_parser()
    args = parser.parse_args(["report", "--by-channel"])
    assert args.by_channel is True
    assert args.by_aid is False  # mutually exclusive in usage, not enforced


def test_report_by_channel_default_off() -> None:
    parser = aegis_cli.build_parser()
    args = parser.parse_args(["report"])
    assert args.by_channel is False


# ── _extract_audit_fields surfaces channel ──────────────────────────


def test_extract_audit_fields_includes_channel() -> None:
    rec = {
        "ts_ns": 1,
        "tool": "Bash",
        "aid": "x",
        "decision": "ALLOW",
        "reason": "",
        "channel": "telegram",
    }
    out = aegis_cli._extract_audit_fields(rec)
    assert out["channel"] == "telegram"


def test_extract_audit_fields_channel_default_empty() -> None:
    rec = {
        "ts_ns": 1,
        "tool": "Bash",
        "aid": "x",
        "decision": "ALLOW",
        "reason": "",
    }
    out = aegis_cli._extract_audit_fields(rec)
    assert out["channel"] == ""


def test_extract_audit_fields_channel_from_sidecar_payload() -> None:
    """Sidecar audit records nest header under payload — make sure
    the extractor finds channel both ways."""
    rec = {
        "payload": {
            "header": {
                "decision": "BLOCK",
                "tool_name": "shell",
                "channel": "slack",
                "timestamp_ns": 1,
            },
        },
    }
    out = aegis_cli._extract_audit_fields(rec)
    assert out["channel"] == "slack"


# ── End-to-end via FastAPI route (route registration smoke) ────────


def test_router_registers_evaluate_openclaw_route() -> None:
    """The new /evaluate/openclaw route must be registered alongside
    the legacy /evaluate so OpenClaw plugin clients have a stable
    public URL."""
    from unittest.mock import MagicMock

    from aegis.api.evaluate import make_router

    r = make_router(
        key=MagicMock(),
        db=MagicMock(),
        log=MagicMock(),
    )
    paths = {route.path for route in r.routes}  # type: ignore[attr-defined]
    assert "/evaluate" in paths
    assert "/evaluate/openclaw" in paths
