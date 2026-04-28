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
import time
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


def _install_args(force: bool = False, mode: str = "sidecar"):  # type: ignore[no-untyped-def]
    """Build a minimal Namespace-like object for cmd_install."""
    import argparse

    return argparse.Namespace(force=force, mode=mode)


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


# ---- Phase 5: --mode sidecar|local + plugin manifest validation ---------


@pytest.fixture
def isolated_install_phase5(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Path:
    """Like ``isolated_install`` but also patches the local-hook + Stop-hook
    + plugin-manifest paths so a per-test sandbox is fully self-contained.
    """
    settings = tmp_path / ".claude" / "settings.json"
    sidecar_hook = tmp_path / "tools" / "aegis_hook.py"
    local_hook = tmp_path / "tools" / "aegis_local_hook.py"
    stop_hook = tmp_path / "tools" / "hooks" / "session_end.py"
    manifest = tmp_path / ".claude-plugin" / "plugin.json"
    sidecar_hook.parent.mkdir(parents=True, exist_ok=True)
    stop_hook.parent.mkdir(parents=True, exist_ok=True)
    manifest.parent.mkdir(parents=True, exist_ok=True)
    sidecar_hook.write_text("#!/usr/bin/env python3\nprint('sidecar')\n")
    local_hook.write_text("#!/usr/bin/env python3\nprint('local')\n")
    stop_hook.write_text("#!/usr/bin/env python3\nprint('stop')\n")
    sidecar_hook.chmod(0o755)
    local_hook.chmod(0o755)
    manifest.write_text(json.dumps({"name": "aegis-mvp", "version": "2.0.0"}))

    monkeypatch.setattr(aegis_cli, "SETTINGS_PATH", settings)
    monkeypatch.setattr(aegis_cli, "HOOK_SCRIPT", sidecar_hook)
    monkeypatch.setattr(aegis_cli, "LOCAL_HOOK_SCRIPT", local_hook)
    monkeypatch.setattr(aegis_cli, "STOP_HOOK_SCRIPT", stop_hook)
    monkeypatch.setattr(aegis_cli, "PLUGIN_MANIFEST", manifest)
    return tmp_path


def test_install_default_mode_is_sidecar() -> None:
    args = aegis_cli.build_parser().parse_args(["install"])
    assert args.mode == "sidecar"


def test_install_mode_local_is_accepted() -> None:
    args = aegis_cli.build_parser().parse_args(["install", "--mode", "local"])
    assert args.mode == "local"


def test_install_mode_invalid_rejected() -> None:
    with pytest.raises(SystemExit):
        aegis_cli.build_parser().parse_args(["install", "--mode", "weird"])


def test_validate_plugin_manifest_ok(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    manifest = tmp_path / "plugin.json"
    manifest.write_text(json.dumps({"name": "aegis-mvp", "version": "2.0.0"}))
    monkeypatch.setattr(aegis_cli, "PLUGIN_MANIFEST", manifest)
    ok, info = aegis_cli._validate_plugin_manifest()
    assert ok is True
    assert info == "2.0.0"


def test_validate_plugin_manifest_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(aegis_cli, "PLUGIN_MANIFEST", tmp_path / "absent.json")
    ok, msg = aegis_cli._validate_plugin_manifest()
    assert ok is False
    assert "not found" in msg


def test_validate_plugin_manifest_malformed_json(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    manifest = tmp_path / "plugin.json"
    manifest.write_text("not json {{{")
    monkeypatch.setattr(aegis_cli, "PLUGIN_MANIFEST", manifest)
    ok, msg = aegis_cli._validate_plugin_manifest()
    assert ok is False
    assert "not valid JSON" in msg


def test_validate_plugin_manifest_missing_required_fields(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    manifest = tmp_path / "plugin.json"
    manifest.write_text(json.dumps({"version": "2.0.0"}))
    monkeypatch.setattr(aegis_cli, "PLUGIN_MANIFEST", manifest)
    ok, msg = aegis_cli._validate_plugin_manifest()
    assert ok is False
    assert "name" in msg


def test_install_sidecar_command_uses_aegis_hook(isolated_install_phase5: Path) -> None:
    rc = aegis_cli.cmd_install(_install_args(mode="sidecar"))
    assert rc == 0
    settings = isolated_install_phase5 / ".claude" / "settings.json"
    data = json.loads(settings.read_text())
    cmd = data["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    assert "aegis_hook.py" in cmd
    assert "aegis_local_hook.py" not in cmd
    assert "AEGIS_POLICY_DIR" not in cmd


def test_install_local_command_embeds_env_and_local_hook(
    isolated_install_phase5: Path,
) -> None:
    rc = aegis_cli.cmd_install(_install_args(mode="local"))
    assert rc == 0
    settings = isolated_install_phase5 / ".claude" / "settings.json"
    data = json.loads(settings.read_text())
    cmd = data["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    assert "aegis_local_hook.py" in cmd
    assert "AEGIS_POLICY_DIR=" in cmd
    assert "PYTHONPATH=" in cmd


def test_install_registers_stop_hook(isolated_install_phase5: Path) -> None:
    aegis_cli.cmd_install(_install_args(mode="sidecar"))
    settings = isolated_install_phase5 / ".claude" / "settings.json"
    data = json.loads(settings.read_text())
    stops = data["hooks"]["Stop"]
    assert len(stops) == 1
    assert "session_end.py" in stops[0]["hooks"][0]["command"]


def test_install_stop_hook_idempotent_across_modes(
    isolated_install_phase5: Path,
) -> None:
    aegis_cli.cmd_install(_install_args(mode="sidecar"))
    aegis_cli.cmd_install(_install_args(mode="local"))
    settings = isolated_install_phase5 / ".claude" / "settings.json"
    data = json.loads(settings.read_text())
    # Two PreToolUse entries (one per mode), still ONE Stop entry.
    assert len(data["hooks"]["PreToolUse"]) == 2
    assert len(data["hooks"]["Stop"]) == 1


def test_install_sidecar_then_local_modes_both_present(
    isolated_install_phase5: Path,
) -> None:
    """Different modes are tracked by independent markers — both can coexist."""
    aegis_cli.cmd_install(_install_args(mode="sidecar"))
    aegis_cli.cmd_install(_install_args(mode="local"))
    settings = isolated_install_phase5 / ".claude" / "settings.json"
    data = json.loads(settings.read_text())
    cmds = [
        h["command"]
        for entry in data["hooks"]["PreToolUse"]
        for h in entry["hooks"]
    ]
    assert any("aegis_hook.py" in c and "aegis_local_hook.py" not in c for c in cmds)
    assert any("aegis_local_hook.py" in c for c in cmds)


def test_install_refuses_when_plugin_manifest_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = tmp_path / ".claude" / "settings.json"
    monkeypatch.setattr(aegis_cli, "SETTINGS_PATH", settings)
    monkeypatch.setattr(aegis_cli, "PLUGIN_MANIFEST", tmp_path / "absent.json")
    rc = aegis_cli.cmd_install(_install_args(mode="sidecar"))
    assert rc == 1
    assert not settings.exists()


def test_install_refuses_when_plugin_manifest_malformed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = tmp_path / ".claude" / "settings.json"
    bad_manifest = tmp_path / "plugin.json"
    bad_manifest.write_text("garbage{")
    monkeypatch.setattr(aegis_cli, "SETTINGS_PATH", settings)
    monkeypatch.setattr(aegis_cli, "PLUGIN_MANIFEST", bad_manifest)
    rc = aegis_cli.cmd_install(_install_args(mode="sidecar"))
    assert rc == 1
    assert not settings.exists()


def test_install_local_mode_idempotent_within_mode(
    isolated_install_phase5: Path,
) -> None:
    aegis_cli.cmd_install(_install_args(mode="local"))
    aegis_cli.cmd_install(_install_args(mode="local"))
    settings = isolated_install_phase5 / ".claude" / "settings.json"
    data = json.loads(settings.read_text())
    locals_ = [
        h
        for entry in data["hooks"]["PreToolUse"]
        for h in entry["hooks"]
        if "aegis_local_hook.py" in h["command"]
    ]
    assert len(locals_) == 1


# ---- Phase 5: helpers ---------------------------------------------------


def test_build_pretool_command_sidecar() -> None:
    cmd = aegis_cli._build_pretool_command("sidecar")
    assert "aegis_hook.py" in cmd
    assert "AEGIS_POLICY_DIR" not in cmd
    assert "PYTHONPATH" not in cmd


def test_build_pretool_command_local_includes_env() -> None:
    cmd = aegis_cli._build_pretool_command("local")
    assert "aegis_local_hook.py" in cmd
    assert "AEGIS_POLICY_DIR=" in cmd
    assert "PYTHONPATH=" in cmd
    # Solo Free contract: local mode must default to dummy providers so
    # the hook does not require an OpenAI key.
    assert "AEGIS_EMBEDDING_PROVIDER=dummy" in cmd
    assert "AEGIS_JUDGE_PROVIDER=dummy" in cmd


def test_pretool_hook_marker_distinguishes_modes() -> None:
    side = aegis_cli._pretool_hook_marker("sidecar")
    local = aegis_cli._pretool_hook_marker("local")
    assert side != local
    assert "aegis_hook.py" in side and "aegis_local_hook.py" not in side
    assert "aegis_local_hook.py" in local


# ---- v2.1.4: aegis report -----------------------------------------------


def _audit_args(  # type: ignore[no-untyped-def]
    audit: str | None = None,
    since: str | None = None,
    verbose: bool = False,
):
    import argparse

    return argparse.Namespace(audit=audit, since=since, verbose=verbose)


def _write_audit(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")


def test_report_no_audit_returns_one(tmp_path: Path) -> None:
    rc = aegis_cli.cmd_report(_audit_args(audit=str(tmp_path / "absent.jsonl")))
    assert rc == 1


def test_report_counts_decisions(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    audit = tmp_path / "audit.jsonl"
    _write_audit(
        audit,
        [
            {"ts_ns": 1, "decision": "ALLOW", "reason": "all firewall steps passed"},
            {"ts_ns": 2, "decision": "ALLOW", "reason": "redundant read-only Read"},
            {"ts_ns": 3, "decision": "BLOCK", "reason": "rule:git_destructive"},
            {"ts_ns": 4, "decision": "BLOCK", "reason": "instruction_drift: CLAUDE.md mutated"},
            {"ts_ns": 5, "decision": "REQUIRE_APPROVAL", "reason": "loop (3× seen)"},
            {"ts_ns": 6, "decision": "REQUIRE_APPROVAL", "reason": "rule:persona_drift"},
        ],
    )
    rc = aegis_cli.cmd_report(_audit_args(audit=str(audit)))
    assert rc == 0
    out = capsys.readouterr().out
    assert "2 safe tool calls" in out
    assert "2 high-risk actions" in out
    assert "1 destructive commands" in out
    assert "1 poisoned-instruction sources" in out
    assert "1 redundant calls" in out
    assert "1 potential loops" in out
    assert str(audit) in out


def test_report_skips_blank_and_malformed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    audit = tmp_path / "audit.jsonl"
    audit.write_text(
        '\n{"decision": "ALLOW", "reason": "ok"}\nnot json\n'
        '{"decision": "BLOCK", "reason": "rule:rm"}\n\n'
    )
    rc = aegis_cli.cmd_report(_audit_args(audit=str(audit)))
    assert rc == 0
    out = capsys.readouterr().out
    assert "1 safe" in out
    assert "1 destructive" in out


def test_report_with_since_window_filters(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    audit = tmp_path / "audit.jsonl"
    now_ns = int(time.time() * 1_000_000_000)
    old_ns = now_ns - 7 * 24 * 3600 * 1_000_000_000  # 7 days ago
    _write_audit(
        audit,
        [
            {"ts_ns": old_ns, "decision": "ALLOW", "reason": "old"},
            {"ts_ns": now_ns, "decision": "ALLOW", "reason": "fresh"},
        ],
    )
    rc = aegis_cli.cmd_report(_audit_args(audit=str(audit), since="24h"))
    assert rc == 0
    out = capsys.readouterr().out
    assert "1 safe" in out  # only the fresh one


def test_report_verbose_shows_top_reasons(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    audit = tmp_path / "audit.jsonl"
    _write_audit(
        audit,
        [
            {"ts_ns": i, "decision": "BLOCK", "reason": "rule:git_destructive"}
            for i in range(5)
        ]
        + [
            {"ts_ns": 100, "decision": "ALLOW", "reason": "all firewall steps passed"},
        ],
    )
    rc = aegis_cli.cmd_report(_audit_args(audit=str(audit), verbose=True))
    assert rc == 0
    out = capsys.readouterr().out
    assert "Top reasons" in out
    assert "rule:git_destructive" in out


def test_report_subcommand_argparse() -> None:
    parser = aegis_cli.build_parser()
    args = parser.parse_args(["report", "--audit", "/tmp/a.jsonl", "--since", "24h", "-v"])
    assert args.fn.__name__ == "cmd_report"
    assert args.audit == "/tmp/a.jsonl"
    assert args.since == "24h"
    assert args.verbose is True


def test_report_subcommand_default_args() -> None:
    parser = aegis_cli.build_parser()
    args = parser.parse_args(["report"])
    assert args.fn.__name__ == "cmd_report"
    assert args.audit is None
    assert args.since is None
    assert args.verbose is False


# ---- v2.2: aegis baseline ----------------------------------------------


def _baseline_args(  # type: ignore[no-untyped-def]
    action: str,
    *,
    root: str | None = None,
    baseline: str | None = None,
    force: bool = False,
):
    import argparse

    return argparse.Namespace(
        action=action, root=root, baseline=baseline, force=force
    )


def _make_repo(root: Path) -> None:
    (root / "CLAUDE.md").write_text("# rules\n")
    (root / "AGENTS.md").write_text("agents.\n")


def test_baseline_init_writes_manifest(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _make_repo(tmp_path)
    out = tmp_path / "m.json"
    rc = aegis_cli.cmd_baseline(
        _baseline_args("init", root=str(tmp_path), baseline=str(out))
    )
    assert rc == 0
    assert out.exists()
    data = json.loads(out.read_text())
    assert "CLAUDE.md" in data["files"]
    assert "AGENTS.md" in data["files"]
    out_text = capsys.readouterr().out
    assert "instruction baseline written" in out_text


def test_baseline_init_refuses_overwrite_without_force(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _make_repo(tmp_path)
    out = tmp_path / "m.json"
    out.write_text("{}")
    rc = aegis_cli.cmd_baseline(
        _baseline_args("init", root=str(tmp_path), baseline=str(out))
    )
    assert rc == 1
    err_text = capsys.readouterr().out
    assert "already exists" in err_text


def test_baseline_init_with_force_overwrites(tmp_path: Path) -> None:
    _make_repo(tmp_path)
    out = tmp_path / "m.json"
    out.write_text("{}")
    rc = aegis_cli.cmd_baseline(
        _baseline_args("init", root=str(tmp_path), baseline=str(out), force=True)
    )
    assert rc == 0
    data = json.loads(out.read_text())
    assert "CLAUDE.md" in data["files"]


def test_baseline_status_reports_clean(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _make_repo(tmp_path)
    out = tmp_path / "m.json"
    aegis_cli.cmd_baseline(
        _baseline_args("init", root=str(tmp_path), baseline=str(out))
    )
    capsys.readouterr()  # consume init output
    rc = aegis_cli.cmd_baseline(
        _baseline_args("status", root=str(tmp_path), baseline=str(out))
    )
    assert rc == 0
    out_text = capsys.readouterr().out
    assert "baseline intact" in out_text


def test_baseline_status_detects_drift(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _make_repo(tmp_path)
    out = tmp_path / "m.json"
    aegis_cli.cmd_baseline(
        _baseline_args("init", root=str(tmp_path), baseline=str(out))
    )
    (tmp_path / "CLAUDE.md").write_text("# poisoned\n")
    capsys.readouterr()
    rc = aegis_cli.cmd_baseline(
        _baseline_args("status", root=str(tmp_path), baseline=str(out))
    )
    assert rc == 1
    out_text = capsys.readouterr().out
    assert "drift detected" in out_text
    assert "CLAUDE.md" in out_text


def test_baseline_status_missing_baseline(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = aegis_cli.cmd_baseline(
        _baseline_args("status", root=str(tmp_path), baseline=str(tmp_path / "no.json"))
    )
    assert rc == 1
    err_text = capsys.readouterr().out
    assert "Run `aegis baseline init`" in err_text


def test_baseline_reattest_overwrites(tmp_path: Path) -> None:
    _make_repo(tmp_path)
    out = tmp_path / "m.json"
    aegis_cli.cmd_baseline(
        _baseline_args("init", root=str(tmp_path), baseline=str(out))
    )
    original = out.read_text()
    (tmp_path / "CLAUDE.md").write_text("# new content\n")
    rc = aegis_cli.cmd_baseline(
        _baseline_args("reattest", root=str(tmp_path), baseline=str(out))
    )
    assert rc == 0
    refreshed = out.read_text()
    assert refreshed != original


def test_baseline_subcommand_argparse() -> None:
    parser = aegis_cli.build_parser()
    args = parser.parse_args(
        ["baseline", "init", "--root", "/r", "--baseline", "/m.json", "--force"]
    )
    assert args.fn.__name__ == "cmd_baseline"
    assert args.action == "init"
    assert args.root == "/r"
    assert args.baseline == "/m.json"
    assert args.force is True


def test_baseline_subcommand_invalid_action_rejected() -> None:
    parser = aegis_cli.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["baseline", "wat"])
