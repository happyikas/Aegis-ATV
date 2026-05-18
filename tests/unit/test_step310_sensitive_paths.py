"""Tests for Step 310 — DOGFOOD Rec #2 (sensitive paths) + Rec #4 (surface_class).

Closes findings:
* FN-1 — `ls -la ~/.ssh/` (was ALLOWED) → REQUIRE_APPROVAL
* FN-2 — `cat ~/.aws/credentials` (was ALLOWED) → BLOCK
* FP-1 — Write of test fixture containing `rm -rf /` text → REQUIRE_APPROVAL not BLOCK
* FP-2 — `printf 'fake test: rm -rf /'` → still BLOCK (Bash IS execution surface)
"""

from __future__ import annotations

import json

import pytest

from aegis.firewall.core import FirewallContext
from aegis.firewall.step310_args import (
    _classify_path,
    _extract_paths_from_args,
    _match_path_glob,
    _surface_class,
    reset_sensitive_paths_cache,
    run,
)
from tests.unit._firewall_helpers import ZERO_ATV, make_input


def _shell(cmd: str) -> str:
    return json.dumps({"command": cmd})


def _read_args(file_path: str) -> str:
    return json.dumps({"file_path": file_path})


def _write_args(file_path: str, content: str) -> str:
    return json.dumps({"file_path": file_path, "content": content})


# ─────────────────────────────────────────────────────────────────────
# _surface_class
# ─────────────────────────────────────────────────────────────────────
class TestSurfaceClass:
    @pytest.mark.parametrize(
        "tool, expected",
        [
            ("execute_shell", "execution"),
            ("read_file", "execution"),
            ("list_directory", "execution"),
            ("db_query", "execution"),
            ("delete_file", "execution"),
            ("transfer_funds", "execution"),
            ("write_file", "content"),
        ],
    )
    def test_classification(self, tool: str, expected: str) -> None:
        assert _surface_class(tool) == expected


# ─────────────────────────────────────────────────────────────────────
# _match_path_glob
# ─────────────────────────────────────────────────────────────────────
class TestMatchPathGlob:
    def test_exact_match(self) -> None:
        assert _match_path_glob("/etc/shadow", "/etc/shadow")

    def test_tilde_expansion(self) -> None:
        # ~ expansion happens on both sides; the actual home matters.
        assert _match_path_glob("~/.ssh/id_rsa", "~/.ssh/id_rsa")

    def test_double_star_anywhere(self) -> None:
        assert _match_path_glob("/Users/x/secrets/db.json", "**/secrets/**")
        assert _match_path_glob("/srv/secrets/file", "**/secrets/**")

    def test_double_star_no_match(self) -> None:
        assert not _match_path_glob("/Users/x/foo/bar", "**/secrets/**")

    def test_glob_star_in_filename(self) -> None:
        assert _match_path_glob("~/.ssh/id_ed25519.pub", "~/.ssh/id_*.pub")
        assert _match_path_glob("~/.ssh/id_rsa.pub", "~/.ssh/id_*.pub")
        assert not _match_path_glob("~/.ssh/id_rsa", "~/.ssh/id_*.pub")


# ─────────────────────────────────────────────────────────────────────
# _classify_path
# ─────────────────────────────────────────────────────────────────────
class TestClassifyPath:
    @pytest.fixture
    def policy(self) -> dict:
        return {
            "block": {
                "patterns": ["~/.aws/credentials", "/etc/shadow", "**/*.pem", "~/.ssh/id_rsa"],
                "exceptions": ["~/.ssh/id_*.pub", "**/public.pem"],
            },
            "approve": {
                "patterns": ["~/.ssh/**", ".env", "**/secrets/**"],
            },
        }

    def test_block_credentials(self, policy: dict) -> None:
        assert _classify_path("~/.aws/credentials", policy) == "block"

    def test_block_shadow(self, policy: dict) -> None:
        assert _classify_path("/etc/shadow", policy) == "block"

    def test_block_pem(self, policy: dict) -> None:
        assert _classify_path("/Users/x/keys/cert.pem", policy) == "block"

    def test_exception_overrides_block(self, policy: dict) -> None:
        # ~/.ssh/id_rsa is blocked but id_*.pub is an exception.
        assert _classify_path("~/.ssh/id_rsa.pub", policy) == "approve"
        # Public pem also exempt.
        assert _classify_path("/Users/x/keys/public.pem", policy) == "ok"

    def test_id_rsa_still_blocked(self, policy: dict) -> None:
        # id_rsa (private) blocked, even though id_*.pub exists as exception.
        assert _classify_path("~/.ssh/id_rsa", policy) == "block"

    def test_approve_ssh_dir(self, policy: dict) -> None:
        assert _classify_path("~/.ssh/known_hosts", policy) == "approve"

    def test_approve_env(self, policy: dict) -> None:
        assert _classify_path(".env", policy) == "approve"

    def test_approve_secrets(self, policy: dict) -> None:
        assert _classify_path("/srv/secrets/db.json", policy) == "approve"

    def test_ok_for_normal_path(self, policy: dict) -> None:
        assert _classify_path("/Users/x/code/main.py", policy) == "ok"


