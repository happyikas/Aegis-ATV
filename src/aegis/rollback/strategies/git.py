"""Git capture: HEAD ref + uncommitted diff."""

from __future__ import annotations

import subprocess
from pathlib import Path

from aegis.rollback._tools import SHELL_TOOLS

_GIT_VERBS: tuple[str, ...] = (
    "git commit",
    "git reset",
    "git rebase",
    "git checkout",
    "git merge",
    "git revert",
    "git stash drop",
    "git push",
    "git apply",
    "git am",
    "git cherry-pick",
)


def _run(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=5.0,
        check=False,
    )


def _git_head() -> str | None:
    r = _run(["git", "rev-parse", "HEAD"])
    return r.stdout.strip() if r.returncode == 0 else None


def _git_branch() -> str | None:
    r = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"])
    return r.stdout.strip() if r.returncode == 0 else None


def capture(tool: str, args: dict[str, object], snap_dir: Path) -> list[str]:
    if tool not in SHELL_TOOLS:
        return []
    cmd_raw = args.get("command") or ""
    cmd = cmd_raw.lower() if isinstance(cmd_raw, str) else ""
    if not any(v in cmd for v in _GIT_VERBS):
        return []
    head = _git_head()
    if not head:
        return []
    snap_dir.mkdir(parents=True, exist_ok=True)
    # Capture uncommitted diff so dirty tree can be restored.
    try:
        diff = _run(["git", "diff", "HEAD"]).stdout
        (snap_dir / "uncommitted.diff").write_text(diff)
    except OSError:
        pass
    branch = _git_branch() or ""
    return [f"git:{head}", f"git-branch:{branch}"]


def restore(
    meta: dict[str, object], snap_dir: Path, *, allow_git: bool = False
) -> dict[str, list[str]]:
    restored: list[str] = []
    skipped: list[str] = []
    captured_raw = meta.get("captured", [])
    captured: list[str] = list(captured_raw) if isinstance(captured_raw, list) else []

    head: str | None = None
    branch = ""
    for cap in captured:
        if cap.startswith("git-branch:"):
            branch = cap.split(":", 1)[1]
        elif cap.startswith("git:"):
            head = cap.split(":", 1)[1]

    if not head:
        return {"restored": [], "skipped": []}
    if not allow_git:
        skipped.append(f"git:{head} (use --allow-git)")
        return {"restored": [], "skipped": skipped}

    if branch:
        r = _run(["git", "checkout", branch])
        if r.returncode != 0:
            skipped.append(f"git-checkout:{branch} ({r.stderr.strip()})")

    r = _run(["git", "reset", "--hard", head])
    if r.returncode == 0:
        restored.append(f"git:{head}")
    else:
        skipped.append(f"git-reset:{r.stderr.strip()}")

    # Reapply uncommitted diff if present.
    diff_file = snap_dir / "uncommitted.diff"
    if diff_file.exists() and diff_file.stat().st_size > 0:
        r = _run(["git", "apply", "--allow-empty", str(diff_file)])
        if r.returncode == 0:
            restored.append("git-uncommitted-restored")
        else:
            skipped.append(f"git-uncommitted: {r.stderr.strip()[:100]}")

    return {"restored": restored, "skipped": skipped}
