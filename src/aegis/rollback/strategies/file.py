"""File-write/edit capture: backup existing file, mark new files for unlink."""

from __future__ import annotations

import shutil
from pathlib import Path

from aegis.rollback._tools import FILE_WRITE_TOOLS


def capture(tool: str, args: dict[str, object], snap_dir: Path) -> list[str]:
    if tool not in FILE_WRITE_TOOLS:
        return []
    # path could be under multiple keys, depending on the tool / MCP shape
    path = args.get("file_path") or args.get("path") or args.get("filename")
    if not isinstance(path, str) or not path:
        return []
    target = Path(path)
    if target.exists() and target.is_file():
        snap_dir.mkdir(parents=True, exist_ok=True)
        # Use sanitized filename inside snap_dir to avoid path collisions
        safe_name = target.name.replace("/", "_")
        shutil.copy2(target, snap_dir / f"file_{safe_name}.bak")
        return [f"file:{path}"]
    # Not yet existing → record new_file so we can unlink it on restore
    snap_dir.mkdir(parents=True, exist_ok=True)
    return [f"new_file:{path}"]


def restore(
    meta: dict[str, object], snap_dir: Path, *, allow_git: bool = False
) -> dict[str, list[str]]:
    restored: list[str] = []
    skipped: list[str] = []
    captured_raw = meta.get("captured", [])
    captured: list[str] = list(captured_raw) if isinstance(captured_raw, list) else []

    path_raw = meta.get("path")
    path = path_raw if isinstance(path_raw, str) else None
    if not path:
        for cap in captured:
            if cap.startswith(("file:", "new_file:")):
                path = cap.split(":", 1)[1]
                break
    if not path:
        return {"restored": [], "skipped": []}

    for cap in captured:
        if cap.startswith("file:"):
            p = Path(cap.split(":", 1)[1])
            safe = p.name.replace("/", "_")
            bak = snap_dir / f"file_{safe}.bak"
            # Older snapshots used "file.bak" — also try that.
            if not bak.exists():
                bak = snap_dir / "file.bak"
            if bak.exists():
                shutil.copy2(bak, p)
                restored.append(f"file:{p}")
            else:
                skipped.append(f"file:{p} (backup missing)")
        elif cap.startswith("new_file:"):
            p = Path(cap.split(":", 1)[1])
            if p.exists():
                try:
                    p.unlink()
                    restored.append(f"unlink:{p}")
                except OSError as e:
                    skipped.append(f"unlink:{p} ({e})")
    return {"restored": restored, "skipped": skipped}
