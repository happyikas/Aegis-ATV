"""Pluggable capture/restore strategies.

Each strategy returns a list of capture descriptors of the form
``"<kind>:<payload>"`` (e.g. ``"file:/etc/foo"``, ``"git:abc123"``,
``"mcp-log:mcp__db__write"``). :func:`dispatch_capture` runs every
strategy and concatenates the descriptors; :func:`dispatch_restore`
asks each strategy to reverse only the descriptors it owns.
"""

from __future__ import annotations

from pathlib import Path

from aegis.rollback.strategies.file import capture as file_capture
from aegis.rollback.strategies.file import restore as file_restore
from aegis.rollback.strategies.git import capture as git_capture
from aegis.rollback.strategies.git import restore as git_restore
from aegis.rollback.strategies.mcp import capture as mcp_capture
from aegis.rollback.strategies.mcp import restore as mcp_restore
from aegis.rollback.strategies.shell import capture as shell_capture
from aegis.rollback.strategies.shell import restore as shell_restore


def dispatch_capture(tool: str, args: dict[str, object], snap_dir: Path) -> list[str]:
    captured: list[str] = []
    for strategy in (file_capture, shell_capture, git_capture, mcp_capture):
        captured.extend(strategy(tool, args, snap_dir))
    return captured


def dispatch_restore(
    meta: dict[str, object], snap_dir: Path, *, allow_git: bool = False
) -> dict[str, list[str]]:
    restored: list[str] = []
    skipped: list[str] = []
    for strategy in (file_restore, shell_restore, git_restore, mcp_restore):
        r = strategy(meta, snap_dir, allow_git=allow_git)
        restored.extend(r["restored"])
        skipped.extend(r["skipped"])
    return {"restored": restored, "skipped": skipped}
