"""MCP-tool capture — generic strategy for ``mcp__<server>__<method>`` tools.

Strategy:

* If the tool name suggests file mutation, defer to the file strategy
  (this strategy returns nothing in that case so we don't double-snapshot).
* If the call has SQL or HTTP side-effects, log it (not generically
  reversible) so operators can audit / manually undo.
* If the verb in the tool name suggests mutation
  (``write/create/update/delete/post/send/exec`` etc.) record a
  ``log-only`` capture.
"""

from __future__ import annotations

import json
from pathlib import Path

_MUTATING_VERBS: tuple[str, ...] = (
    "write",
    "create",
    "update",
    "delete",
    "post",
    "send",
    "exec",
    "execute",
    "run",
    "modify",
    "patch",
    "put",
    "remove",
    "drop",
    "truncate",
)


def _looks_mutating(tool: str) -> bool:
    t = tool.lower()
    if not t.startswith("mcp__") and "mcp:" not in t:
        return False
    return any(v in t for v in _MUTATING_VERBS)


def capture(tool: str, args: dict[str, object], snap_dir: Path) -> list[str]:
    if not _looks_mutating(tool):
        return []
    snap_dir.mkdir(parents=True, exist_ok=True)
    # Best-effort: dump args for manual audit. Note: secrets are already
    # redacted upstream by snapshot._summarize_args.
    (snap_dir / "mcp_call.json").write_text(
        json.dumps({"tool": tool, "args": args}, default=str, indent=2)
    )
    return [f"mcp-log:{tool}"]


def restore(
    meta: dict[str, object], snap_dir: Path, *, allow_git: bool = False
) -> dict[str, list[str]]:
    # MCP side-effects (HTTP POST, SQL UPDATE, Slack send, …) are not
    # generically reversible — surface the log so a human can act.
    skipped: list[str] = []
    captured_raw = meta.get("captured", [])
    captured: list[str] = list(captured_raw) if isinstance(captured_raw, list) else []
    for cap in captured:
        if cap.startswith("mcp-log:"):
            skipped.append(f"{cap} (manual rollback required — see mcp_call.json)")
    return {"restored": [], "skipped": skipped}
