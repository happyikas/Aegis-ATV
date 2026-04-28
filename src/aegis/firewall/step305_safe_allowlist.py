"""Step 305 — Safe Action Auto-Allow (v2.1, Day-1 #1).

Runs FIRST in the pipeline. If the proposed call matches an entry in
``policies/safe_actions.json`` (a static allowlist of trivially safe
read-only / test / lint operations), publishes
``ctx.extras["safe_fast_path"] = True``. Subsequent expensive stages
(notably step340's sLLM judge round-trip) honor the flag and skip.

Crucially this step **does NOT short-circuit** the pipeline. Every
other gate — step310 dangerous regex, step311 donor rule pack, step320
blast, step335 cost — still runs against the call. So a safe-listed
operation is still BLOCKed if its args contain a destructive pattern
(e.g. ``ls`` is on the allowlist, but ``ls $(rm -rf /)`` would still
trip step310).

The "less prompts, better prompts" UX comes from skipping the LLM
judge for known-safe calls: median pre-tool latency drops from
~150 ms (Haiku round-trip) to <5 ms (regex + lookup only) for the
common case.

Matched paths:

* Tool name in the manifest's ``tools`` map with ``any_args: true``
  (Read / Grep / Glob etc.) → fast-path.
* For shell-class tools (Bash / shell / exec / …), the canonical
  command after step312 normalization is checked against
  ``bash_subcommands`` by prefix match. Shell metachars (``|``,
  ``;``, ``&``, ``>``, ``<``, backticks, ``$(...)``) in the raw
  command immediately disqualify — those revert to the full
  pipeline so we don't fast-path a piped destructive call.

The flag is set on ``ctx.extras["safe_fast_path"]`` (bool) plus
``ctx.extras["safe_match"]`` (str, the matched rule id) for audit.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

import numpy as np

from aegis.config import settings
from aegis.firewall.core import FirewallContext, StepResult
from aegis.schema import ATVInput

_SHELL_TOOL_NAMES: frozenset[str] = frozenset({
    "Bash", "shell", "bash", "exec", "sh", "zsh", "fish",
    "execute_shell", "run_command", "terminal",
})

# Disqualifying shell metachars: presence in command means we cannot
# fast-path because the actual blast surface depends on what the
# subshell / pipeline does.
_DISQUALIFYING_RE = re.compile(r"[|;&`]|\$\(|>>?|<<?")


@lru_cache(maxsize=4)
def _load_manifest(policy_dir_str: str) -> dict[str, Any]:
    path = Path(policy_dir_str) / "safe_actions.json"
    if not path.exists():
        return {"tools": {}, "bash_subcommands": []}
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


def reset_safe_actions_cache() -> None:
    """Test helper — clear the lru_cache after editing safe_actions.json."""
    _load_manifest.cache_clear()


def _matches_tool_entry(tool: str, manifest: dict[str, Any]) -> str | None:
    tools = manifest.get("tools") or {}
    entry = tools.get(tool)
    if isinstance(entry, dict) and entry.get("any_args"):
        return f"tool:{tool}"
    return None


def _command_text(inp: ATVInput) -> str:
    """Pull the canonical command text out of tool_args_json."""
    raw = inp.tool_args_json or ""
    if not raw:
        return ""
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return ""
    if not isinstance(parsed, dict):
        return ""
    cmd = parsed.get("command") or parsed.get("cmd") or ""
    return cmd if isinstance(cmd, str) else ""


def _matches_bash_prefix(cmd: str, manifest: dict[str, Any]) -> str | None:
    if not cmd:
        return None
    if _DISQUALIFYING_RE.search(cmd):
        # Pipeline / redirect / backtick / subshell — full pipeline.
        return None
    candidates = manifest.get("bash_subcommands") or []
    if not isinstance(candidates, list):
        return None
    cmd_norm = cmd.strip()
    for prefix in candidates:
        if not isinstance(prefix, str) or not prefix:
            continue
        if cmd_norm == prefix or cmd_norm.startswith(prefix + " "):
            return f"bash:{prefix}"
    return None


def run(
    atv: np.ndarray, inp: ATVInput, ctx: FirewallContext
) -> StepResult:
    """First-stage fast-path classifier — never blocks, may flag safe."""
    manifest = _load_manifest(settings.aegis_policy_dir)

    match: str | None = _matches_tool_entry(inp.tool_name, manifest)

    if match is None and inp.tool_name in _SHELL_TOOL_NAMES:
        match = _matches_bash_prefix(_command_text(inp), manifest)

    if match is not None:
        ctx.extras["safe_fast_path"] = True
        ctx.extras["safe_match"] = match
        return StepResult(
            verdict=None, reason="", trace=f"step305: safe_fast_path ({match})"
        )

    return StepResult(
        verdict=None, reason="", trace="step305: not safe-listed"
    )
