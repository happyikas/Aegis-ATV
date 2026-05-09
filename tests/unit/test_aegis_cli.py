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


def test_cost_subcommands_resolve() -> None:
    """`aegis cost` was restructured into `summary` / `replay` actions
    in v2.5.x. Verify both resolve to the dispatcher and carry the
    expected default flags."""
    parser = aegis_cli.build_parser()

    summary_args = parser.parse_args(["cost", "summary"])
    assert summary_args.fn.__name__ == "cmd_cost"
    assert summary_args.action == "summary"
    assert summary_args.spike_threshold == 0.10

    replay_args = parser.parse_args(["cost", "replay", "/tmp/x.jsonl"])
    assert replay_args.fn.__name__ == "cmd_cost"
    assert replay_args.action == "replay"
    assert replay_args.budget == 1.0
    assert replay_args.hw_provider == "none"
    assert replay_args.multiplier == 3.0


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
    """Redirect SETTINGS_PATH, hook scripts, and plugin manifest into tmp dirs.

    cmd_install reads/writes module-level constants, so tests must
    monkeypatch them rather than $HOME.
    """
    settings = tmp_path / ".claude" / "settings.json"
    hook = tmp_path / "tools" / "aegis_hook.py"
    local_hook = tmp_path / "tools" / "aegis_local_hook.py"
    post_hook = tmp_path / "tools" / "hooks" / "post_tool.py"
    stop_hook = tmp_path / "tools" / "hooks" / "session_end.py"
    manifest = tmp_path / ".claude-plugin" / "plugin.json"
    hook.parent.mkdir(parents=True, exist_ok=True)
    post_hook.parent.mkdir(parents=True, exist_ok=True)
    manifest.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text("#!/usr/bin/env python3\nprint('stub hook')\n")
    local_hook.write_text("#!/usr/bin/env python3\nprint('local')\n")
    post_hook.write_text("#!/usr/bin/env python3\nprint('post')\n")
    stop_hook.write_text("#!/usr/bin/env python3\nprint('stop')\n")
    hook.chmod(0o755)
    local_hook.chmod(0o755)
    manifest.write_text(json.dumps({"name": "aegis-mvp", "version": "2.0.0"}))
    monkeypatch.setattr(aegis_cli, "SETTINGS_PATH", settings)
    monkeypatch.setattr(aegis_cli, "HOOK_SCRIPT", hook)
    monkeypatch.setattr(aegis_cli, "LOCAL_HOOK_SCRIPT", local_hook)
    monkeypatch.setattr(aegis_cli, "POST_HOOK_SCRIPT", post_hook)
    monkeypatch.setattr(aegis_cli, "STOP_HOOK_SCRIPT", stop_hook)
    monkeypatch.setattr(aegis_cli, "PLUGIN_MANIFEST", manifest)
    return tmp_path


def _install_args(  # type: ignore[no-untyped-def]
    force: bool = False, mode: str = "sidecar",
    judge: str = "dummy", embedding: str = "dummy",
    rescue: bool = False,
):
    """Build a minimal Namespace-like object for cmd_install."""
    import argparse

    return argparse.Namespace(
        force=force, mode=mode, judge=judge, embedding=embedding,
        rescue=rescue,
    )


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


def test_cmd_install_force_replaces_existing_aegis_entries(
    isolated_install: Path,
) -> None:
    """``--force`` must drop any prior Aegis-owned entries, then add fresh ones.

    Previously --force appended a second entry (creating duplicates that
    fired the firewall twice per tool call). The new semantics: evict
    then add, so re-installs after path changes don't accumulate dead
    command lines.
    """
    aegis_cli.cmd_install(_install_args())
    rc = aegis_cli.cmd_install(_install_args(force=True))
    assert rc == 0
    settings = isolated_install / ".claude" / "settings.json"
    data = json.loads(settings.read_text())
    # Exactly one of each — not duplicated.
    assert len(data["hooks"]["PreToolUse"]) == 1
    assert len(data["hooks"]["PostToolUse"]) == 1
    assert len(data["hooks"]["Stop"]) == 1


def test_cmd_install_force_preserves_non_aegis_hooks(
    isolated_install: Path,
) -> None:
    """``--force`` must not delete user-owned hooks (e.g. prettier, gitleaks)."""
    aegis_cli.cmd_install(_install_args())
    settings = isolated_install / ".claude" / "settings.json"
    data = json.loads(settings.read_text())
    # Inject a non-Aegis hook — it must survive --force.
    data["hooks"]["PreToolUse"].append({
        "matcher": "Edit",
        "hooks": [{"type": "command", "command": "/usr/local/bin/prettier"}],
    })
    settings.write_text(json.dumps(data))
    aegis_cli.cmd_install(_install_args(force=True))
    final = json.loads(settings.read_text())
    cmds = [
        h["command"]
        for e in final["hooks"]["PreToolUse"]
        for h in e["hooks"]
    ]
    assert any("/usr/local/bin/prettier" in c for c in cmds), \
        "third-party hook was wrongly evicted by --force"


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


# ---- P0 (post #19): PostToolUse + --judge + venv python ------------------


def test_install_registers_posttooluse(isolated_install: Path) -> None:
    """`aegis install` must register PostToolUse so ATMU 2PC phase 2 closes.

    Before this fix the hook chain only had PreToolUse + Stop, so every
    intent stayed open and the perf-feedback EWMA never updated.
    """
    rc = aegis_cli.cmd_install(_install_args(mode="local"))
    assert rc == 0
    data = json.loads((isolated_install / ".claude" / "settings.json").read_text())
    assert "PostToolUse" in data["hooks"]
    cmds = [
        h["command"]
        for e in data["hooks"]["PostToolUse"]
        for h in e["hooks"]
    ]
    assert any("post_tool.py" in c for c in cmds)


def test_install_default_judge_is_none_resolved_from_profile() -> None:
    """PR-A: --judge default became None at the parser level; resolution
    happens in cmd_install via _resolve_profile (free profile → dummy)."""
    args = aegis_cli.build_parser().parse_args(["install"])
    assert args.judge is None
    assert args.profile == "free"
    resolved = aegis_cli._resolve_profile(
        args.profile, judge_arg=args.judge, embedding_arg=args.embedding,
    )
    assert resolved["judge"] == "dummy"
    assert resolved["embedding"] == "dummy"


def test_install_judge_hybrid_accepted() -> None:
    args = aegis_cli.build_parser().parse_args(
        ["install", "--mode", "local", "--judge", "hybrid"]
    )
    assert args.judge == "hybrid"


def test_install_judge_invalid_rejected() -> None:
    with pytest.raises(SystemExit):
        aegis_cli.build_parser().parse_args(
            ["install", "--mode", "local", "--judge", "haiku"]
        )


