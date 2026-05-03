"""Unit tests for ``aegis uninstall`` and ``aegis report --explain --json``."""

from __future__ import annotations

import argparse
import io
import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "tools"))

import aegis_cli  # noqa: E402,I001


# ─────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────


@pytest.fixture
def isolated_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Redirect SETTINGS_PATH to a tmp file. Returns the path."""
    settings = tmp_path / "settings.json"
    monkeypatch.setattr(aegis_cli, "SETTINGS_PATH", settings)
    return settings


def _write_settings(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2))


def _aegis_pretool_entry(label: str = "local") -> dict:
    cmd = (
        "AEGIS_EMBEDDING_PROVIDER=dummy AEGIS_JUDGE_PROVIDER=dummy "
        "/path/to/.venv/bin/python /path/to/tools/aegis_local_hook.py"
    )
    if label == "sidecar":
        cmd = "/path/to/.venv/bin/python /path/to/tools/aegis_hook.py"
    return {"matcher": "*", "hooks": [{"type": "command", "command": cmd}]}


def _aegis_post_entry() -> dict:
    return {
        "matcher": "*",
        "hooks": [{
            "type": "command",
            "command": "PYTHONPATH=/p /v/bin/python /p/tools/hooks/post_tool.py",
        }],
    }


def _third_party_entry(cmd: str = "/usr/local/bin/prettier-check") -> dict:
    return {"matcher": "Edit", "hooks": [{"type": "command", "command": cmd}]}


# ─────────────────────────────────────────────────────────────────────
# uninstall: argparse
# ─────────────────────────────────────────────────────────────────────


class TestUninstallArgparse:
    def test_uninstall_subcommand_dispatches(self) -> None:
        args = aegis_cli.build_parser().parse_args(["uninstall"])
        assert args.fn is aegis_cli.cmd_uninstall
        assert args.dry_run is False
        assert args.no_backup is False

    def test_uninstall_dry_run_flag(self) -> None:
        args = aegis_cli.build_parser().parse_args(["uninstall", "--dry-run"])
        assert args.dry_run is True

    def test_uninstall_no_backup_flag(self) -> None:
        args = aegis_cli.build_parser().parse_args(["uninstall", "--no-backup"])
        assert args.no_backup is True


# ─────────────────────────────────────────────────────────────────────
# uninstall: behaviour
# ─────────────────────────────────────────────────────────────────────


def _ns(*, dry_run: bool = False, no_backup: bool = False) -> argparse.Namespace:
    return argparse.Namespace(dry_run=dry_run, no_backup=no_backup)


class TestUninstallBehaviour:
    def test_no_settings_returns_zero(self, isolated_settings: Path) -> None:
        # File doesn't exist
        rc = aegis_cli.cmd_uninstall(_ns())
        assert rc == 0

    def test_no_aegis_hooks_present_returns_zero_idempotent(
        self, isolated_settings: Path,
    ) -> None:
        _write_settings(isolated_settings, {
            "hooks": {"PreToolUse": [_third_party_entry()]},
        })
        rc = aegis_cli.cmd_uninstall(_ns())
        assert rc == 0
        # File untouched.
        data = json.loads(isolated_settings.read_text())
        assert any(
            "prettier-check" in h["command"]
            for entry in data["hooks"]["PreToolUse"]
            for h in entry["hooks"]
        )

    def test_removes_local_pretool_hook(
        self, isolated_settings: Path,
    ) -> None:
        _write_settings(isolated_settings, {
            "hooks": {
                "PreToolUse": [_aegis_pretool_entry("local")],
                "PostToolUse": [_aegis_post_entry()],
            },
        })
        rc = aegis_cli.cmd_uninstall(_ns(no_backup=True))
        assert rc == 0
        data = json.loads(isolated_settings.read_text())
        # Stages now empty (preserved keys, dropped entries).
        assert data["hooks"]["PreToolUse"] == []
        assert data["hooks"]["PostToolUse"] == []

    def test_preserves_third_party_hooks(
        self, isolated_settings: Path,
    ) -> None:
        _write_settings(isolated_settings, {
            "hooks": {
                "PreToolUse": [
                    _aegis_pretool_entry("local"),
                    _third_party_entry("/usr/local/bin/prettier-check"),
                    _third_party_entry("/usr/local/bin/gitleaks"),
                ],
            },
        })
        aegis_cli.cmd_uninstall(_ns(no_backup=True))
        data = json.loads(isolated_settings.read_text())
        cmds = [
            h["command"]
            for entry in data["hooks"]["PreToolUse"]
            for h in entry["hooks"]
        ]
        # Both third-party hooks survive; the Aegis one is gone.
        assert "/usr/local/bin/prettier-check" in cmds
        assert "/usr/local/bin/gitleaks" in cmds
        assert not any("aegis_local_hook" in c for c in cmds)

    def test_dry_run_does_not_write(
        self, isolated_settings: Path,
    ) -> None:
        original = {
            "hooks": {"PreToolUse": [_aegis_pretool_entry("local")]},
        }
        _write_settings(isolated_settings, original)
        before_text = isolated_settings.read_text()
        rc = aegis_cli.cmd_uninstall(_ns(dry_run=True))
        assert rc == 0
        # File bytes unchanged.
        assert isolated_settings.read_text() == before_text

    def test_default_creates_backup(
        self, isolated_settings: Path,
    ) -> None:
        _write_settings(isolated_settings, {
            "hooks": {"PreToolUse": [_aegis_pretool_entry("local")]},
        })
        aegis_cli.cmd_uninstall(_ns())
        # A settings.json.bak.<ts> sibling exists.
        backups = list(isolated_settings.parent.glob("settings.json.bak.*"))
        assert len(backups) == 1

    def test_no_backup_skips_safety_copy(
        self, isolated_settings: Path,
    ) -> None:
        _write_settings(isolated_settings, {
            "hooks": {"PreToolUse": [_aegis_pretool_entry("local")]},
        })
        aegis_cli.cmd_uninstall(_ns(no_backup=True))
        backups = list(isolated_settings.parent.glob("settings.json.bak.*"))
        assert backups == []

    def test_removes_sidecar_pretool_hook(
        self, isolated_settings: Path,
    ) -> None:
        """Sidecar-mode pretool entry uses ``aegis_hook.py`` (not
        ``aegis_local_hook.py``); both fingerprints must be recognised."""
        _write_settings(isolated_settings, {
            "hooks": {"PreToolUse": [_aegis_pretool_entry("sidecar")]},
        })
        aegis_cli.cmd_uninstall(_ns(no_backup=True))
        data = json.loads(isolated_settings.read_text())
        assert data["hooks"]["PreToolUse"] == []

    def test_malformed_json_returns_one(
        self, isolated_settings: Path,
    ) -> None:
        isolated_settings.write_text("{ not valid json")
        rc = aegis_cli.cmd_uninstall(_ns())
        assert rc == 1

    def test_install_uninstall_roundtrip_idempotent(
        self,
        monkeypatch: pytest.MonkeyPatch,
        isolated_settings: Path,
        tmp_path: Path,
    ) -> None:
        """End-to-end: install creates entries, uninstall removes them
        cleanly, third-party entries inserted between are preserved."""
        # Patch hook script paths to filenames matching the rotation
        # fingerprints (so the uninstall path's _is_aegis_owned()
        # correctly identifies them as Aegis-owned).
        script_names = {
            "HOOK_SCRIPT":       "aegis_hook.py",
            "LOCAL_HOOK_SCRIPT": "aegis_local_hook.py",
            "POST_HOOK_SCRIPT":  "tools/hooks/post_tool.py",
            "STOP_HOOK_SCRIPT":  "tools/hooks/session_end.py",
        }
        for attr, fname in script_names.items():
            p = tmp_path / fname
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("#!/usr/bin/env python3\n")
            p.chmod(0o755)
            monkeypatch.setattr(aegis_cli, attr, p)
        manifest = tmp_path / "plugin.json"
        manifest.write_text(json.dumps({"name": "aegis-mvp", "version": "2.0.0"}))
        monkeypatch.setattr(aegis_cli, "PLUGIN_MANIFEST", manifest)

        # Pre-existing third-party
        _write_settings(isolated_settings, {
            "hooks": {"PreToolUse": [_third_party_entry()]},
        })

        # Install
        ns_install = argparse.Namespace(
            mode="local", judge="dummy", embedding="dummy", force=False,
        )
        assert aegis_cli.cmd_install(ns_install) == 0
        data = json.loads(isolated_settings.read_text())
        # Both Aegis and the third-party hook are present.
        all_cmds = [
            h["command"]
            for stage in data["hooks"].values()
            for entry in stage
            for h in entry.get("hooks", [])
        ]
        assert any("aegis_local_hook" in c for c in all_cmds)
        assert any("prettier-check" in c for c in all_cmds)

        # Uninstall
        assert aegis_cli.cmd_uninstall(_ns(no_backup=True)) == 0
        data = json.loads(isolated_settings.read_text())
        all_cmds = [
            h["command"]
            for stage in data["hooks"].values()
            for entry in stage
            for h in entry.get("hooks", [])
        ]
        # Aegis gone, third-party preserved.
        assert not any("aegis_local_hook" in c for c in all_cmds)
        assert not any("post_tool.py" in c for c in all_cmds)
        assert not any("session_end.py" in c for c in all_cmds)
        assert any("prettier-check" in c for c in all_cmds)


# ─────────────────────────────────────────────────────────────────────
# report --explain --json
# ─────────────────────────────────────────────────────────────────────


@pytest.fixture
def audit_log_with_explain(tmp_path: Path) -> Path:
    """Reuse the fixture pattern from test_report_explain.py."""
    path = tmp_path / "audit.jsonl"
    rec = {
        "ts_ns": 1,
        "tool": "Edit",
        "aid": "sess-test",
        "decision": "BLOCK",
        "reason": "credential pattern",
        "trace_id": "abc" * 11,
        "latency_ms": 12.3,
        "mode": "local",
        "explain": {
            "atv_dim": 2080,
            "atv_sha3": "0123" * 16,
            "step_traces": {"step310_args.run": "step310: hit"},
            "m13_top": [{"subfield": "tool_arg_inspection", "score": 0.95}],
            "m13_score": 0.95,
        },
    }
    path.write_text(json.dumps(rec) + "\n")
    return path


def _capture_stdout(fn: callable, *args, **kwargs) -> tuple[int, str]:
    buf = io.StringIO()
    real_stdout = sys.stdout
    sys.stdout = buf
    try:
        rc = fn(*args, **kwargs)
    finally:
        sys.stdout = real_stdout
    return rc, buf.getvalue()


class TestReportExplainJson:
    def test_argparse_json_flag(self) -> None:
        args = aegis_cli.build_parser().parse_args(
            ["report", "--explain", "LAST", "--json"]
        )
        assert args.json is True
        assert args.explain == "LAST"

    def test_argparse_json_default_false(self) -> None:
        args = aegis_cli.build_parser().parse_args(
            ["report", "--explain", "LAST"]
        )
        assert args.json is False

    def test_json_output_is_one_line(
        self, audit_log_with_explain: Path,
    ) -> None:
        rc, out = _capture_stdout(
            aegis_cli._cmd_report_explain,
            audit_log_with_explain, "LAST", as_json=True,
        )
        assert rc == 0
        # Strip trailing newline; output should be exactly one JSON line.
        lines = out.strip().split("\n")
        assert len(lines) == 1

    def test_json_output_parses_to_full_record(
        self, audit_log_with_explain: Path,
    ) -> None:
        _, out = _capture_stdout(
            aegis_cli._cmd_report_explain,
            audit_log_with_explain, "LAST", as_json=True,
        )
        data = json.loads(out.strip())
        # Top-level audit fields preserved verbatim.
        assert data["decision"] == "BLOCK"
        assert data["tool"] == "Edit"
        assert data["latency_ms"] == 12.3
        # Explain block embedded as-is.
        assert "explain" in data
        assert data["explain"]["m13_score"] == 0.95
        assert data["explain"]["m13_top"][0]["subfield"] == "tool_arg_inspection"

    def test_json_output_for_unknown_trace_returns_one_with_error_envelope(
        self, audit_log_with_explain: Path,
    ) -> None:
        rc, out = _capture_stdout(
            aegis_cli._cmd_report_explain,
            audit_log_with_explain, "no-such-trace", as_json=True,
        )
        assert rc == 1
        data = json.loads(out.strip())
        assert data["error"] == "not_found"
        assert data["target"] == "no-such-trace"

    def test_human_mode_unaffected_by_json_addition(
        self, audit_log_with_explain: Path,
    ) -> None:
        """Existing --explain (no --json) output unchanged."""
        rc, out = _capture_stdout(
            aegis_cli._cmd_report_explain,
            audit_log_with_explain, "LAST", as_json=False,
        )
        assert rc == 0
        assert "Decision Explanation" in out
        # Should NOT be valid JSON
        with pytest.raises(json.JSONDecodeError):
            json.loads(out.split("\n")[0])

    def test_json_via_cmd_report_full_path(
        self,
        monkeypatch: pytest.MonkeyPatch,
        audit_log_with_explain: Path,
    ) -> None:
        """End-to-end through cmd_report (not just _cmd_report_explain)."""
        ns = argparse.Namespace(
            audit=str(audit_log_with_explain),
            since=None,
            verbose=False,
            explain="LAST",
            json=True,
        )
        rc, out = _capture_stdout(aegis_cli.cmd_report, ns)
        assert rc == 0
        data = json.loads(out.strip())
        assert data["decision"] == "BLOCK"
