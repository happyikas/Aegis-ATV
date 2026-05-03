"""End-to-end Claude Code plug-in tests (plugin checkup PR).

Each test feeds a Claude Code-shaped PreToolUse payload through the
same code path the live ``aegis_local_hook.py`` uses (build_atv →
run_firewall) and verifies the verdict matches the documented
expectation in :mod:`demo.plugin_scenarios`.

Two adapter variants:
* sparse  — :func:`aegis.atv.adapter.from_claude_code_payload` (v4.4)
* enhanced — :func:`aegis.atv.adapter.from_claude_code_payload_enhanced`
              (plugin-checkup PR)

The 7 scenarios cover real coding-AI incidents (Replit DB deletion,
secret leak, self-modification, loop attack, cost runaway, capability
escalation, system-file probe).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# demo/ is not a package; import directly via path.
DEMO_DIR = Path(__file__).resolve().parents[2] / "demo"
sys.path.insert(0, str(DEMO_DIR))

from plugin_scenarios import (  # noqa: E402
    Scenario,
    build_scenarios,
    run_scenario,
)


@pytest.fixture
def scenarios(tmp_path: Path) -> list[Scenario]:
    """Build scenarios with a per-test transcripts directory."""
    return build_scenarios(tmp_path)


# ─────────────────────────────────────────────────────────────────────
# Per-scenario tests with hybrid judge (production-recommended)
# ─────────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _hybrid_judge(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force hybrid judge for these tests; matches production setting.

    settings is a pydantic-settings singleton frozen at import time, so
    monkeypatching the env var doesn't propagate. Mutate the live
    attribute instead.
    """
    from aegis.config import settings as _settings
    monkeypatch.setattr(_settings, "aegis_judge_provider", "hybrid")
    monkeypatch.setattr(_settings, "aegis_embedding_provider", "dummy")


def test_scenario_1_replit_style_db_destruction(scenarios: list[Scenario]) -> None:
    s1 = scenarios[0]
    r = run_scenario(s1, enhanced=True)
    assert r.pass_fail == "PASS", f"unexpected verdict {r.actual_decision}: {r.actual_reason}"
    assert r.actual_decision in {"BLOCK", "REQUIRE_APPROVAL"}


def test_scenario_2_secret_leak(scenarios: list[Scenario]) -> None:
    s2 = scenarios[1]
    r = run_scenario(s2, enhanced=True)
    assert r.pass_fail == "PASS"
    assert r.actual_decision in {"BLOCK", "REQUIRE_APPROVAL"}


def test_scenario_3_instruction_drift(scenarios: list[Scenario]) -> None:
    """CLAUDE.md baseline drift → BLOCK."""
    s3 = scenarios[2]
    r = run_scenario(s3, enhanced=True)
    # When baseline is seeded, drift detection fires.
    assert r.actual_decision == "BLOCK"
    assert "instruction_drift" in r.actual_reason.lower() or \
           "modified" in r.actual_reason.lower()


def test_scenario_4_loop_attack(scenarios: list[Scenario]) -> None:
    s4 = scenarios[3]
    r = run_scenario(s4, enhanced=True)
    assert r.pass_fail == "PASS"
    assert r.actual_decision in {"BLOCK", "REQUIRE_APPROVAL"}


def test_scenario_5_cost_runaway(scenarios: list[Scenario]) -> None:
    s5 = scenarios[4]
    r = run_scenario(s5, enhanced=True)
    # Cost-divergence flips to REQUIRE_APPROVAL when cumulative
    # dollars exceed budget.
    assert r.pass_fail == "PASS"


def test_scenario_6_capability_escalation(scenarios: list[Scenario]) -> None:
    s6 = scenarios[5]
    r = run_scenario(s6, enhanced=True)
    assert r.pass_fail == "PASS"
    assert r.actual_decision == "BLOCK"
    assert "capability" in r.actual_reason.lower()


def test_scenario_7_system_file_probe(scenarios: list[Scenario]) -> None:
    s7 = scenarios[6]
    r = run_scenario(s7, enhanced=True)
    assert r.pass_fail == "PASS"
    assert r.actual_decision in {"BLOCK", "REQUIRE_APPROVAL"}


# ─────────────────────────────────────────────────────────────────────
# All 7 in one go (CI smoke test)
# ─────────────────────────────────────────────────────────────────────


def test_all_seven_scenarios_pass(scenarios: list[Scenario]) -> None:
    """Run all 7 in sequence — at least 6 must PASS."""
    results = [run_scenario(s, enhanced=True) for s in scenarios]
    n_pass = sum(1 for r in results if r.pass_fail == "PASS")
    assert n_pass >= 6, (
        f"Only {n_pass}/7 scenarios passed. Failures: "
        + ", ".join(f"#{r.id}: {r.actual_decision}" for r in results if r.pass_fail != "PASS")
    )


# ─────────────────────────────────────────────────────────────────────
# Sparse vs enhanced adapter — comparison
# ─────────────────────────────────────────────────────────────────────


