"""Pre-tool snapshot + 4-strategy rollback (D4).

Donor: aegis-mvp v1.0.0 ``rollback/``.

The rollback module captures filesystem and git state immediately before
a tool runs, so the operator can ``aegis rollback INVOCATION_ID`` if the
tool's effect turns out to be wrong. Four pluggable strategies decide
what to capture per tool:

- :mod:`aegis.rollback.strategies.file`  — Write/Edit/MultiEdit and MCP
  variants: backup the existing file content, mark new files for unlink.
- :mod:`aegis.rollback.strategies.shell` — Bash command parsing for
  redirect, tee, sed -i, awk -i inplace, mv, cp, rm, find -delete,
  chmod, chown.
- :mod:`aegis.rollback.strategies.git`   — git commit/reset/rebase/
  checkout/merge/revert/stash drop/push/apply/am/cherry-pick: capture
  HEAD and uncommitted diff, restore behind ``--allow-git``.
- :mod:`aegis.rollback.strategies.mcp`   — generic MCP tool calls with
  mutating verbs: log only (HTTP/SQL side-effects are not generically
  reversible).

The public surface is :func:`capture`, :func:`restore`,
:func:`list_snapshots`, :func:`prune` and :func:`bulk_restore`.
"""

from __future__ import annotations

from aegis.rollback.snapshot import (
    bulk_restore,
    capture,
    list_snapshots,
    prune,
    restore,
)

__all__ = [
    "bulk_restore",
    "capture",
    "list_snapshots",
    "prune",
    "restore",
]
