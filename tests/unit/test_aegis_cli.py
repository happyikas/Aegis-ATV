"""Unit tests for tools/aegis_cli.py (D3).

Most subcommands import D4/D5/D7/D8/D10 backing modules lazily and will
raise ImportError until those D-numbered ports land. We exercise:

- argparse: every subcommand wires up correctly and dispatches to the
  expected fn (via set_defaults).
- cmd_install: full path — fresh install, idempotent re-run, --force
  override, settings backup, JSON preservation. This is the only
  subcommand that is fully operational at D3.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "tools"))

import aegis_cli  # noqa: E402,I001


# ---- argparse wiring -------------------------------------------------------


@pytest.mark.parametrize(
    ("argv", "fn_name"),
    [
        (["status"], "cmd_status"),
        (["verify-audit"], "cmd_verify_audit"),
        (["replay"], "cmd_replay"),
        (["replay", "5"], "cmd_replay"),
        (["policy-replay"], "cmd_policy_replay"),
        (["cost"], "cmd_cost"),
        (["health"], "cmd_health"),
        (["rollback"], "cmd_rollback"),
        (["rollback", "inv-1"], "cmd_rollback"),
        (["snapshots"], "cmd_snapshots"),
        (["snapshots", "list"], "cmd_snapshots"),
        (["snapshots", "prune", "--older-than", "30d"], "cmd_snapshots"),
        (["burnin", "retrain"], "cmd_burnin"),
        (["burnin", "revert"], "cmd_burnin"),
        (
            ["cost-record", "--inv", "x", "--in", "10", "--out", "20"],
            "cmd_cost_record",
        ),
        (["cost-import", "transcript", "--path", "/tmp/t.jsonl"], "cmd_cost_import"),
        (["budget", "show"], "cmd_budget"),
        (["budget", "set", "--daily", "5"], "cmd_budget"),
        (["install"], "cmd_install"),
        (["install", "--force"], "cmd_install"),
    ],
)
def test_subcommand_dispatches_to_expected_fn(argv: list[str], fn_name: str) -> None:
    parser = aegis_cli.build_parser()
    args = parser.parse_args(argv)
    assert args.fn.__name__ == fn_name


def test_replay_default_n_is_20() -> None:
    args = aegis_cli.build_parser().parse_args(["replay"])
    assert args.n == 20


def test_cost_default_days_is_7() -> None:
    args = aegis_cli.build_parser().parse_args(["cost"])
    assert args.days == 7


def test_install_default_force_is_false() -> None:
    args = aegis_cli.build_parser().parse_args(["install"])
    assert args.force is False


# ---- _parse_window_secs ---------------------------------------------------


@pytest.mark.parametrize(
    ("spec", "secs"),
    [("7d", 7 * 86400), ("24h", 24 * 3600), ("3600", 3600)],
)
def test_parse_window_secs(spec: str, secs: int) -> None:
    assert aegis_cli._parse_window_secs(spec) == secs


# ---- cmd_install ---------------------------------------------------------


@pytest.fixture
def isolated_install(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Redirect SETTINGS_PATH and HOOK_SCRIPT into tmp dirs.

    cmd_install reads/writes the module-level SETTINGS_PATH and HOOK_SCRIPT,
    so tests must monkeypatch those instead of $HOME.
    """
    settings = tmp_path / ".claude" / "settings.json"
    hook = tmp_path / "tools" / "aegis_hook.py"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text("#!/usr/bin/env python3\nprint('stub hook')\n")
    hook.chmod(0o755)
    monkeypatch.setattr(aegis_cli, "SETTINGS_PATH", settings)
    monkeypatch.setattr(aegis_cli, "HOOK_SCRIPT", hook)
    return tmp_path


def _install_args(force: bool = False):  # type: ignore[no-untyped-def]
    """Build a minimal Namespace-like object for cmd_install."""
    import argparse

    return argparse.Namespace(force=force)


def test_cmd_install_creates_settings_when_absent(isolated_install: Path) -> None:
    rc = aegis_cli.cmd_install(_install_args())
    assert rc == 0
    settings = isolated_install / ".claude" / "settings.json"
    assert settings.exists()
    data = json.loads(settings.read_text())
    pretool = data["hooks"]["PreToolUse"]
    assert len(pretool) == 1
    assert "aegis_hook.py" in pretool[0]["hooks"][0]["command"]


def test_cmd_install_idempotent_second_run(isolated_install: Path) -> None:
    aegis_cli.cmd_install(_install_args())
    rc = aegis_cli.cmd_install(_install_args())
    assert rc == 0
    settings = isolated_install / ".claude" / "settings.json"
    data = json.loads(settings.read_text())
    # No second entry appended.
    assert len(data["hooks"]["PreToolUse"]) == 1


def test_cmd_install_force_appends_extra_entry(isolated_install: Path) -> None:
    aegis_cli.cmd_install(_install_args())
    rc = aegis_cli.cmd_install(_install_args(force=True))
    assert rc == 0
    settings = isolated_install / ".claude" / "settings.json"
    data = json.loads(settings.read_text())
    assert len(data["hooks"]["PreToolUse"]) == 2


def test_cmd_install_backs_up_existing_settings(isolated_install: Path) -> None:
    settings = isolated_install / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    # Pre-existing user settings unrelated to Aegis.
    settings.write_text(json.dumps({"theme": "dark", "hooks": {}}))
    rc = aegis_cli.cmd_install(_install_args())
    assert rc == 0
    backups = list(settings.parent.glob("settings.json.bak.*"))
    assert len(backups) == 1
    # Unrelated keys preserved.
    data = json.loads(settings.read_text())
    assert data["theme"] == "dark"


def test_cmd_install_refuses_invalid_existing_json(isolated_install: Path) -> None:
    settings = isolated_install / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text("{ this is not valid json")
    rc = aegis_cli.cmd_install(_install_args())
    assert rc == 1
    # File untouched.
    assert settings.read_text() == "{ this is not valid json"


def test_cmd_install_returns_1_when_hook_script_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = tmp_path / ".claude" / "settings.json"
    monkeypatch.setattr(aegis_cli, "SETTINGS_PATH", settings)
    monkeypatch.setattr(aegis_cli, "HOOK_SCRIPT", tmp_path / "does-not-exist.py")
    rc = aegis_cli.cmd_install(_install_args())
    assert rc == 1


def test_main_dispatches_via_argv(
    monkeypatch: pytest.MonkeyPatch, isolated_install: Path
) -> None:
    monkeypatch.setattr(sys, "argv", ["aegis", "install"])
    rc = aegis_cli.main()
    assert rc == 0