def test_enhanced_adapter_richer_atv(tmp_path: Path) -> None:
    """Enhanced adapter must populate >= 4 more subfields than sparse
    when a transcript is available."""
    from aegis.atv.adapter import (
        from_claude_code_payload,
        from_claude_code_payload_enhanced,
    )

    # Seed a transcript
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text(
        "\n".join(
            json.dumps(ev)
            for ev in [
                {"type": "user", "content": "Help me debug"},
                {"type": "assistant", "content": "I'll start by reading files",
                 "usage": {"input_tokens": 100, "output_tokens": 50}},
                {"type": "tool_use", "name": "Read", "input": {"file_path": "/tmp/x"}},
                {"type": "assistant", "content": "Plan: refactor auth module",
                 "usage": {"input_tokens": 100, "output_tokens": 80}},
            ]
        )
    )

    payload = {
        "session_id": "sess-1",
        "transcript_path": str(transcript),
        "tool_name": "Edit",
        "tool_input": {"file_path": "/tmp/y", "new_string": "..."},
        "hook_event_name": "PreToolUse",
        "invocation_id": "inv-1",
        "cwd": "/tmp",
    }

    sparse = from_claude_code_payload(payload, tenant_id="t")
    enhanced = from_claude_code_payload_enhanced(payload, tenant_id="t")

    # Sparse leaves these empty:
    assert sparse.agent_state_text == ""
    assert sparse.recent_actions == []
    assert sparse.memory_fingerprint is None
    assert sparse.cost_estimate.cumulative_tokens == 0

    # Enhanced populates them:
    assert enhanced.agent_state_text != ""  # last assistant message
    assert len(enhanced.recent_actions) >= 1
    assert enhanced.memory_fingerprint is not None
    assert len(enhanced.memory_fingerprint) == 64  # SHA3-256 hex
    assert enhanced.cost_estimate.cumulative_tokens > 0


def test_enhanced_adapter_falls_back_when_no_transcript() -> None:
    """No transcript → enhanced returns same as sparse."""
    from aegis.atv.adapter import (
        from_claude_code_payload,
        from_claude_code_payload_enhanced,
    )

    payload = {
        "session_id": "sess-1",
        "tool_name": "Bash",
        "tool_input": {"command": "ls"},
        "hook_event_name": "PreToolUse",
        "invocation_id": "inv-1",
    }
    sparse = from_claude_code_payload(payload, tenant_id="t")
    enhanced = from_claude_code_payload_enhanced(payload, tenant_id="t")

    assert sparse.tool_name == enhanced.tool_name
    assert sparse.tool_args_json == enhanced.tool_args_json
    assert enhanced.agent_state_text == sparse.agent_state_text  # both empty


def test_enhanced_adapter_handles_missing_transcript() -> None:
    """Pointing at a nonexistent transcript → graceful fallback."""
    from aegis.atv.adapter import from_claude_code_payload_enhanced

    payload = {
        "session_id": "sess-1",
        "transcript_path": "/nonexistent/path/transcript.jsonl",
        "tool_name": "Bash",
        "tool_input": {"command": "ls"},
        "hook_event_name": "PreToolUse",
        "invocation_id": "inv-1",
    }
    inp = from_claude_code_payload_enhanced(payload, tenant_id="t")
    # Should not crash; should fall back to sparse defaults.
    assert inp.tool_name == "Bash"
    assert inp.agent_state_text == ""


# ─────────────────────────────────────────────────────────────────────
# Transcript reader unit tests
# ─────────────────────────────────────────────────────────────────────


def test_transcript_reader_extracts_assistant_text(tmp_path: Path) -> None:
    from aegis.atv.transcript_reader import read_transcript_context

    p = tmp_path / "t.jsonl"
    p.write_text(
        "\n".join(
            json.dumps(ev)
            for ev in [
                {"type": "assistant", "content": "Hello world!"},
                {"type": "user", "content": "Hi"},
                {"type": "assistant", "content": "Plan: explore the repo first"},
            ]
        )
    )
    ctx = read_transcript_context(p, next_tool_args_json='{"x": 1}')
    assert ctx is not None
    assert "Plan: explore" in ctx.last_assistant_message
    assert ctx.transcript_sha3 is not None


def test_transcript_reader_extracts_tool_calls(tmp_path: Path) -> None:
    from aegis.atv.transcript_reader import read_transcript_context

    p = tmp_path / "t.jsonl"
    p.write_text(
        "\n".join(
            json.dumps(ev)
            for ev in [
                {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}},
                {"type": "tool_use", "name": "Edit", "input": {"file_path": "/x"}},
                {"type": "tool_use", "name": "mcp__github__create_issue",
                 "input": {"title": "bug"}},
            ]
        )
    )
    ctx = read_transcript_context(p, next_tool_args_json="")
    assert ctx is not None
    assert len(ctx.recent_tool_calls) == 3
    # MCP signal appears
    assert ctx.mcp_signals.get("server_identity_score", 0) > 0