def test_install_local_with_hybrid_writes_env_var(
    isolated_install: Path,
) -> None:
    aegis_cli.cmd_install(_install_args(mode="local", judge="hybrid"))
    data = json.loads((isolated_install / ".claude" / "settings.json").read_text())
    cmd = data["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    assert "AEGIS_JUDGE_PROVIDER=hybrid" in cmd


def test_install_local_with_dummy_writes_env_var(
    isolated_install: Path,
) -> None:
    aegis_cli.cmd_install(_install_args(mode="local", judge="dummy"))
    data = json.loads((isolated_install / ".claude" / "settings.json").read_text())
    cmd = data["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    assert "AEGIS_JUDGE_PROVIDER=dummy" in cmd


def test_build_pretool_command_invalid_judge_raises() -> None:
    with pytest.raises(ValueError, match="judge"):
        aegis_cli._build_pretool_command("local", judge="claude-opus")


def test_hook_python_executable_prefers_venv(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """If <repo>/.venv/bin/python exists, it wins over sys.executable."""
    fake_venv_py = tmp_path / ".venv" / "bin" / "python"
    fake_venv_py.parent.mkdir(parents=True)
    fake_venv_py.touch()
    monkeypatch.setattr(aegis_cli, "PROJECT_ROOT", tmp_path)
    assert aegis_cli._hook_python_executable() == str(fake_venv_py)


def test_hook_python_executable_falls_back_when_no_venv(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setattr(aegis_cli, "PROJECT_ROOT", tmp_path)
    out = aegis_cli._hook_python_executable()
    # Falls back to sys.executable (the running interpreter).
    assert out == sys.executable or out == "python3"


def test_install_command_uses_venv_python(isolated_install: Path) -> None:
    """The hook command in settings.json must NOT use bare `python3`.

    Bare `python3` on macOS is system Python (no numpy / pydantic) — the
    hook would crash on first call. Real fix: use repo's .venv/bin/python.
    """
    aegis_cli.cmd_install(_install_args(mode="local"))
    data = json.loads((isolated_install / ".claude" / "settings.json").read_text())
    cmd = data["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    # Must contain a path-qualified python (either /…/.venv/bin/python or
    # an absolute interpreter from sys.executable). Bare ` python3 ` (with
    # spaces) would mean the user's shell has to resolve it.
    assert " python3 " not in f" {cmd} ", \
        f"hook command uses bare python3 — will crash on macOS:\n{cmd}"


def test_drop_aegis_entries_removes_only_aegis_owned() -> None:
    hooks = {
        "PreToolUse": [
            {"matcher": "*", "hooks": [{"command": "x aegis_local_hook.py y"}]},
            {"matcher": "*", "hooks": [{"command": "/usr/local/bin/prettier"}]},
        ],
        "PostToolUse": [
            {"matcher": "*", "hooks": [{"command": "y tools/hooks/post_tool.py"}]},
        ],
        "Stop": [
            {"hooks": [{"command": "z tools/hooks/session_end.py"}]},
        ],
    }
    n = aegis_cli._drop_aegis_entries(hooks)
    assert n == 3
    # Only the prettier entry survives.
    assert len(hooks["PreToolUse"]) == 1
    assert "prettier" in hooks["PreToolUse"][0]["hooks"][0]["command"]
    assert hooks["PostToolUse"] == []
    assert hooks["Stop"] == []


def test_install_idempotent_posttooluse_too(isolated_install: Path) -> None:
    """Re-running install without --force must not duplicate PostToolUse."""
    aegis_cli.cmd_install(_install_args(mode="local"))
    aegis_cli.cmd_install(_install_args(mode="local"))
    data = json.loads((isolated_install / ".claude" / "settings.json").read_text())
    assert len(data["hooks"]["PostToolUse"]) == 1


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


def test_build_pretool_command_local_anti_self_dos_defaults() -> None:
    """PR #101: local-mode install must not lock the user out via the
    cost gate or REQUIRE_APPROVAL escalation. Verified by checking
    that the emitted hook command pre-pends both safety overrides."""
    cmd = aegis_cli._build_pretool_command("local")
    # REQUIRE_APPROVAL surfaces as informational, doesn't block tool calls
    assert "AEGIS_APPROVE_AS_BLOCK=0" in cmd
    # Cost gate disabled (wildcard-large budget)
    assert "AEGIS_TOKEN_BUDGET=99999999" in cmd


def test_build_pretool_command_sidecar_keeps_strict_defaults() -> None:
    """Sidecar (Enterprise) mode must NOT prepend the Personal anti-
    self-DoS overrides — Enterprise admins set per-tenant budgets via
    the budget store, and REQUIRE_APPROVAL → BLOCK is the safe default
    when an approval queue is configured."""
    cmd = aegis_cli._build_pretool_command("sidecar")
    assert "AEGIS_APPROVE_AS_BLOCK=0" not in cmd
    assert "AEGIS_TOKEN_BUDGET=99999999" not in cmd


# ── PR #101 — aegis install --rescue ──────────────────────────────────


def test_rescue_restores_most_recent_backup(
    isolated_install: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """--rescue copies the newest settings.json.bak.<ts> to
    settings.json. Useful when a previous install leaves the user
    locked out by the cost gate."""
    settings = isolated_install / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    # Simulate a broken live settings.json
    settings.write_text('{"hooks": {"PreToolUse": [{"broken": true}]}}')
    # Simulate two backups, newer one carries known content
    older = settings.parent / "settings.json.bak.1000000"
    newer = settings.parent / "settings.json.bak.2000000"
    older.write_text('{"old": "content"}')
    newer.write_text('{"new": "content"}')
    # Force mtime ordering so test isn't flaky on fast filesystems
    import os
    os.utime(older, (1000000, 1000000))
    os.utime(newer, (2000000, 2000000))

    rc = aegis_cli.cmd_install(_install_args(rescue=True))
    assert rc == 0
    # Latest backup wins
    assert json.loads(settings.read_text()) == {"new": "content"}
    out = capsys.readouterr().out
    assert "restored" in out
    assert "settings.json.bak.2000000" in out


def test_rescue_with_no_backups_returns_error(
    isolated_install: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """When no bak files exist, --rescue must return non-zero with a
    clear message rather than silently doing nothing."""
    rc = aegis_cli.cmd_install(_install_args(rescue=True))
    assert rc == 1
    err = capsys.readouterr().err
    assert "no settings.json.bak" in err


def test_rescue_skips_normal_install_path(
    isolated_install: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """When --rescue is set, the install pipeline (manifest validation,
    hook script registration, etc.) must NOT run. Verified by checking
    that no settings.json is created when --rescue runs without backups
    (the rescue code path returns before touching anything)."""
    settings = isolated_install / ".claude" / "settings.json"
    assert not settings.exists()
    aegis_cli.cmd_install(_install_args(rescue=True))
    # No backups + rescue → no install side-effects either
    assert not settings.exists()


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


# ---- v2.2.1: dual-schema audit reader -----------------------------------


def test_extract_audit_fields_local_schema() -> None:
    rec = {
        "ts_ns": 12345,
        "tool": "Bash",
        "aid": "session-abc",
        "decision": "BLOCK",
        "reason": "rule:git_destructive",
    }
    out = aegis_cli._extract_audit_fields(rec)
    assert out == {
        "decision": "BLOCK",
        "reason": "rule:git_destructive",
        "tool": "Bash",
        "ts_ns": 12345,
        "aid": "session-abc",
        "channel": "",
        "provider": "",
    }


def test_extract_audit_fields_sidecar_schema() -> None:
    """Sidecar JSONL nests decision + tool_name in payload.header."""
    rec = {
        "payload": {
            "header": {
                "decision": "REQUIRE_APPROVAL",
                "tool_name": "execute_shell",
                "timestamp_ns": 99999,
            },
            "signed_at_ns": 100000,
        },
        "atv_id": "abc",
    }
    out = aegis_cli._extract_audit_fields(rec)
    assert out["decision"] == "REQUIRE_APPROVAL"
    assert out["tool"] == "execute_shell"
    assert out["reason"] == ""  # sidecar JSONL has no reason
    assert out["ts_ns"] == 99999


def test_extract_audit_fields_top_level_decision_wins() -> None:
    """Local-schema fields win over the sidecar nested fallback."""
    rec = {
        "decision": "ALLOW",
        "tool": "Bash",
        "payload": {
            "header": {"decision": "BLOCK", "tool_name": "execute_shell"},
        },
    }
    out = aegis_cli._extract_audit_fields(rec)
    assert out["decision"] == "ALLOW"
    assert out["tool"] == "Bash"


def test_extract_audit_fields_falls_back_signed_at_ns() -> None:
    """If header.timestamp_ns is missing, payload.signed_at_ns is used."""
    rec = {
        "payload": {
            "header": {"decision": "BLOCK", "tool_name": "Bash"},
            "signed_at_ns": 7777,
        },
    }
    out = aegis_cli._extract_audit_fields(rec)
    assert out["ts_ns"] == 7777


def test_extract_audit_fields_robust_to_garbage() -> None:
    """Non-dict payload / non-numeric ts_ns must not raise."""
    rec_a = {"decision": "ALLOW", "ts_ns": "not a number"}
    out_a = aegis_cli._extract_audit_fields(rec_a)
    assert out_a["ts_ns"] == 0

    rec_b = {"payload": "not a dict"}
    out_b = aegis_cli._extract_audit_fields(rec_b)
    assert out_b["decision"] == ""
    assert out_b["tool"] == "?"


def test_report_against_sidecar_jsonl(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    audit = tmp_path / "sidecar.jsonl"
    _write_audit(
        audit,
        [
            {
                "payload": {
                    "header": {
                        "decision": "ALLOW",
                        "tool_name": "read_file",
                        "timestamp_ns": 1,
                    }
                }
            },
            {
                "payload": {
                    "header": {
                        "decision": "BLOCK",
                        "tool_name": "execute_shell",
                        "timestamp_ns": 2,
                    }
                }
            },
            {
                "payload": {
                    "header": {
                        "decision": "REQUIRE_APPROVAL",
                        "tool_name": "write_file",
                        "timestamp_ns": 3,
                    }
                }
            },
        ],
    )
    rc = aegis_cli.cmd_report(_audit_args(audit=str(audit), verbose=True))
    assert rc == 0
    out = capsys.readouterr().out
    # Counts are accurate even without reason text.
    assert "1 safe tool calls" in out
    assert "1 high-risk actions" in out
    assert "1 destructive commands" in out
    # Sidecar warning surfaced.
    assert "no `reason` text" in out
    # Top-reasons fallback uses tool name.
    assert "ALLOW read_file" in out
    assert "BLOCK execute_shell" in out
    assert "REQUIRE_APPROVAL write_file" in out


def test_report_mixed_schemas_in_one_log(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    audit = tmp_path / "mixed.jsonl"
    _write_audit(
        audit,
        [
            # local-schema line
            {
                "ts_ns": 1,
                "decision": "BLOCK",
                "reason": "rule:git_destructive",
                "tool": "Bash",
            },
            # sidecar-schema line
            {
                "payload": {
                    "header": {
                        "decision": "ALLOW",
                        "tool_name": "read_file",
                        "timestamp_ns": 2,
                    }
                }
            },
        ],
    )
    rc = aegis_cli.cmd_report(_audit_args(audit=str(audit)))
    assert rc == 0
    out = capsys.readouterr().out
    assert "1 safe tool calls" in out
    assert "1 destructive commands" in out
    # Mixed log → sidecar warning NOT shown (some records DID have reason).
    assert "no `reason` text" not in out


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


# ---- Solo Free real-sLLM: pull-model + --judge local-phi -----------------


def test_pull_model_subcommand_argparse_default() -> None:
    args = aegis_cli.build_parser().parse_args(["pull-model"])
    assert args.fn is aegis_cli.cmd_pull_model
    assert args.model == "llama-3.2-1b"
    assert args.list is False
    assert args.force is False


def test_pull_model_argparse_list() -> None:
    args = aegis_cli.build_parser().parse_args(["pull-model", "--list"])
    assert args.list is True


def test_pull_model_argparse_unknown_model_rejected() -> None:
    """Argparse uses ``choices=`` so unknown models are rejected at parse."""
    with pytest.raises(SystemExit):
        aegis_cli.build_parser().parse_args(["pull-model", "--model", "gpt-9000"])


def test_pull_model_list_returns_zero_and_prints_default(capsys) -> None:  # type: ignore[no-untyped-def]
    import argparse
    rc = aegis_cli.cmd_pull_model(argparse.Namespace(
        list=True, model="llama-3.2-1b", force=False,
    ))
    assert rc == 0
    out = capsys.readouterr().out
    assert "llama-3.2-1b" in out
    assert "(default)" in out
    # All three registered models must be in the table.
    assert "qwen-0.5b" in out
    assert "phi-3.5-mini" in out


def test_pull_model_skips_when_already_present(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys,  # type: ignore[no-untyped-def]
) -> None:
    """Idempotency: re-running pull-model with the file present is a no-op."""
    import argparse
    monkeypatch.setattr(aegis_cli, "MODELS_DIR", tmp_path)
    from aegis.judge.model_registry import default_model, model_target_path
    target = model_target_path(default_model(), tmp_path)
    target.write_bytes(b"\x00" * (1024 * 1024))  # 1MB placeholder
    rc = aegis_cli.cmd_pull_model(argparse.Namespace(
        list=False, model="llama-3.2-1b", force=False,
    ))
    assert rc == 0
    out = capsys.readouterr().out
    assert "already present" in out
    assert str(target) in out


def test_install_local_phi_judge_accepted() -> None:
    args = aegis_cli.build_parser().parse_args(
        ["install", "--mode", "local", "--judge", "local-phi"]
    )
    assert args.judge == "local-phi"


def test_install_local_phi_writes_model_path_env(
    isolated_install: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """``--judge local-phi`` must embed AEGIS_JUDGE_MODEL_PATH in the hook."""
    monkeypatch.setattr(aegis_cli, "MODELS_DIR", tmp_path / "models")
    aegis_cli.cmd_install(_install_args(mode="local", judge="local-phi"))
    settings = json.loads(
        (isolated_install / ".claude" / "settings.json").read_text()
    )
    cmd = settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    assert "AEGIS_JUDGE_PROVIDER=local-phi" in cmd
    assert "AEGIS_JUDGE_MODEL_PATH=" in cmd


def test_install_hybrid_judge_also_writes_model_path_env(
    isolated_install: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Hybrid cascade includes local-phi tier — same env var needed."""
    monkeypatch.setattr(aegis_cli, "MODELS_DIR", tmp_path / "models")
    aegis_cli.cmd_install(_install_args(mode="local", judge="hybrid"))
    settings = json.loads(
        (isolated_install / ".claude" / "settings.json").read_text()
    )
    cmd = settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    assert "AEGIS_JUDGE_MODEL_PATH=" in cmd


def test_install_dummy_does_not_write_model_path(
    isolated_install: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Dummy judge has no LLM — don't pollute settings.json with model path."""
    monkeypatch.setattr(aegis_cli, "MODELS_DIR", tmp_path / "models")
    aegis_cli.cmd_install(_install_args(mode="local", judge="dummy"))
    settings = json.loads(
        (isolated_install / ".claude" / "settings.json").read_text()
    )
    cmd = settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    assert "AEGIS_JUDGE_MODEL_PATH" not in cmd


def test_gguf_status_for_install_warns_when_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setattr(aegis_cli, "MODELS_DIR", tmp_path / "empty")
    ok, msg = aegis_cli._gguf_status_for_install("local-phi")
    assert ok is False
    assert "GGUF not found" in msg
    assert "pull-model" in msg


def test_check_llama_cpp_installed_truth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Whether llama-cpp is installed must be reported truthfully."""
    ok, msg = aegis_cli._check_llama_cpp_installed()
    # We can't assume llama-cpp is installed in CI, but the function
    # must not crash and must return a sensible (ok, msg) pair.
    assert isinstance(ok, bool)
    if not ok:
        assert "uv sync --extra local-llm" in msg


def test_human_size_formats() -> None:
    assert aegis_cli._human_size(0) == "0 B"
    assert aegis_cli._human_size(1023) == "1023 B"
    assert "KB" in aegis_cli._human_size(2048)
    assert "MB" in aegis_cli._human_size(5 * 1024 * 1024)
    assert "GB" in aegis_cli._human_size(2 * 1024**3)


# ---- Solo Free real embedding: --embedding flag + BGE wiring -----------


def test_install_default_embedding_resolves_to_dummy_via_profile() -> None:
    """PR-A: parser default is None now; profile resolver supplies
    the per-tier embedding. free profile → dummy."""
    args = aegis_cli.build_parser().parse_args(["install"])
    assert args.embedding is None
    resolved = aegis_cli._resolve_profile(
        args.profile, judge_arg=None, embedding_arg=None,
    )
    assert resolved["embedding"] == "dummy"


def test_install_embedding_bge_local_accepted() -> None:
    args = aegis_cli.build_parser().parse_args(
        ["install", "--mode", "local", "--embedding", "bge-local"]
    )
    assert args.embedding == "bge-local"


def test_install_embedding_invalid_rejected() -> None:
    with pytest.raises(SystemExit):
        aegis_cli.build_parser().parse_args(
            ["install", "--mode", "local", "--embedding", "openai-prod"]
        )


def test_install_local_with_bge_writes_embedding_path_env(
    isolated_install: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """``--embedding bge-local`` must embed AEGIS_EMBEDDING_MODEL_PATH +
    AEGIS_EMBEDDING_PROVIDER=bge-local in the hook command."""
    monkeypatch.setattr(aegis_cli, "MODELS_DIR", tmp_path / "models")
    aegis_cli.cmd_install(_install_args(
        mode="local", judge="dummy", embedding="bge-local",
    ))
    settings = json.loads(
        (isolated_install / ".claude" / "settings.json").read_text()
    )
    cmd = settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    assert "AEGIS_EMBEDDING_PROVIDER=bge-local" in cmd
    assert "AEGIS_EMBEDDING_MODEL_PATH=" in cmd
    assert "bge-base-en" in cmd  # the default embedding model filename


def test_install_dummy_embedding_does_not_write_path(
    isolated_install: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Default ``--embedding dummy`` keeps the hook free of model-path env."""
    monkeypatch.setattr(aegis_cli, "MODELS_DIR", tmp_path / "models")
    aegis_cli.cmd_install(_install_args(mode="local", judge="dummy"))
    settings = json.loads(
        (isolated_install / ".claude" / "settings.json").read_text()
    )
    cmd = settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    assert "AEGIS_EMBEDDING_PROVIDER=dummy" in cmd
    assert "AEGIS_EMBEDDING_MODEL_PATH" not in cmd


def test_build_pretool_command_invalid_embedding_raises() -> None:
    with pytest.raises(ValueError, match="embedding"):
        aegis_cli._build_pretool_command(
            "local", judge="dummy", embedding="vertex-ai",
        )


def test_install_hybrid_with_bge_writes_both_model_paths(
    isolated_install: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Hybrid + bge-local = both judge AND embedding GGUF paths embedded."""
    monkeypatch.setattr(aegis_cli, "MODELS_DIR", tmp_path / "models")
    aegis_cli.cmd_install(_install_args(
        mode="local", judge="hybrid", embedding="bge-local",
    ))
    settings = json.loads(
        (isolated_install / ".claude" / "settings.json").read_text()
    )
    cmd = settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    assert "AEGIS_JUDGE_MODEL_PATH=" in cmd
    assert "AEGIS_EMBEDDING_MODEL_PATH=" in cmd
    assert "AEGIS_JUDGE_PROVIDER=hybrid" in cmd
    assert "AEGIS_EMBEDDING_PROVIDER=bge-local" in cmd


def test_bge_status_for_install_warns_when_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setattr(aegis_cli, "MODELS_DIR", tmp_path / "empty")
    ok, msg = aegis_cli._bge_status_for_install("bge-local")
    assert ok is False
    assert "Embedding GGUF not found" in msg
    assert "pull-model" in msg
    assert "bge-base-en" in msg


def test_pull_model_argparse_accepts_bge_models() -> None:
    args = aegis_cli.build_parser().parse_args(
        ["pull-model", "--model", "bge-base-en"]
    )
    assert args.model == "bge-base-en"
    args = aegis_cli.build_parser().parse_args(
        ["pull-model", "--model", "bge-small-en"]
    )
    assert args.model == "bge-small-en"


# ── PR-A: --profile {free,pro,cloud} ────────────────────────────────


def test_profile_default_is_free() -> None:
    args = aegis_cli.build_parser().parse_args(["install"])
    assert args.profile == "free"


def test_profile_choices_validated() -> None:
    import pytest

    parser = aegis_cli.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["install", "--profile", "bogus"])


def test_resolve_profile_free_keeps_dummy() -> None:
    r = aegis_cli._resolve_profile(
        "free", judge_arg=None, embedding_arg=None,
    )
    assert r["judge"] == "dummy"
    assert r["embedding"] == "dummy"
    assert r["advisor_enabled"] is False
    assert r["auto_pull"] == ()


def test_resolve_profile_pro_resolves_to_hybrid_bge_with_advisor() -> None:
    r = aegis_cli._resolve_profile(
        "pro", judge_arg=None, embedding_arg=None,
    )
    assert r["judge"] == "hybrid"
    assert r["embedding"] == "bge-local"
    assert r["advisor_enabled"] is True
    assert r["advisor_provider"] == "heuristic"
    assert "bge-base-en" in r["auto_pull"]
    assert "llama-3.2-1b" in r["auto_pull"]


def test_resolve_profile_cloud_uses_haiku_advisor() -> None:
    r = aegis_cli._resolve_profile(
        "cloud", judge_arg=None, embedding_arg=None,
    )
    assert r["judge"] == "hybrid"
    assert r["embedding"] == "bge-local"
    assert r["advisor_enabled"] is True
    assert r["advisor_provider"] == "haiku"
    # Cloud reuses the local-tier model files for fallback when the
    # API key is missing or Anthropic is unreachable.
    assert "bge-base-en" in r["auto_pull"]


def test_resolve_profile_explicit_judge_overrides_baseline() -> None:
    """User can pin a tier even when picking a richer profile."""
    r = aegis_cli._resolve_profile(
        "pro", judge_arg="dummy", embedding_arg=None,
    )
    assert r["judge"] == "dummy"
    assert r["embedding"] == "bge-local"  # profile baseline still applies


def test_resolve_profile_explicit_embedding_overrides_baseline() -> None:
    r = aegis_cli._resolve_profile(
        "pro", judge_arg=None, embedding_arg="dummy",
    )
    assert r["embedding"] == "dummy"
    assert r["judge"] == "hybrid"  # profile baseline still applies


def test_resolve_profile_rejects_unknown_name() -> None:
    import pytest

    with pytest.raises(ValueError, match="--profile must be one of"):
        aegis_cli._resolve_profile(
            "nonsense", judge_arg=None, embedding_arg=None,
        )


def test_build_pretool_command_advisor_off_by_default() -> None:
    """free profile → no AEGIS_ADVISOR_ENABLED in the hook command."""
    cmd = aegis_cli._build_pretool_command(
        "local", judge="dummy", embedding="dummy", advisor_enabled=False,
    )
    assert "AEGIS_ADVISOR_ENABLED" not in cmd


def test_build_pretool_command_advisor_on_for_pro() -> None:
    cmd = aegis_cli._build_pretool_command(
        "local", judge="hybrid", embedding="bge-local",
        advisor_enabled=True, advisor_provider="heuristic",
    )
    assert "AEGIS_ADVISOR_ENABLED=1" in cmd
    assert "AEGIS_ADVISOR_PROVIDER=heuristic" in cmd


def test_build_pretool_command_advisor_haiku_for_cloud() -> None:
    cmd = aegis_cli._build_pretool_command(
        "local", judge="hybrid", embedding="bge-local",
        advisor_enabled=True, advisor_provider="haiku",
    )
    assert "AEGIS_ADVISOR_ENABLED=1" in cmd
    assert "AEGIS_ADVISOR_PROVIDER=haiku" in cmd


def test_auto_pull_models_no_op_on_empty_tuple() -> None:
    """free profile passes () so install doesn't trigger any download."""
    rc = aegis_cli._auto_pull_models(())
    assert rc == 0


# ── PR-C: aegis forensic <selector> ─────────────────────────────────


def _write_audit_record(
    fh, *, ts_ns: int, decision: str, tool: str = "Bash",
    aid: str = "session-abc", trace_id: str = "trace-1",
    reason: str = "stub", latency_ms: float = 1.0,
    explain: dict | None = None,
) -> None:
    rec = {
        "ts_ns": ts_ns,
        "tool": tool,
        "aid": aid,
        "decision": decision,
        "reason": reason,
        "trace_id": trace_id,
        "latency_ms": latency_ms,
        "mode": "local",
        "explain": explain or {},
        "prev_hash": "0" * 64,
        "this_hash": "f" * 64,
    }
    fh.write(json.dumps(rec) + "\n")


def _make_audit_fixture(tmp_path: Path) -> Path:
    """Mixed-decision, two-AID audit log for forensic tests."""
    audit = tmp_path / "audit.jsonl"
    base = 1_700_000_000_000_000_000  # any plausible ns timestamp
    with audit.open("w") as fh:
        # Session A — 3 records, mixed decisions
        _write_audit_record(
            fh, ts_ns=base + 0, decision="ALLOW", tool="Read",
            aid="session-A", trace_id="trA-001", latency_ms=12.0,
        )
        _write_audit_record(
            fh, ts_ns=base + 1_000_000_000,
            decision="REQUIRE_APPROVAL", tool="Edit",
            aid="session-A", trace_id="trA-002", latency_ms=145.0,
            reason="m13 score=0.55",
            explain={"step_timings_us": {"step340_policy.run": 27432}},
        )
        _write_audit_record(
            fh, ts_ns=base + 2_000_000_000,
            decision="BLOCK", tool="Bash",
            aid="session-A", trace_id="trA-003", latency_ms=240.0,
            reason="dangerous pattern",
            explain={"advisor_gate": {"invoked": True, "reason": "BLOCK"}},
        )
        # Session B — different AID, should be filtered out by selector
        _write_audit_record(
            fh, ts_ns=base + 3_000_000_000,
            decision="ALLOW", aid="session-B", trace_id="trB-001",
        )
    return audit


def _forensic_args(selector: str, **kw):
    import argparse
    return argparse.Namespace(
        selector=selector,
        trace=kw.get("trace"),
        audit=kw.get("audit"),
        since=kw.get("since"),
        limit=kw.get("limit", 0),
        json=kw.get("json", False),
    )


def test_forensic_no_audit_returns_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    rc = aegis_cli.cmd_forensic(_forensic_args(
        "anything", audit=str(tmp_path / "absent.jsonl"),
    ))
    assert rc == 1
    out = capsys.readouterr().out
    assert "no audit log" in out


def test_forensic_filters_by_aid(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    audit = _make_audit_fixture(tmp_path)
    rc = aegis_cli.cmd_forensic(_forensic_args(
        "session-A", audit=str(audit),
    ))
    assert rc == 0
    out = capsys.readouterr().out
    assert "session-A" in out
    assert "3 record(s)" in out
    # Session-B record must NOT bleed in.
    assert "trB-001" not in out


def test_forensic_last_resolves_to_most_recent_aid(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    audit = _make_audit_fixture(tmp_path)
    rc = aegis_cli.cmd_forensic(_forensic_args("last", audit=str(audit)))
    assert rc == 0
    out = capsys.readouterr().out
    # Most recent = session-B in our fixture (last appended).
    assert "session-B" in out


def test_forensic_trace_filter_narrows_to_one_call(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    audit = _make_audit_fixture(tmp_path)
    rc = aegis_cli.cmd_forensic(_forensic_args(
        "session-A", audit=str(audit), trace="trA-002",
    ))
    assert rc == 0
    out = capsys.readouterr().out
    assert "1 record(s)" in out
    assert "trA-002" in out
    assert "trA-001" not in out


def test_forensic_json_output_structure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    audit = _make_audit_fixture(tmp_path)
    rc = aegis_cli.cmd_forensic(_forensic_args(
        "session-A", audit=str(audit), json=True,
    ))
    assert rc == 0
    out = capsys.readouterr().out
    obj = json.loads(out)
    assert obj["selector"] == "session-A"
    assert obj["count"] == 3
    assert obj["first_ts_ns"] < obj["last_ts_ns"]
    assert len(obj["records"]) == 3
    assert all(r["aid"] == "session-A" for r in obj["records"])


def test_forensic_no_matching_records_returns_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    audit = _make_audit_fixture(tmp_path)
    rc = aegis_cli.cmd_forensic(_forensic_args(
        "session-zzz", audit=str(audit),
    ))
    assert rc == 1
    out = capsys.readouterr().out
    assert "no records match" in out


def test_forensic_limit_respects_recent_n(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    audit = _make_audit_fixture(tmp_path)
    rc = aegis_cli.cmd_forensic(_forensic_args(
        "session-A", audit=str(audit), limit=2,
    ))
    assert rc == 0
    out = capsys.readouterr().out
    assert "2 record(s)" in out
    # The two most-recent records of session-A → trA-002 + trA-003.
    assert "trA-002" in out
    assert "trA-003" in out


def test_forensic_renders_step_timings_and_advisor_gate(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """When the audit record has PR-D step_timings_us or PR-A
    advisor_gate, the timeline surfaces them as └─ sublines."""
    audit = _make_audit_fixture(tmp_path)
    rc = aegis_cli.cmd_forensic(_forensic_args(
        "session-A", audit=str(audit),
    ))
    assert rc == 0
    out = capsys.readouterr().out
    # PR-D timing line present for trA-002.
    assert "slow steps" in out
    assert "step340_policy" in out
    # PR-A advisor gate line present for trA-003.
    assert "advisor: invoked" in out


def test_forensic_subparser_dispatches_correctly() -> None:
    parser = aegis_cli.build_parser()
    args = parser.parse_args(["forensic", "session-A", "--limit", "5"])
    assert args.fn.__name__ == "cmd_forensic"
    assert args.selector == "session-A"
    assert args.limit == 5


# ── PR-F: aegis advise ──────────────────────────────────────────────


def _write_advice_record(
    fh, *, ts_ns: int, aid: str, advisor: str, priority: str = "medium",
    action: str = "stub action", reasoning: str = "stub reasoning",
    cited_signals=None, action_steps=None,
) -> None:
    """Write an audit record carrying explain.action_advice."""
    rec = {
        "ts_ns": ts_ns,
        "tool": "Bash",
        "aid": aid,
        "decision": "REQUIRE_APPROVAL",
        "reason": "stub",
        "trace_id": f"trace-{advisor}-{ts_ns}",
        "latency_ms": 50.0,
        "mode": "local",
        "explain": {
            "action_advice": {
                "decision": "REQUIRE_APPROVAL",
                "advisor_kind": "heuristic",
                "recommended_advisors": [
                    {
                        "advisor": advisor,
                        "priority": priority,
                        "action": action,
                        "reasoning": reasoning,
                        "cited_signals": cited_signals or [],
                        "action_steps": action_steps or [],
                    }
                ],
            },
        },
        "prev_hash": "0" * 64,
        "this_hash": "f" * 64,
    }
    fh.write(json.dumps(rec) + "\n")


def _make_advise_fixture(tmp_path: Path) -> Path:
    """Audit log with advisor recommendations spanning all 3 categories.

    Records appended in chronological order (matches how
    aegis_local_hook actually emits them): session-B first (older
    AID, before session-A started), then session-A in time order.
    ``selector="last"`` therefore resolves to session-A.
    """
    audit = tmp_path / "audit.jsonl"
    base_ts = int(time.time() * 1_000_000_000)  # now, in ns
    with audit.open("w") as fh:
        # session-B starts and ends earlier — must be appended FIRST
        # so the "last AID" rule (line order matches ts order) picks A.
        _write_advice_record(
            fh, ts_ns=base_ts - 1_000_000,
            aid="session-B",
            advisor="permission-escalator", priority="high",
            action="Different session noise",
        )
        # session-A: 3 cost recs (high)
        for i in range(3):
            _write_advice_record(
                fh, ts_ns=base_ts + i, aid="session-A",
                advisor="cost-optimizer", priority="high",
                action="Prune expensive turns",
                reasoning="budget burn 1.5x",
                cited_signals=["budget_used_ratio"],
                action_steps=[{
                    "verb": "prune-turns",
                    "parameters": {
                        "turn_indices_rel": [-3, -2, -1],
                        "saved_dollars_estimate": 0.42,
                    },
                    "expected_impact": "saves $0.42",
                    "confidence": 0.85,
                }],
            )
        # session-A: performance (medium)
        _write_advice_record(
            fh, ts_ns=base_ts + 10, aid="session-A",
            advisor="loop-breaker", priority="medium",
            action="Break the retry loop",
            reasoning="same call 3x in a row",
        )
        # session-A: security (low)
        _write_advice_record(
            fh, ts_ns=base_ts + 20, aid="session-A",
            advisor="security-reviewer", priority="low",
            action="Review destructive intent",
            reasoning="ambiguous fs op",
        )
    return audit


def _advise_args(**kw):
    import argparse
    return argparse.Namespace(
        selector=kw.get("selector", "last"),
        audit=kw.get("audit"),
        since=kw.get("since", "7d"),
        category=kw.get("category", "all"),
        priority=kw.get("priority", "all"),
        limit=kw.get("limit", 20),
        json=kw.get("json", False),
    )


def test_advise_no_audit_returns_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    rc = aegis_cli.cmd_advise(_advise_args(
        audit=str(tmp_path / "absent.jsonl"),
    ))
    assert rc == 1
    out = capsys.readouterr().out
    assert "no audit log" in out


def test_advise_aggregates_3_recs_across_categories(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    audit = _make_advise_fixture(tmp_path)
    rc = aegis_cli.cmd_advise(_advise_args(audit=str(audit)))
    assert rc == 0
    out = capsys.readouterr().out
    # 3 distinct recommendations should appear (cost + perf + sec); the
    # cost one should aggregate 3 occurrences into one ×3 row.
    assert "3 recommendation(s)" in out
    assert "cost-optimizer" in out
    assert "×3" in out  # dedupe count rendered
    assert "loop-breaker" in out
    assert "security-reviewer" in out


def test_advise_priority_ordering(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    audit = _make_advise_fixture(tmp_path)
    rc = aegis_cli.cmd_advise(_advise_args(audit=str(audit)))
    assert rc == 0
    out = capsys.readouterr().out
    high_pos = out.find("HIGH")
    medium_pos = out.find("MEDIUM")
    low_pos = out.find("LOW")
    # HIGH > MEDIUM > LOW in render order.
    assert 0 < high_pos < medium_pos < low_pos


def test_advise_category_filter_cost_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    audit = _make_advise_fixture(tmp_path)
    rc = aegis_cli.cmd_advise(_advise_args(
        audit=str(audit), category="cost",
    ))
    assert rc == 0
    out = capsys.readouterr().out
    assert "cost-optimizer" in out
    # Other categories filtered out.
    assert "loop-breaker" not in out
    assert "security-reviewer" not in out


def test_advise_priority_filter_high_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    audit = _make_advise_fixture(tmp_path)
    rc = aegis_cli.cmd_advise(_advise_args(
        audit=str(audit), priority="high",
    ))
    assert rc == 0
    out = capsys.readouterr().out
    assert "cost-optimizer" in out  # the only high-priority advisor
    assert "loop-breaker" not in out  # medium
    assert "security-reviewer" not in out  # low


def test_advise_selector_all_includes_other_session(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    audit = _make_advise_fixture(tmp_path)
    rc = aegis_cli.cmd_advise(_advise_args(
        audit=str(audit), selector="all",
    ))
    assert rc == 0
    out = capsys.readouterr().out
    # session-B's permission-escalator should now appear.
    assert "permission-escalator" in out


def test_advise_json_mode_structure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    audit = _make_advise_fixture(tmp_path)
    rc = aegis_cli.cmd_advise(_advise_args(
        audit=str(audit), json=True,
    ))
    assert rc == 0
    obj = json.loads(capsys.readouterr().out)
    assert obj["selector"] == "last"
    assert obj["scope_aid"] == "session-A"
    assert obj["records_walked"] >= 5
    assert obj["advisor_invocations"] == 5
    assert obj["category_counts"]["cost"] == 1
    assert obj["category_counts"]["performance"] == 1
    assert obj["category_counts"]["security"] == 1
    # Recommendations sorted high-first.
    assert obj["recommendations"][0]["priority"] == "high"
    assert obj["recommendations"][0]["count"] == 3


def test_advise_no_advice_records_returns_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """When the audit log has no action_advice records (free profile,
    advisor disabled), advise prints a friendly hint to upgrade."""
    audit = tmp_path / "audit.jsonl"
    base_ts = int(time.time() * 1_000_000_000)
    with audit.open("w") as fh:
        # records without action_advice
        rec = {
            "ts_ns": base_ts, "tool": "Bash", "aid": "session-A",
            "decision": "ALLOW", "reason": "ok", "trace_id": "tr-1",
            "latency_ms": 5.0, "mode": "local", "explain": {},
            "prev_hash": "0" * 64, "this_hash": "f" * 64,
        }
        fh.write(json.dumps(rec) + "\n")
    rc = aegis_cli.cmd_advise(_advise_args(audit=str(audit)))
    assert rc == 1
    out = capsys.readouterr().out
    assert "no advisor recommendations" in out
    assert "--profile pro" in out


def test_advise_subparser_dispatches() -> None:
    parser = aegis_cli.build_parser()
    args = parser.parse_args(["advise", "--category", "cost"])
    assert args.fn.__name__ == "cmd_advise"
    assert args.selector == "last"
    assert args.category == "cost"


# ── PR3: /aegis slash commands ──────────────────────────────────────


def test_resolve_aegis_cmd_uses_path_when_available(monkeypatch) -> None:
    """When `aegis` is on PATH, slash commands invoke the bare command."""
    import shutil
    monkeypatch.setattr(shutil, "which", lambda _: "/opt/homebrew/bin/aegis")
    assert aegis_cli._resolve_aegis_cmd() == "aegis"


def test_resolve_aegis_cmd_falls_back_to_uv_run(monkeypatch) -> None:
    """No `aegis` on PATH → uv run fallback for source clones."""
    import shutil
    monkeypatch.setattr(shutil, "which", lambda _: None)
    cmd = aegis_cli._resolve_aegis_cmd()
    assert cmd.startswith("uv run --project ")
    assert cmd.endswith(" aegis")


def test_install_slash_commands_writes_5_files(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        aegis_cli, "SLASH_COMMANDS_DST", tmp_path / "commands",
    )
    n, names = aegis_cli._install_slash_commands()
    assert n == 5
    assert set(names) == {
        "aegis-report.md",
        "aegis-verify.md",
        "aegis-advise.md",
        "aegis-forensic.md",
        "aegis-help.md",
    }
    # Marker prepended so uninstall can identify Aegis-owned files
    body = (tmp_path / "commands" / "aegis-report.md").read_text()
    assert aegis_cli.SLASH_COMMANDS_MARKER in body
    # AEGIS_CMD placeholder is fully substituted (no remnant)
    assert "{AEGIS_CMD}" not in body


def test_install_slash_commands_substitutes_resolved_aegis_cmd(
    tmp_path, monkeypatch,
) -> None:
    monkeypatch.setattr(
        aegis_cli, "SLASH_COMMANDS_DST", tmp_path / "commands",
    )
    import shutil
    monkeypatch.setattr(shutil, "which", lambda _: "/usr/local/bin/aegis")
    aegis_cli._install_slash_commands()
    body = (tmp_path / "commands" / "aegis-report.md").read_text()
    assert "aegis report --since 24h" in body
    # Source-clone fallback string should NOT appear when aegis is on PATH
    assert "uv run --project" not in body


def test_uninstall_slash_commands_only_removes_aegis_owned(
    tmp_path, monkeypatch,
) -> None:
    """User-owned files (without the marker) must survive uninstall —
    even if their filename happens to start with 'aegis-'."""
    cmds_dir = tmp_path / "commands"
    cmds_dir.mkdir(parents=True)
    monkeypatch.setattr(aegis_cli, "SLASH_COMMANDS_DST", cmds_dir)
    aegis_cli._install_slash_commands()  # 5 marked files

    # Plant a user-owned file that has 'aegis-' prefix but no marker
    (cmds_dir / "aegis-but-mine.md").write_text(
        "no marker — this is the user's own command\n"
    )
    (cmds_dir / "my-command.md").write_text("user's other command")

    n_removed, names = aegis_cli._uninstall_slash_commands()
    assert n_removed == 5
    # Aegis files all gone
    assert not (cmds_dir / "aegis-report.md").exists()
    assert not (cmds_dir / "aegis-help.md").exists()
    # User files preserved verbatim
    assert (cmds_dir / "aegis-but-mine.md").exists()
    assert (cmds_dir / "my-command.md").exists()


def test_uninstall_slash_commands_dry_run_writes_nothing(
    tmp_path, monkeypatch,
) -> None:
    cmds_dir = tmp_path / "commands"
    monkeypatch.setattr(aegis_cli, "SLASH_COMMANDS_DST", cmds_dir)
    aegis_cli._install_slash_commands()
    n_before = len(list(cmds_dir.glob("aegis-*.md")))
    n_removed, _ = aegis_cli._uninstall_slash_commands(dry_run=True)
    assert n_removed == 5
    n_after = len(list(cmds_dir.glob("aegis-*.md")))
    assert n_after == n_before  # nothing actually deleted


def test_install_no_commands_flag_skips_slash_install(
    isolated_install: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`aegis install --no-commands` patches settings.json but installs
    no slash command files."""
    import argparse
    cmds_dir = isolated_install / ".claude" / "commands"
    monkeypatch.setattr(aegis_cli, "SLASH_COMMANDS_DST", cmds_dir)
    rc = aegis_cli.cmd_install(argparse.Namespace(
        force=False, mode="local", judge=None, embedding=None,
        rescue=False, profile="free", no_commands=True,
    ))
    assert rc == 0
    # Settings still patched
    assert (isolated_install / ".claude" / "settings.json").exists()
    # Slash commands NOT installed (--no-commands)
    if cmds_dir.exists():
        assert not list(cmds_dir.glob("aegis-*.md"))


def test_install_default_includes_slash_commands(
    isolated_install: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`aegis install --mode local` (no --no-commands) installs slash
    commands by default."""
    import argparse
    cmds_dir = isolated_install / ".claude" / "commands"
    monkeypatch.setattr(aegis_cli, "SLASH_COMMANDS_DST", cmds_dir)
    rc = aegis_cli.cmd_install(argparse.Namespace(
        force=False, mode="local", judge=None, embedding=None,
        rescue=False, profile="free", no_commands=False,
    ))
    assert rc == 0
    assert cmds_dir.exists()
    # All 5 expected files present
    expected = {
        "aegis-report.md", "aegis-verify.md", "aegis-advise.md",
        "aegis-forensic.md", "aegis-help.md",
    }
    actual = {f.name for f in cmds_dir.glob("aegis-*.md")}
    assert actual == expected


def test_install_no_commands_arg_in_parser() -> None:
    parser = aegis_cli.build_parser()
    args = parser.parse_args(["install", "--no-commands"])
    assert args.no_commands is True
    args = parser.parse_args(["install"])
    assert args.no_commands is False


# ── PR4: SessionStart hook registration ─────────────────────────────


def test_install_registers_session_start_hook(
    isolated_install: Path,
) -> None:
    """`aegis install` should register the SessionStart hook into
    settings.json so the welcome message can fire on the user's next
    Claude Code launch."""
    import argparse
    rc = aegis_cli.cmd_install(argparse.Namespace(
        force=False, mode="local", judge=None, embedding=None,
        rescue=False, profile="free", no_commands=False,
    ))
    assert rc == 0
    settings = json.loads(
        (isolated_install / ".claude" / "settings.json").read_text()
    )
    sess_start = settings.get("hooks", {}).get("SessionStart", [])
    assert len(sess_start) == 1
    cmd = sess_start[0]["hooks"][0]["command"]
    assert "session_start.py" in cmd


def test_uninstall_removes_session_start_hook(
    isolated_install: Path,
) -> None:
    """Uninstall must drop SessionStart hook (alongside the others)."""
    import argparse
    aegis_cli.cmd_install(argparse.Namespace(
        force=False, mode="local", judge=None, embedding=None,
        rescue=False, profile="free", no_commands=False,
    ))
    settings_path = isolated_install / ".claude" / "settings.json"
    pre = json.loads(settings_path.read_text())
    assert pre["hooks"]["SessionStart"], "pre-condition: hook installed"

    rc = aegis_cli.cmd_uninstall(argparse.Namespace(
        dry_run=False, no_backup=False,
    ))
    assert rc == 0
    post = json.loads(settings_path.read_text())
    # SessionStart entries were Aegis-owned → all dropped.
    assert post["hooks"].get("SessionStart", []) == []


def test_session_start_script_is_aegis_owned() -> None:
    """The SessionStart script path must match the fingerprint set so
    --force can evict stale entries on re-install."""
    fp = "tools/hooks/session_start.py"
    assert any(
        fp in candidate
        for candidate in aegis_cli._AEGIS_HOOK_FINGERPRINTS
    )


# ── Three release tracks (--target flag) ─────────────────────────────


def test_install_target_arg_parses_three_choices() -> None:
    """--target accepts only the 3 sanctioned release tracks. Default
    is claude-code (current GA behaviour preserved)."""
    parser = aegis_cli.build_parser()

    args = parser.parse_args(["install"])
    assert args.target == "claude-code"

    args = parser.parse_args(["install", "--target", "claude-code"])
    assert args.target == "claude-code"

    args = parser.parse_args(["install", "--target", "openclaw-local"])
    assert args.target == "openclaw-local"

    args = parser.parse_args(["install", "--target", "openclaw-cloud"])
    assert args.target == "openclaw-cloud"


def test_install_target_invalid_choice_rejected() -> None:
    """Argparse must reject unknown release-track names."""
    import contextlib
    import io

    parser = aegis_cli.build_parser()
    with contextlib.redirect_stderr(io.StringIO()), pytest.raises(SystemExit):
        parser.parse_args(["install", "--target", "nonsense-track"])


def test_install_openclaw_local_stub_returns_zero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """openclaw-local is Preview — stub must exit 0 and print the
    track label + roadmap pointer + fallback to claude-code."""
    rc = aegis_cli._cmd_install_openclaw_stub("openclaw-local")
    assert rc == 0
    captured = capsys.readouterr()
    assert "OpenClaw + Local OSS LLM" in captured.out
    assert "Preview" in captured.out
    assert "docs/releases/OPENCLAW_LOCAL.ko.md" in captured.out
    # Falls back to recommending the GA track
    assert "claude-code" in captured.out


def test_install_openclaw_cloud_stub_returns_zero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """openclaw-cloud is Preview — same stub behaviour as Local."""
    rc = aegis_cli._cmd_install_openclaw_stub("openclaw-cloud")
    assert rc == 0
    captured = capsys.readouterr()
    assert "OpenClaw + Cloud LLM API" in captured.out
    assert "Preview" in captured.out
    assert "docs/releases/OPENCLAW_CLOUD.ko.md" in captured.out


def test_install_openclaw_target_routes_to_stub(
    isolated_install: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`aegis install --target openclaw-local` must NOT touch
    settings.json — it must short-circuit to the stub."""
    import argparse
    settings_path = isolated_install / ".claude" / "settings.json"
    # Pre-condition: no settings.json yet
    assert not settings_path.exists()

    rc = aegis_cli.cmd_install(argparse.Namespace(
        target="openclaw-local",
        force=False, mode="local", judge=None, embedding=None,
        rescue=False, profile="free", no_commands=False,
    ))
    assert rc == 0
    # Stub must NOT have created settings.json — that would be an
    # accidental Claude Code install under the Preview track.
    assert not settings_path.exists()
    captured = capsys.readouterr()
    assert "Preview" in captured.out


def test_install_default_target_is_claude_code(
    isolated_install: Path,
) -> None:
    """When --target is omitted, the install must behave exactly as
    before — i.e. patch settings.json (current GA Claude Code track).
    This guards against breaking existing users on upgrade."""
    import argparse
    rc = aegis_cli.cmd_install(argparse.Namespace(
        # No `target` attribute at all — getattr default kicks in
        force=False, mode="local", judge=None, embedding=None,
        rescue=False, profile="free", no_commands=False,
    ))
    assert rc == 0
    assert (isolated_install / ".claude" / "settings.json").exists()