# ─────────────────────────────────────────────────────────────────────
# _extract_paths_from_args
# ─────────────────────────────────────────────────────────────────────
class TestExtractPaths:
    def test_extracts_path_field(self) -> None:
        assert "/tmp/x.txt" in _extract_paths_from_args(json.dumps({"path": "/tmp/x.txt"}))

    def test_extracts_file_path_field(self) -> None:
        assert "~/.aws/credentials" in _extract_paths_from_args(_read_args("~/.aws/credentials"))

    def test_extracts_path_token_from_shell_command(self) -> None:
        paths = _extract_paths_from_args(_shell("cat ~/.aws/credentials"))
        assert "~/.aws/credentials" in paths

    def test_extracts_multiple_path_tokens(self) -> None:
        paths = _extract_paths_from_args(_shell("cp /etc/shadow /tmp/x"))
        assert "/etc/shadow" in paths
        assert "/tmp/x" in paths

    def test_no_path_returns_empty(self) -> None:
        assert _extract_paths_from_args(_shell("echo hello")) == []

    def test_relative_paths_not_collected(self) -> None:
        # We only flag paths starting with / or ~/ to avoid false-positive on every
        # ./relative reference.
        paths = _extract_paths_from_args(_shell("cat ./README.md"))
        assert "./README.md" not in paths


# ─────────────────────────────────────────────────────────────────────
# step310.run end-to-end with real production policy
# ─────────────────────────────────────────────────────────────────────
class TestStep310SensitivePaths:
    def setup_method(self) -> None:
        reset_sensitive_paths_cache()

    def test_aws_credentials_via_read_blocked(self) -> None:
        ctx = FirewallContext()
        r = run(ZERO_ATV, make_input(tool_name="read_file", tool_args_json=_read_args("~/.aws/credentials")), ctx)
        assert r.verdict == "BLOCK"
        assert "sensitive-path" in r.trace

    def test_aws_credentials_via_bash_blocked(self) -> None:
        """Closes FN-2: cat ~/.aws/credentials must now BLOCK."""
        ctx = FirewallContext()
        r = run(ZERO_ATV, make_input(tool_name="execute_shell", tool_args_json=_shell("cat ~/.aws/credentials")), ctx)
        assert r.verdict == "BLOCK"

    def test_ssh_directory_listing_requires_approval(self) -> None:
        """Closes FN-1: ls -la ~/.ssh/ must now require approval."""
        ctx = FirewallContext()
        r = run(ZERO_ATV, make_input(tool_name="execute_shell", tool_args_json=_shell("ls -la ~/.ssh/")), ctx)
        assert r.verdict == "REQUIRE_APPROVAL"

    def test_ssh_pubkey_allowed(self) -> None:
        """Exception: ~/.ssh/id_*.pub is whitelisted from the block list, but the
        *directory* approve pattern still matches → REQUIRE_APPROVAL (not BLOCK)."""
        ctx = FirewallContext()
        r = run(ZERO_ATV, make_input(tool_name="read_file", tool_args_json=_read_args("~/.ssh/id_ed25519.pub")), ctx)
        # Either OK (if exception fully clears) or REQUIRE_APPROVAL (matches ~/.ssh/**)
        assert r.verdict in (None, "REQUIRE_APPROVAL")

    def test_ssh_private_key_blocked(self) -> None:
        ctx = FirewallContext()
        r = run(ZERO_ATV, make_input(tool_name="read_file", tool_args_json=_read_args("~/.ssh/id_ed25519")), ctx)
        assert r.verdict == "BLOCK"

    def test_etc_shadow_blocked(self) -> None:
        ctx = FirewallContext()
        r = run(ZERO_ATV, make_input(tool_name="execute_shell", tool_args_json=_shell("cat /etc/shadow")), ctx)
        assert r.verdict == "BLOCK"

    def test_etc_passwd_requires_approval(self) -> None:
        """/etc/passwd is in the approve set (it's world-readable but
        sensitive). Sensitive-path step fires before regex step, so the
        /etc/passwd regex pattern never runs. That's fine — the user-
        visible behavior is still 'no silent read'."""
        ctx = FirewallContext()
        r = run(ZERO_ATV, make_input(tool_name="execute_shell", tool_args_json=_shell("cat /etc/passwd")), ctx)
        assert r.verdict == "REQUIRE_APPROVAL"

    def test_normal_file_passes(self) -> None:
        ctx = FirewallContext()
        r = run(ZERO_ATV, make_input(tool_name="read_file", tool_args_json=_read_args("./README.md")), ctx)
        assert r.verdict is None

    def test_claude_settings_json_blocked(self) -> None:
        """Reading ~/.claude/settings.json (the plugin hook config)
        must BLOCK — an agent scoping out the firewall to disable it."""
        ctx = FirewallContext()
        r = run(ZERO_ATV, make_input(
            tool_name="read_file",
            tool_args_json=_read_args("~/.claude/settings.json"),
        ), ctx)
        assert r.verdict == "BLOCK"

    def test_claude_settings_local_json_blocked(self) -> None:
        ctx = FirewallContext()
        r = run(ZERO_ATV, make_input(
            tool_name="read_file",
            tool_args_json=_read_args("~/.claude/settings.local.json"),
        ), ctx)
        assert r.verdict == "BLOCK"

    def test_aegis_state_dir_requires_approval(self) -> None:
        """~/.aegis/** (audit chain, intent log, autonomy state) is
        not a hard BLOCK because legitimate aegis CLI flows occasionally
        cat / jq these for diagnostics — but it must REQUIRE_APPROVAL
        so the user sees what's being touched."""
        ctx = FirewallContext()
        r = run(ZERO_ATV, make_input(
            tool_name="read_file",
            tool_args_json=_read_args("~/.aegis/audit.jsonl"),
        ), ctx)
        assert r.verdict == "REQUIRE_APPROVAL"


