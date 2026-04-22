"""Tests for Step 320 — DOGFOOD Rec #1 Bash sub-command classification.

The headline finding from the dogfood report was that 71% of Bash
calls in a normal Claude Code session inherited blast=8 (the static
``execute_shell`` value) and got escalated by step 330 to
REQUIRE_APPROVAL. This refinement looks at the actual sub-command
and classifies into read_only (blast 2), local_mutation (blast 5),
or side_effecting (blast 8).
"""

from __future__ import annotations

import json

import pytest

from aegis.firewall.core import FirewallContext
from aegis.firewall.step320_blast import (
    _classify_bash,
    _extract_command_args,
    _first_words,
    reset_bash_policy_cache,
    run,
)
from tests.unit._firewall_helpers import ZERO_ATV, make_input


# ─────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────
def _shell(cmd: str) -> str:
    """Build the JSON tool_args_json a hook would emit for a Bash call."""
    return json.dumps({"command": cmd})


# ─────────────────────────────────────────────────────────────────────
# _extract_command_args
# ─────────────────────────────────────────────────────────────────────
class TestExtractCommandArgs:
    def test_extracts_simple_command(self) -> None:
        assert _extract_command_args(_shell("ls -la")) == "ls -la"

    def test_extracts_with_quoted_args(self) -> None:
        assert _extract_command_args(_shell('echo "hello world"')) == 'echo "hello world"'

    def test_extracts_with_pipes(self) -> None:
        assert _extract_command_args(_shell("ls | grep py")) == "ls | grep py"

    def test_returns_none_for_empty(self) -> None:
        assert _extract_command_args("") is None

    def test_returns_none_when_command_missing(self) -> None:
        assert _extract_command_args('{"path": "./x"}') is None


# ─────────────────────────────────────────────────────────────────────
# _first_words
# ─────────────────────────────────────────────────────────────────────
class TestFirstWords:
    def test_simple(self) -> None:
        assert _first_words("ls -la /tmp") == ["ls", "-la"]

    def test_default_n2(self) -> None:
        assert _first_words("git push --force") == ["git", "push"]

    def test_n1(self) -> None:
        assert _first_words("git status", n=1) == ["git"]

    def test_quoted(self) -> None:
        assert _first_words('echo "hi there" world')[:2] == ["echo", "hi there"]

    def test_unbalanced_quote_falls_back_to_split(self) -> None:
        # shlex would raise; the fallback uses plain split.
        result = _first_words('echo "unbalanced')
        assert result[0] == "echo"


# ─────────────────────────────────────────────────────────────────────
# _classify_bash — direct
# ─────────────────────────────────────────────────────────────────────
class TestClassifyBash:
    @pytest.fixture
    def policy(self) -> dict:
        return {
            "default_blast": 8,
            "two_word_overrides": {"git push": 8, "git push --force": 9},
            "categories": {
                "read_only": {
                    "blast": 2,
                    "commands": ["ls", "cat", "git", "pwd"],
                },
                "local_mutation": {
                    "blast": 5,
                    "commands": ["rm", "cp", "mv", "git add"],
                },
                "side_effecting": {
                    "blast": 8,
                    "commands": ["curl", "ssh"],
                },
            },
        }

    def test_read_only_one_word(self, policy: dict) -> None:
        assert _classify_bash("ls -la", policy) == 2

    def test_read_only_one_word_pwd(self, policy: dict) -> None:
        assert _classify_bash("pwd", policy) == 2

    def test_local_mutation_one_word(self, policy: dict) -> None:
        assert _classify_bash("rm tmp.txt", policy) == 5

    def test_side_effecting_one_word(self, policy: dict) -> None:
        assert _classify_bash("curl https://example.com", policy) == 8

    def test_two_word_override_takes_precedence(self, policy: dict) -> None:
        # "git" alone is read_only (2). "git push" is in two_word_overrides (8).
        assert _classify_bash("git push origin main", policy) == 8

    def test_two_word_override_in_category(self, policy: dict) -> None:
        # "git add" is in local_mutation. Should match before falling to "git" (read_only).
        assert _classify_bash("git add file.py", policy) == 5

    def test_unknown_falls_to_default(self, policy: dict) -> None:
        assert _classify_bash("xyzzy --foo", policy) == 8

    def test_empty_command_falls_to_default(self, policy: dict) -> None:
        assert _classify_bash("", policy) == 8


