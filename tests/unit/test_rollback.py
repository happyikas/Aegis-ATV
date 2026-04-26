"""Unit tests for src/aegis/rollback/ (D4).

Covers:
- _tools constants
- file strategy: existing-file backup, new-file unlink-on-restore
- shell strategy: pattern extraction + capture for redirect/rm/cp
- mcp strategy: mutating-verb detection + log-only capture
- snapshot orchestrator: capture→restore round-trip, list_snapshots, prune
- bulk_restore with since_iso (session_id branch tested separately via
  the session_invocations_lookup hook)

git strategy is largely process-mediated (calls real git); we test the
verb-detection branch and the no-HEAD short-circuit only.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from aegis.rollback import snapshot as snap_mod
from aegis.rollback._tools import FILE_TOOLS, FILE_WRITE_TOOLS, SHELL_TOOLS
from aegis.rollback.strategies import file as file_strat
from aegis.rollback.strategies import git as git_strat
from aegis.rollback.strategies import mcp as mcp_strat
from aegis.rollback.strategies import shell as shell_strat

# ---- _tools ---------------------------------------------------------------


def test_tool_sets_are_disjoint_kinds() -> None:
    assert "Bash" in SHELL_TOOLS
    assert "Write" in FILE_WRITE_TOOLS
    assert "Read" not in FILE_WRITE_TOOLS
    assert FILE_WRITE_TOOLS.issubset(FILE_TOOLS)


# ---- file strategy --------------------------------------------------------


def test_file_capture_existing_then_restore(tmp_path: Path) -> None:
    target = tmp_path / "original.txt"
    target.write_text("hello\n")
    snap_dir = tmp_path / "snap"

    cap = file_strat.capture("Write", {"file_path": str(target)}, snap_dir)
    assert cap == [f"file:{target}"]
    assert (snap_dir / f"file_{target.name}.bak").read_text() == "hello\n"

    # Mutate the file as a tool would, then restore.
    target.write_text("clobbered")
    out = file_strat.restore(
        {"path": str(target), "captured": cap}, snap_dir, allow_git=False
    )
    assert out["restored"] == [f"file:{target}"]
    assert target.read_text() == "hello\n"


def test_file_capture_new_file_then_unlink_on_restore(tmp_path: Path) -> None:
    target = tmp_path / "new.txt"
    snap_dir = tmp_path / "snap"
    cap = file_strat.capture("Write", {"file_path": str(target)}, snap_dir)
    assert cap == [f"new_file:{target}"]

    # Tool creates the file; restore should unlink it.
    target.write_text("created by tool")
    out = file_strat.restore({"captured": cap}, snap_dir, allow_git=False)
    assert out["restored"] == [f"unlink:{target}"]
    assert not target.exists()


def test_file_strategy_skips_non_write_tools(tmp_path: Path) -> None:
    target = tmp_path / "x.txt"
    target.write_text("y")
    cap = file_strat.capture("Bash", {"file_path": str(target)}, tmp_path / "snap")
    assert cap == []


# ---- shell strategy -------------------------------------------------------


def test_shell_extract_targets_redirect_rm_mv() -> None:
    targets = shell_strat._extract_targets("echo hi > /tmp/a; rm /tmp/b; mv c d")
    paths = [p for _, p in targets]
    assert "/tmp/a" in paths
    assert "/tmp/b" in paths
    assert "d" in paths


def test_shell_capture_existing_file_then_restore(tmp_path: Path) -> None:
    f = tmp_path / "f.txt"
    f.write_text("v1")
    snap_dir = tmp_path / "snap"
    cap = shell_strat.capture("Bash", {"command": f"echo new > {f}"}, snap_dir)
    assert any(c.startswith(f"shell-file:{f}") for c in cap)

    f.write_text("v2")
    out = shell_strat.restore({"captured": cap}, snap_dir, allow_git=False)
    assert any(r.startswith(f"shell-file:{f}") for r in out["restored"])
    assert f.read_text() == "v1"


def test_shell_capture_new_file_then_unlink(tmp_path: Path) -> None:
    f = tmp_path / "n.txt"
    snap_dir = tmp_path / "snap"
    cap = shell_strat.capture("Bash", {"command": f"echo hi > {f}"}, snap_dir)
    assert any(c.startswith(f"shell-new:{f}") for c in cap)

    f.write_text("created")
    out = shell_strat.restore({"captured": cap}, snap_dir, allow_git=False)
    assert any(r.startswith(f"shell-unlink:{f}") for r in out["restored"])


def test_shell_strategy_skips_non_shell_tools(tmp_path: Path) -> None:
    cap = shell_strat.capture("Read", {"command": "rm /tmp/x"}, tmp_path / "snap")
    assert cap == []


# ---- mcp strategy ---------------------------------------------------------


@pytest.mark.parametrize(
    ("tool", "expected"),
    [
        ("mcp__db__write", True),
        ("mcp__slack__send_message", True),
        ("mcp__github__create_issue", True),
        ("mcp__db__select", False),
        ("mcp__inspect__list", False),
        ("Bash", False),
        ("Write", False),
    ],
)
def test_mcp_looks_mutating(tool: str, expected: bool) -> None:
    assert mcp_strat._looks_mutating(tool) is expected


def test_mcp_capture_logs_call(tmp_path: Path) -> None:
    snap_dir = tmp_path / "snap"
    cap = mcp_strat.capture(
        "mcp__db__update", {"sql": "UPDATE t SET x=1"}, snap_dir
    )
    assert cap == ["mcp-log:mcp__db__update"]
    payload = json.loads((snap_dir / "mcp_call.json").read_text())
    assert payload["tool"] == "mcp__db__update"


def test_mcp_restore_marks_manual(tmp_path: Path) -> None:
    out = mcp_strat.restore(
        {"captured": ["mcp-log:mcp__db__update"]}, tmp_path / "snap", allow_git=False
    )
    assert out["restored"] == []
    assert any("manual rollback required" in s for s in out["skipped"])


# ---- git strategy ---------------------------------------------------------


def test_git_capture_skips_non_git_command(tmp_path: Path) -> None:
    cap = git_strat.capture("Bash", {"command": "ls -la"}, tmp_path / "snap")
    assert cap == []


def test_git_capture_skips_non_shell_tool(tmp_path: Path) -> None:
    cap = git_strat.capture("Write", {"command": "git commit"}, tmp_path / "snap")
    assert cap == []


def test_git_restore_skipped_without_allow_git(tmp_path: Path) -> None:
    out = git_strat.restore(
        {"captured": ["git:abc123", "git-branch:main"]},
        tmp_path / "snap",
        allow_git=False,
    )
    assert out["restored"] == []
    assert any("use --allow-git" in s for s in out["skipped"])


# ---- snapshot orchestrator -----------------------------------------------


@pytest.fixture
def snap_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    root = tmp_path / "snapshots"
    monkeypatch.setattr(snap_mod, "SNAP_ROOT", root)
    return root


def test_capture_returns_empty_when_no_strategy_matches(snap_root: Path) -> None:
    out = snap_mod.capture("inv-1", "UnknownTool", {})
    assert out == ""
    assert not snap_root.exists()


def test_capture_then_restore_file(snap_root: Path, tmp_path: Path) -> None:
    target = tmp_path / "data.txt"
    target.write_text("v1")
    snap_dir = snap_mod.capture(
        "inv-2", "Write", {"file_path": str(target)}
    )
    assert snap_dir
    assert (Path(snap_dir) / "meta.json").exists()

    target.write_text("clobbered")
    out = snap_mod.restore("inv-2")
    assert any(r.startswith("file:") for r in out["restored"])
    assert target.read_text() == "v1"


def test_restore_dry_run(snap_root: Path, tmp_path: Path) -> None:
    target = tmp_path / "f.txt"
    target.write_text("v1")
    snap_mod.capture("inv-3", "Write", {"file_path": str(target)})
    out = snap_mod.restore("inv-3", dry_run=True)
    assert out["dry_run"] is True
    assert out["would_restore"]


def test_restore_missing_invocation_raises(snap_root: Path) -> None:
    with pytest.raises(FileNotFoundError):
        snap_mod.restore("never-existed")


def test_list_snapshots_newest_first(snap_root: Path, tmp_path: Path) -> None:
    f1 = tmp_path / "a.txt"
    f1.write_text("a")
    f2 = tmp_path / "b.txt"
    f2.write_text("b")
    snap_mod.capture("inv-A", "Write", {"file_path": str(f1)})
    time.sleep(0.01)
    snap_mod.capture("inv-B", "Write", {"file_path": str(f2)})

    out = snap_mod.list_snapshots(limit=10)
    assert [s["invocation_id"] for s in out] == ["inv-B", "inv-A"]


def test_prune_removes_old_dirs(snap_root: Path, tmp_path: Path) -> None:
    target = tmp_path / "f.txt"
    target.write_text("x")
    snap_mod.capture("inv-old", "Write", {"file_path": str(target)})
    # Backdate the snapshot so prune sees it.
    old = snap_root / "inv-old"
    old_time = time.time() - 10_000
    import os

    os.utime(old, (old_time, old_time))

    n = snap_mod.prune(older_than_secs=3600)
    assert n == 1
    assert not old.exists()


def test_summarize_args_redacts_secrets() -> None:
    out = snap_mod._summarize_args(
        {"password": "p", "API_KEY": "k", "data": "x" * 300}
    )
    assert out["password"] == "<redacted>"
    assert out["API_KEY"] == "<redacted>"
    assert isinstance(out["data"], str)
    assert "...<truncated>" in out["data"]


# ---- bulk_restore --------------------------------------------------------


def test_bulk_restore_since_iso(snap_root: Path, tmp_path: Path) -> None:
    target = tmp_path / "z.txt"
    target.write_text("v1")
    snap_mod.capture("inv-Z", "Write", {"file_path": str(target)})
    target.write_text("v2")

    # since 1970 → matches everything
    out = snap_mod.bulk_restore(since_iso="1970-01-01", dry_run=True)
    assert out["candidates"] == 1
    assert out["dry_run"] is True


def test_bulk_restore_session_id_uses_lookup_hook(
    snap_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "s.txt"
    target.write_text("v1")
    snap_mod.capture("inv-S", "Write", {"file_path": str(target)})

    monkeypatch.setattr(
        snap_mod, "session_invocations_lookup", lambda _sid: {"inv-S"}
    )
    out = snap_mod.bulk_restore(session_id="any", dry_run=True)
    assert out["candidates"] == 1


def test_bulk_restore_session_id_default_lookup_returns_empty(
    snap_root: Path, tmp_path: Path
) -> None:
    target = tmp_path / "u.txt"
    target.write_text("v1")
    snap_mod.capture("inv-U", "Write", {"file_path": str(target)})
    out = snap_mod.bulk_restore(session_id="any", dry_run=True)
    assert out["candidates"] == 0