# ─────────────────────────────────────────────────────────────────────
# Surface-class split (Rec #4)
# ─────────────────────────────────────────────────────────────────────
class TestSurfaceClassSplit:
    def setup_method(self) -> None:
        reset_sensitive_paths_cache()

    def test_execution_surface_blocks_dangerous_pattern(self) -> None:
        """Bash with `rm -rf /` → BLOCK (unchanged from pre-DOGFOOD behavior)."""
        ctx = FirewallContext()
        r = run(ZERO_ATV, make_input(tool_name="execute_shell", tool_args_json=_shell("rm -rf /")), ctx)
        assert r.verdict == "BLOCK"

    def test_content_surface_approves_dangerous_pattern(self) -> None:
        """Closes FP-1: Write of file CONTAINING `rm -rf /` → REQUIRE_APPROVAL not BLOCK."""
        ctx = FirewallContext()
        r = run(
            ZERO_ATV,
            make_input(
                tool_name="write_file",
                tool_args_json=_write_args("/tmp/notes.md", "Today I learned: rm -rf / wipes the disk"),
            ),
            ctx,
        )
        assert r.verdict == "REQUIRE_APPROVAL", f"got {r.verdict} ({r.reason})"
        assert "content surface" in r.reason

    def test_read_file_with_dangerous_pattern_in_path_still_blocks(self) -> None:
        """read_file is still execution surface (the path gets dereferenced)."""
        ctx = FirewallContext()
        r = run(ZERO_ATV, make_input(tool_name="read_file", tool_args_json=_read_args("/etc/shadow")), ctx)
        # Will BLOCK at sensitive-path step, before reaching pattern check.
        assert r.verdict == "BLOCK"

    def test_sql_drop_in_write_content_approves_not_blocks(self) -> None:
        ctx = FirewallContext()
        r = run(
            ZERO_ATV,
            make_input(
                tool_name="write_file",
                tool_args_json=_write_args(
                    "/tmp/migrations.sql",
                    "-- migration: DROP TABLE old_users",
                ),
            ),
            ctx,
        )
        assert r.verdict == "REQUIRE_APPROVAL"
