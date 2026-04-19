"""GET /source — return source-code snippets from inside the aegis package.

Powers the Theater's "Source-code paths" panel: when an /evaluate verdict
fires, the UI can fetch the exact function that captured the ATV, the
firewall step that examined it, and the audit-signing function — and
show them inline alongside the verdict.

Security:
    * path is resolved relative to ``src/aegis/`` and rejected if it
      escapes that root (path traversal).
    * only ``.py`` files are served.
    * total bytes returned are capped (large files trimmed).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query

DEF_RE = re.compile(r"^\s*(?:async\s+)?def\s+(\w+)\s*\(")
CLASS_RE = re.compile(r"^\s*class\s+(\w+)\b")
MAX_LINES = 200


def _find_function(lines: list[str], name: str) -> int | None:
    """Return 1-based line number where ``def name(`` starts, or None."""
    for i, line in enumerate(lines, start=1):
        m = DEF_RE.match(line)
        if m and m.group(1) == name:
            return i
    return None


def _slice_function(lines: list[str], start_line: int, max_len: int) -> tuple[int, int]:
    """Return (start, end) 1-based slice that covers a single function body.

    Stops at the next top-level ``def`` / ``class`` (less indentation than
    the target function), at end-of-file, or at ``max_len`` lines.
    """
    base_indent = len(lines[start_line - 1]) - len(lines[start_line - 1].lstrip())
    end = start_line
    for j in range(start_line + 1, min(len(lines) + 1, start_line + max_len)):
        line = lines[j - 1]
        if line.strip() == "":
            end = j
            continue
        indent = len(line) - len(line.lstrip())
        if indent <= base_indent and (DEF_RE.match(line) or CLASS_RE.match(line)):
            break
        end = j
    return start_line, end


def make_router(*, package_root: Path) -> APIRouter:
    package_root = package_root.resolve()
    r = APIRouter()

    @r.get("/source")
    def source(
        path: str = Query(..., description="Path relative to src/aegis/, e.g. 'firewall/step310_args.py'"),
        function: str = Query("", description="Optional function name to slice to"),
        max_lines: int = Query(60, ge=1, le=MAX_LINES),
    ) -> dict[str, Any]:
        # Reject obvious junk before resolving (defense in depth).
        if "\x00" in path or path.startswith("/"):
            raise HTTPException(400, "invalid path")
        target = (package_root / path).resolve()
        try:
            target.relative_to(package_root)
        except ValueError as e:
            raise HTTPException(400, f"path escapes aegis package: {path}") from e
        if target.suffix != ".py":
            raise HTTPException(400, "only .py source files are served")
        if not target.is_file():
            raise HTTPException(404, f"not found: {path}")

        text = target.read_text(encoding="utf-8")
        lines = text.splitlines()

        start_line = 1
        end_line = min(len(lines), max_lines)

        if function:
            found = _find_function(lines, function)
            if found is None:
                raise HTTPException(404, f"function '{function}' not in {path}")
            start_line, end_line = _slice_function(lines, found, max_lines)

        snippet = "\n".join(lines[start_line - 1:end_line])

        return {
            "path": path,
            "function": function or None,
            "start_line": start_line,
            "end_line": end_line,
            "total_lines": len(lines),
            "snippet": snippet,
        }

    return r
