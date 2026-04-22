"""Step 320 — Blast Radius (PLAN 6.4 + DOGFOOD Rec #1).

Looks up the tool in a static table and publishes the value into
``ctx.blast_radius`` for later steps.

DOGFOOD Rec #1 refinement: when ``inp.tool_name == "execute_shell"``,
parse the first word(s) of ``tool_args_json`` and look up a sub-command
classification in ``policies/safe_bash_subcommands.json``. This
eliminates the headline false-positive from the dogfood report where
71% of Bash calls (every plain ``ls``, ``git status``, ``pwd``)
inherited blast=8 and got escalated to REQUIRE_APPROVAL at step 330.
"""

from __future__ import annotations

import json
import re
import shlex
from functools import lru_cache
from pathlib import Path
from typing import Any

import numpy as np

from aegis.config import settings
from aegis.firewall.core import FirewallContext, StepResult
from aegis.schema import ATVInput

TOOL_BLAST_TABLE: dict[str, int] = {
    "read_file": 1,
    "list_directory": 1,
    "write_file": 3,
    "execute_shell": 8,
    "call_external_api": 5,
    "send_email": 6,
    "db_query": 2,
    "db_mutation": 7,
    "transfer_funds": 10,
    "delete_file": 6,
}

UNKNOWN_TOOL_BLAST = 5


@lru_cache(maxsize=4)
def _load_bash_policy(policy_dir_str: str) -> dict[str, Any]:
    path = Path(policy_dir_str) / "safe_bash_subcommands.json"
    if not path.exists():
        # Fallback: behave exactly like pre-DOGFOOD step 320.
        return {
            "default_blast": TOOL_BLAST_TABLE["execute_shell"],
            "two_word_overrides": {},
            "categories": {},
        }
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


def reset_bash_policy_cache() -> None:
    """Test helper — clear the lru_cache so policies/ edits take effect."""
    _load_bash_policy.cache_clear()


def _extract_command_args(tool_args_json: str) -> str | None:
    """Pull the ``command`` field out of the tool_args JSON.

    The Claude Code Bash hook emits ``{"command": "...", ...}``. Other
    callers may emit different shapes; we use a regex rather than
    ``json.loads`` to stay tolerant of malformed input — step 310 has
    already done semantic validation.
    """
    if not tool_args_json:
        return None
    m = re.search(r'"command"\s*:\s*"((?:[^"\\]|\\.)*)"', tool_args_json)
    if not m:
        return None
    # Unescape the JSON string body.
    return m.group(1).encode("utf-8").decode("unicode_escape")


def _first_words(command: str, n: int = 2) -> list[str]:
    """Tokenize ``command`` and return the first ``n`` words.

    Uses ``shlex`` so quoted arguments stay grouped. Falls back to a
    plain split on shlex parse error (e.g. unbalanced quotes).
    """
    try:
        toks = shlex.split(command, comments=True, posix=True)
    except ValueError:
        toks = command.split()
    return toks[:n]


def _classify_bash(command: str, policy: dict[str, Any]) -> int:
    """Return the blast radius for a Bash command, per the policy.

    Lookup order:
        1. Exact two-word match in ``two_word_overrides``
        2. Exact two-word match in any ``categories[*].commands``
        3. One-word match in ``categories[*].commands``
        4. Fallback to ``policy.default_blast``
    """
    words = _first_words(command, n=2)
    if not words:
        return int(policy.get("default_blast", TOOL_BLAST_TABLE["execute_shell"]))

    one = words[0]
    two = " ".join(words[:2]) if len(words) >= 2 else None

    # 1. Two-word overrides (e.g. "git push" -> 8)
    if two and two in policy.get("two_word_overrides", {}):
        return int(policy["two_word_overrides"][two])

    # 2 + 3. Categories — prefer two-word match, then one-word match
    categories = policy.get("categories", {})
    for prefer in (two, one):
        if not prefer:
            continue
        for _cat_name, cat in categories.items():
            cmds = cat.get("commands", [])
            if prefer in cmds:
                return int(cat.get("blast", policy.get("default_blast", 8)))

    return int(policy.get("default_blast", TOOL_BLAST_TABLE["execute_shell"]))


def run(atv: np.ndarray, inp: ATVInput, ctx: FirewallContext) -> StepResult:
    """Compute and publish ``ctx.blast_radius``.

    For ``execute_shell``, refines the static blast=8 default by
    consulting the DOGFOOD Rec #1 policy file for the actual sub-command.
    """
    base = TOOL_BLAST_TABLE.get(inp.tool_name, UNKNOWN_TOOL_BLAST)

    if inp.tool_name == "execute_shell":
        cmd = _extract_command_args(inp.tool_args_json or "")
        if cmd:
            policy = _load_bash_policy(settings.aegis_policy_dir)
            base = _classify_bash(cmd, policy)
            ctx.extras["bash_first_word"] = (_first_words(cmd, n=1) or [""])[0]
            ctx.extras["bash_blast_source"] = "policy"

    ctx.blast_radius = base
    return StepResult(None, "", f"step320: blast={base} (tool={inp.tool_name})")
