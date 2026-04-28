"""Unit tests for step305 safe-action allowlist (v2.1 Day-1 #1)."""

from __future__ import annotations

import json
from typing import Any

import numpy as np
import pytest

from aegis.firewall import step305_safe_allowlist as step305
from aegis.firewall.core import FirewallContext
from aegis.schema import ATVHeader, ATVInput


def _atv_input(tool: str, args: dict[str, Any]) -> ATVInput:
    return ATVInput(
        header=ATVHeader(
            trace_id="t" * 32,
            span_id="s" * 16,
            tenant_id="t",
            aid="a",
            timestamp_ns=0,
        ),
        tool_name=tool,
        tool_args_json=json.dumps(args),
    )


def _run(tool: str, args: dict[str, Any]) -> tuple[FirewallContext, str]:
    inp = _atv_input(tool, args)
    ctx = FirewallContext()
    res = step305.run(np.zeros(2080, dtype=np.float32), inp, ctx)
    return ctx, res.trace


@pytest.fixture(autouse=True)
def _reset_cache() -> None:
    step305.reset_safe_actions_cache()


# ---- Tool entries (any_args=true) ---------------------------------------


@pytest.mark.parametrize("tool", ["Read", "Grep", "Glob"])
def test_read_only_tools_are_fast_pathed(tool: str) -> None:
    ctx, trace = _run(tool, {"file_path": "/etc/passwd"})  # path itself irrelevant for step305
    assert ctx.extras.get("safe_fast_path") is True
    assert ctx.extras.get("safe_match") == f"tool:{tool}"
    assert "safe_fast_path" in trace


def test_unknown_tool_does_not_fast_path() -> None:
    ctx, trace = _run("UnknownTool", {})
    assert ctx.extras.get("safe_fast_path") is None
    assert "not safe-listed" in trace


# ---- Bash subcommand prefix matching ------------------------------------


@pytest.mark.parametrize(
    "cmd",
    [
        "ls -la",
        "pwd",
        "git status",
        "git log --oneline",
        "uv run pytest tests/",
        "ruff check .",
        "mypy src",
        "npm test",
    ],
)
def test_safe_bash_subcommands_fast_path(cmd: str) -> None:
    ctx, _ = _run("Bash", {"command": cmd})
    assert ctx.extras.get("safe_fast_path") is True


def test_unknown_bash_subcommand_does_not_fast_path() -> None:
    ctx, _ = _run("Bash", {"command": "rm -rf /tmp/foo"})
    assert ctx.extras.get("safe_fast_path") is None


@pytest.mark.parametrize(
    "cmd",
    [
        "ls | rm -rf /",
        "ls; rm -rf /",
        "ls && rm -rf /",
        "ls > /etc/passwd",
        "ls $(rm -rf /)",
        "ls `rm -rf /`",
    ],
)
def test_pipeline_or_subshell_disqualifies_fast_path(cmd: str) -> None:
    """Even if the leading subcommand is on the allowlist, shell metachars
    revert the call to the full firewall pipeline so the LLM judge still
    sees the destructive surface.
    """
    ctx, _ = _run("Bash", {"command": cmd})
    assert ctx.extras.get("safe_fast_path") is None


def test_command_extracted_from_alt_arg_key() -> None:
    """tool_args may use 'cmd' instead of 'command' in some shells."""
    inp = _atv_input("Bash", {"cmd": "ls -la"})
    ctx = FirewallContext()
    step305.run(np.zeros(2080, dtype=np.float32), inp, ctx)
    assert ctx.extras.get("safe_fast_path") is True


def test_malformed_args_json_does_not_crash() -> None:
    inp = ATVInput(
        header=ATVHeader(
            trace_id="t" * 32,
            span_id="s" * 16,
            tenant_id="t",
            aid="a",
            timestamp_ns=0,
        ),
        tool_name="Bash",
        tool_args_json="not json {{",
    )
    ctx = FirewallContext()
    res = step305.run(np.zeros(2080, dtype=np.float32), inp, ctx)
    assert ctx.extras.get("safe_fast_path") is None
    assert res.verdict is None


def test_step305_never_blocks() -> None:
    """Even for the most destructive payloads, step305 itself returns None
    (it only flags or doesn't). The block decision is for downstream gates.
    """
    for cmd in ("rm -rf /", "DROP TABLE users", "git push --force origin main"):
        inp = _atv_input("Bash", {"command": cmd})
        ctx = FirewallContext()
        res = step305.run(np.zeros(2080, dtype=np.float32), inp, ctx)
        assert res.verdict is None


def test_non_shell_tool_with_command_arg_does_not_use_bash_path() -> None:
    """Only shell-class tools consult bash_subcommands; other tools that
    happen to have a 'command' key (e.g. a custom tool) shouldn't be
    fast-pathed by command text.
    """
    ctx, _ = _run("UnknownTool", {"command": "ls"})
    assert ctx.extras.get("safe_fast_path") is None