# ─────────────────────────────────────────────────────────────────────
# Full step320.run with real production policy
# ─────────────────────────────────────────────────────────────────────
class TestStep320Bash:
    def setup_method(self) -> None:
        reset_bash_policy_cache()

    @pytest.mark.parametrize(
        "cmd, expected_blast_max",
        [
            ("ls -la", 2),
            ("git status", 2),
            ("pwd", 2),
            ("cat README.md", 2),
            ("grep foo bar.txt", 2),
            ("echo hello", 2),
            ("printf 'x'", 2),
        ],
    )
    def test_read_only_bash_calls_get_low_blast(self, cmd: str, expected_blast_max: int) -> None:
        ctx = FirewallContext()
        run(ZERO_ATV, make_input(tool_name="execute_shell", tool_args_json=_shell(cmd)), ctx)
        assert ctx.blast_radius is not None
        assert ctx.blast_radius <= expected_blast_max, (
            f"command {cmd!r} got blast={ctx.blast_radius}, expected ≤ {expected_blast_max}"
        )

    @pytest.mark.parametrize(
        "cmd",
        [
            "rm tmp.txt",
            "cp a b",
            "mv x y",
            "mkdir new_dir",
        ],
    )
    def test_local_mutation_bash_gets_medium_blast(self, cmd: str) -> None:
        ctx = FirewallContext()
        run(ZERO_ATV, make_input(tool_name="execute_shell", tool_args_json=_shell(cmd)), ctx)
        assert ctx.blast_radius is not None
        assert 3 <= ctx.blast_radius <= 6, (
            f"command {cmd!r} got blast={ctx.blast_radius}, expected 3..6"
        )

    @pytest.mark.parametrize(
        "cmd",
        [
            "curl https://api.example.com",
            "ssh user@host",
            "kubectl delete pod foo",
            "git push origin main",
            "docker run -it ubuntu",
        ],
    )
    def test_side_effecting_bash_keeps_high_blast(self, cmd: str) -> None:
        ctx = FirewallContext()
        run(ZERO_ATV, make_input(tool_name="execute_shell", tool_args_json=_shell(cmd)), ctx)
        assert ctx.blast_radius is not None
        assert ctx.blast_radius >= 6, (
            f"command {cmd!r} got blast={ctx.blast_radius}, expected ≥ 6 (side-effecting)"
        )

    def test_unknown_bash_command_falls_to_default(self) -> None:
        ctx = FirewallContext()
        run(ZERO_ATV, make_input(tool_name="execute_shell", tool_args_json=_shell("xyzzy_made_up_tool")), ctx)
        # Falls to the policy's default_blast which mirrors the legacy execute_shell value.
        assert ctx.blast_radius == 8

    def test_empty_command_args_falls_to_static_table(self) -> None:
        ctx = FirewallContext()
        run(ZERO_ATV, make_input(tool_name="execute_shell", tool_args_json=""), ctx)
        # No command extracted, so we use the static TOOL_BLAST_TABLE value.
        assert ctx.blast_radius == 8

    def test_non_shell_tool_unaffected(self) -> None:
        """write_file and other non-shell tools still use TOOL_BLAST_TABLE."""
        ctx = FirewallContext()
        run(ZERO_ATV, make_input(tool_name="write_file"), ctx)
        assert ctx.blast_radius == 3  # from TOOL_BLAST_TABLE

    def test_publishes_bash_first_word_to_extras(self) -> None:
        ctx = FirewallContext()
        run(ZERO_ATV, make_input(tool_name="execute_shell", tool_args_json=_shell("git status")), ctx)
        assert ctx.extras.get("bash_first_word") == "git"
        assert ctx.extras.get("bash_blast_source") == "policy"