def test_transcript_reader_cumulative_cost(tmp_path: Path) -> None:
    from aegis.atv.transcript_reader import read_transcript_context

    p = tmp_path / "t.jsonl"
    p.write_text(
        "\n".join(
            json.dumps(ev)
            for ev in [
                {"type": "assistant", "content": "x",
                 "usage": {"input_tokens": 100, "output_tokens": 50}},
                {"type": "assistant", "content": "y",
                 "usage": {"input_tokens": 200, "output_tokens": 100}},
            ]
        )
    )
    ctx = read_transcript_context(p, next_tool_args_json="")
    assert ctx is not None
    assert ctx.cumulative_cost.input_token_count == 300
    assert ctx.cumulative_cost.output_token_count == 150
    assert ctx.cumulative_cost.cumulative_dollars > 0


def test_transcript_reader_returns_none_for_empty_file(tmp_path: Path) -> None:
    from aegis.atv.transcript_reader import read_transcript_context

    p = tmp_path / "empty.jsonl"
    p.write_text("")
    assert read_transcript_context(p, next_tool_args_json="") is None


def test_transcript_reader_returns_none_for_missing_file(tmp_path: Path) -> None:
    from aegis.atv.transcript_reader import read_transcript_context

    assert read_transcript_context(
        tmp_path / "nope.jsonl", next_tool_args_json="",
    ) is None


def test_transcript_reader_novelty_zero_for_identical(tmp_path: Path) -> None:
    """Same args as recent history → novelty ~ 0."""
    from aegis.atv.transcript_reader import read_transcript_context

    p = tmp_path / "t.jsonl"
    # Need ≥100 bytes to clear the MIN_TRANSCRIPT_BYTES gate.
    p.write_text(
        "\n".join(
            json.dumps({"type": "tool_use", "name": "Bash",
                        "input": {"command": "rm -rf /tmp/x"}})
            for _ in range(3)
        )
    )
    ctx = read_transcript_context(
        p, next_tool_args_json='{"command": "rm -rf /tmp/x"}',
    )
    assert ctx is not None
    assert ctx.novelty_score < 0.5  # high overlap → low novelty


def test_transcript_reader_novelty_high_for_disjoint(tmp_path: Path) -> None:
    """Totally different args → novelty ~ 1."""
    from aegis.atv.transcript_reader import read_transcript_context

    p = tmp_path / "t.jsonl"
    p.write_text(
        "\n".join(
            json.dumps({"type": "tool_use", "name": "Bash",
                        "input": {"command": "echo hello world banana"}})
            for _ in range(3)
        )
    )
    ctx = read_transcript_context(
        p,
        next_tool_args_json='{"file_path": "/totally/different/zebra/path"}',
    )
    assert ctx is not None
    assert ctx.novelty_score > 0.5


# ─────────────────────────────────────────────────────────────────────
# PostToolUse hook
# ─────────────────────────────────────────────────────────────────────


def test_posttool_hook_handles_success(tmp_path: Path, monkeypatch) -> None:
    """PostToolUse with success exit_code → status='success', no crash."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools" / "hooks"))
    monkeypatch.setenv(
        "AEGIS_LOCAL_AUDIT", str(tmp_path / "audit.jsonl")
    )
    # Reload to pick up the new env var
    import importlib

    import post_tool
    importlib.reload(post_tool)

    payload = {
        "hook_event_name": "PostToolUse",
        "session_id": "s",
        "tool_name": "Bash",
        "tool_input": {"command": "ls"},
        "tool_response": {"output": "file1\nfile2"},
        "exit_code": 0,
        "invocation_id": "inv-1",
    }
    import io
    rc = post_tool.handle_posttool(io.StringIO(json.dumps(payload)), io.StringIO())
    assert rc == 0
    audit_log = tmp_path / "audit.jsonl"
    assert audit_log.exists()


def test_posttool_hook_handles_empty_input(tmp_path: Path) -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools" / "hooks"))
    import io

    import post_tool
    rc = post_tool.handle_posttool(io.StringIO(""), io.StringIO())
    assert rc == 0


def test_posttool_hook_classifies_failure(tmp_path: Path) -> None:
    """Non-zero exit_code → status='failure'."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools" / "hooks"))
    import post_tool

    payload = {
        "hook_event_name": "PostToolUse",
        "session_id": "s",
        "tool_name": "Bash",
        "tool_input": {},
        "tool_response": {},
        "exit_code": 1,
        "invocation_id": "inv-2",
    }
    rc = post_tool._classify_status(payload["tool_response"], 1)
    assert rc == "failure"


def test_posttool_hook_classifies_timeout() -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools" / "hooks"))
    import post_tool

    assert post_tool._classify_status({}, 124) == "timeout"
    assert post_tool._classify_status({}, 137) == "timeout"


def test_posttool_hook_handles_error_dict() -> None:
    """tool_response with error key → failure."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools" / "hooks"))
    import post_tool

    assert post_tool._classify_status({"is_error": True}, None) == "failure"
    assert post_tool._classify_status({"error": "boom"}, None) == "failure"
