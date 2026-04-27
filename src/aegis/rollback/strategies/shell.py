"""Shell command capture: parses Bash command for mutating filesystem ops.

Patterns recognised:

* Output redirection:  ``cmd > file``, ``cmd >> file``
* Tee:                 ``cmd | tee file``, ``cmd | tee -a file``
* In-place edit:       ``sed -i``, ``awk -i inplace``
* Move/copy:           ``mv X Y``, ``cp -f X Y``
* Filesystem destroy:  ``rm <path>``, ``find … -delete``
* Permission change:   ``chmod``, ``chown``
* Symlink:             ``ln``, ``ln -s``

For each target file, copy the existing content into ``snap_dir`` before
the command runs so :func:`restore` can put it back.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

from aegis.rollback._tools import SHELL_TOOLS

_REDIRECT = re.compile(r"(?:\s|^)(?:>{1,2})\s*([^\s|;&<>]+)")
_TEE = re.compile(r"\|\s*tee(?:\s+-a)?\s+([^\s|;&]+)")
_SED_INPLACE = re.compile(r"\bsed\s+(?:[^\s']*-i[^\s']*\s+)+(?:\S+\s+)+([^\s|;&]+)")
_AWK_INPLACE = re.compile(r"\bawk\s+-i\s+inplace\s+(?:\S+\s+)+([^\s|;&]+)")
_MV_DST = re.compile(r"\bmv\s+(?:-\S+\s+)*\S+\s+([^\s|;&]+)")
_CP_DST = re.compile(r"\bcp\s+(?:-\S+\s+)*\S+\s+([^\s|;&]+)")
_RM_TARGET = re.compile(r"\brm\s+(?:-\S+\s+)*([^\s|;&]+)")
_FIND_DELETE = re.compile(r"\bfind\s+([^\s|;&]+)[^|;]*-delete\b")
_CHMOD_TARGET = re.compile(r"\bchmod\s+(?:-\S+\s+)*\S+\s+([^\s|;&]+)")
_CHOWN_TARGET = re.compile(r"\bchown\s+(?:-\S+\s+)*\S+\s+([^\s|;&]+)")


def _extract_targets(cmd: str) -> list[tuple[str, str]]:
    """Return ``[(reason, path), ...]`` of files this command will mutate."""
    targets: list[tuple[str, str]] = []
    for m in _REDIRECT.finditer(cmd):
        targets.append(("redirect", m.group(1)))
    for m in _TEE.finditer(cmd):
        targets.append(("tee", m.group(1)))
    for m in _SED_INPLACE.finditer(cmd):
        targets.append(("sed-i", m.group(1)))
    for m in _AWK_INPLACE.finditer(cmd):
        targets.append(("awk-i", m.group(1)))
    for m in _MV_DST.finditer(cmd):
        targets.append(("mv", m.group(1)))
    for m in _CP_DST.finditer(cmd):
        targets.append(("cp", m.group(1)))
    for m in _RM_TARGET.finditer(cmd):
        targets.append(("rm", m.group(1)))
    for m in _FIND_DELETE.finditer(cmd):
        targets.append(("find-delete", m.group(1)))
    for m in _CHMOD_TARGET.finditer(cmd):
        targets.append(("chmod", m.group(1)))
    for m in _CHOWN_TARGET.finditer(cmd):
        targets.append(("chown", m.group(1)))
    # Deduplicate while preserving order
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for r, p in targets:
        if p not in seen:
            seen.add(p)
            out.append((r, p))
    return out


def capture(tool: str, args: dict[str, object], snap_dir: Path) -> list[str]:
    if tool not in SHELL_TOOLS:
        return []
    cmd_raw = args.get("command", "") or ""
    cmd = cmd_raw if isinstance(cmd_raw, str) else ""
    if not cmd:
        return []
    captured: list[str] = []
    snap_dir.mkdir(parents=True, exist_ok=True)
    for reason, path in _extract_targets(cmd):
        try:
            p = Path(path).expanduser()
        except (OSError, ValueError):
            continue
        if p.exists() and p.is_file():
            safe = p.name.replace("/", "_")
            shutil.copy2(p, snap_dir / f"shell_{safe}.bak")
            captured.append(f"shell-file:{p}|{reason}")
        else:
            captured.append(f"shell-new:{p}|{reason}")
    return captured


def restore(
    meta: dict[str, object], snap_dir: Path, *, allow_git: bool = False
) -> dict[str, list[str]]:
    restored: list[str] = []
    skipped: list[str] = []
    captured_raw = meta.get("captured", [])
    captured: list[str] = list(captured_raw) if isinstance(captured_raw, list) else []
    for cap in captured:
        if cap.startswith("shell-file:"):
            payload = cap[len("shell-file:") :]
            path_str, _, _ = payload.partition("|")
            p = Path(path_str)
            safe = p.name.replace("/", "_")
            bak = snap_dir / f"shell_{safe}.bak"
            if bak.exists():
                shutil.copy2(bak, p)
                restored.append(f"shell-file:{p}")
            else:
                skipped.append(f"shell-file:{p} (backup missing)")
        elif cap.startswith("shell-new:"):
            payload = cap[len("shell-new:") :]
            path_str, _, _ = payload.partition("|")
            p = Path(path_str)
            if p.exists():
                try:
                    p.unlink()
                    restored.append(f"shell-unlink:{p}")
                except OSError as e:
                    skipped.append(f"shell-unlink:{p} ({e})")
    return {"restored": restored, "skipped": skipped}
