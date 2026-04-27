"""Pre-tool snapshot orchestrator.

Public surface:

* :func:`capture(invocation_id, tool, args)` → ``str`` (snapshot dir, may be empty)
* :func:`restore(invocation_id, *, allow_git=False, dry_run=False)` → result dict
* :func:`list_snapshots(limit)` → ``list[dict]`` (newest first)
* :func:`prune(older_than_secs)` → ``int`` (deleted count)
* :func:`bulk_restore(*, session_id=None, since_iso=None, dry_run=False, allow_git=False)`

The session-aware branch of :func:`bulk_restore` looks up invocation IDs
through :data:`session_invocations_lookup` — a hook that defaults to
returning an empty set. D11 (ATMU rule port) wires the MVP intent log in
so ``aegis rollback --session SID`` can resolve session → invocations.
"""

from __future__ import annotations

import json
import os
import shutil
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from aegis.rollback.strategies import dispatch_capture, dispatch_restore

SNAP_ROOT = Path(".aegis/snapshots")
SNAP_MAX = int(os.environ.get("AEGIS_SNAP_MAX", "1000"))


def _no_session_lookup(_session_id: str) -> set[str]:
    return set()


# Hook: ``session_invocations_lookup(session_id) -> set[str]`` returns the
# set of invocation_ids associated with a session/agent id. Defaults to an
# empty set; D11 may rebind it to query MVP's intent log.
session_invocations_lookup: Callable[[str], set[str]] = _no_session_lookup


def capture(invocation_id: str, tool: str, args: dict[str, object]) -> str:
    if not invocation_id:
        invocation_id = f"auto-{time.time_ns()}"
    snap_dir = SNAP_ROOT / invocation_id
    captured = dispatch_capture(tool, args, snap_dir)
    if not captured:
        return ""
    snap_dir.mkdir(parents=True, exist_ok=True)
    path_field = (
        args.get("file_path")
        or args.get("path")
        or args.get("filename")
    )
    meta: dict[str, Any] = {
        "invocation_id": invocation_id,
        "tool": tool,
        "ts_ns": time.time_ns(),
        "path": path_field if isinstance(path_field, str) else None,
        "captured": captured,
        # Keep a few useful args raw for debugging — secrets redacted below.
        "args_summary": _summarize_args(args),
    }
    (snap_dir / "meta.json").write_text(json.dumps(meta, indent=2))
    _enforce_max()
    return str(snap_dir)


def restore(
    invocation_id: str, *, allow_git: bool = False, dry_run: bool = False
) -> dict[str, Any]:
    snap_dir = SNAP_ROOT / invocation_id
    meta_path = snap_dir / "meta.json"
    if not meta_path.exists():
        raise FileNotFoundError(f"no snapshot for {invocation_id}")
    meta: dict[str, Any] = json.loads(meta_path.read_text())

    if dry_run:
        return {
            "would_restore": meta.get("captured", []),
            "tool": meta.get("tool"),
            "invocation_id": invocation_id,
            "dry_run": True,
        }

    result: dict[str, Any] = dict(
        dispatch_restore(meta, snap_dir, allow_git=allow_git)
    )
    result["meta"] = meta
    return result


def list_snapshots(limit: int = 50) -> list[dict[str, Any]]:
    if not SNAP_ROOT.exists():
        return []
    out: list[dict[str, Any]] = []
    for d in sorted(SNAP_ROOT.glob("*"), key=lambda p: p.stat().st_mtime, reverse=True):
        mp = d / "meta.json"
        if mp.exists():
            try:
                out.append(json.loads(mp.read_text()))
            except json.JSONDecodeError:
                continue
        if len(out) >= limit:
            break
    return out


def prune(older_than_secs: int) -> int:
    if not SNAP_ROOT.exists():
        return 0
    cutoff = time.time() - older_than_secs
    n = 0
    for d in SNAP_ROOT.iterdir():
        if not d.is_dir():
            continue
        try:
            if d.stat().st_mtime < cutoff:
                shutil.rmtree(d)
                n += 1
        except OSError:
            continue
    return n


def _enforce_max() -> None:
    if not SNAP_ROOT.exists():
        return
    dirs = sorted(SNAP_ROOT.iterdir(), key=lambda p: p.stat().st_mtime)
    excess = len(dirs) - SNAP_MAX
    if excess > 0:
        for d in dirs[:excess]:
            if d.is_dir():
                shutil.rmtree(d, ignore_errors=True)


def bulk_restore(
    *,
    session_id: str | None = None,
    since_iso: str | None = None,
    dry_run: bool = False,
    allow_git: bool = False,
) -> dict[str, Any]:
    """Restore many snapshots in reverse chronological order.

    Filter:

    * ``session_id`` — uses :data:`session_invocations_lookup` to resolve
      session → invocation_ids (defaults to empty set, so this branch
      finds nothing until D11 wires up MVP's intent log).
    * ``since_iso`` — snapshot mtime ≥ ``since_iso`` (parsed as UTC).
    """
    candidates: list[tuple[float, str]] = []  # (mtime, invocation_id)

    if session_id:
        wanted = session_invocations_lookup(session_id)
        for d in SNAP_ROOT.glob("*"):
            if d.name in wanted:
                candidates.append((d.stat().st_mtime, d.name))
    else:
        since_ts = 0.0
        if since_iso:
            since_ts = datetime.fromisoformat(since_iso).replace(tzinfo=UTC).timestamp()
        for d in SNAP_ROOT.glob("*"):
            if d.is_dir() and d.stat().st_mtime >= since_ts:
                candidates.append((d.stat().st_mtime, d.name))

    # Restore newest-first so older states stick.
    candidates.sort(reverse=True)
    all_restored: list[str] = []
    all_skipped: list[str] = []
    for _, inv in candidates:
        try:
            r = restore(inv, allow_git=allow_git, dry_run=dry_run)
        except FileNotFoundError as e:
            all_skipped.append(f"{inv} ({e})")
            continue
        if dry_run:
            all_restored.extend(r.get("would_restore", []))
        else:
            all_restored.extend(r.get("restored", []))
            all_skipped.extend(r.get("skipped", []))
    return {
        "candidates": len(candidates),
        "restored": all_restored,
        "skipped": all_skipped,
        "dry_run": dry_run,
    }


def _summarize_args(args: dict[str, object]) -> dict[str, object]:
    """Compact args for debugging (drop big content fields, redact secrets)."""
    secret_keys = {"password", "token", "api_key", "secret", "authorization"}
    out: dict[str, object] = {}
    for k, v in args.items():
        if k.lower() in secret_keys:
            out[k] = "<redacted>"
        elif isinstance(v, str) and len(v) > 200:
            out[k] = v[:200] + "...<truncated>"
        else:
            out[k] = v
    return out
