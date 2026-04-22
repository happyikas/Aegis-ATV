"""Step 312 — Tool argument normalization (DOGFOOD Rec #3).

Closes finding FP-4: same target file, different tool wrapper, opposite
verdict.  Specifically:
    Read(file_path=.claude/settings.local.json)         -> BLOCKED by Haiku
    Bash(command="cat .claude/settings.local.json ...") -> ALLOWED

Both target the same path, but Haiku saw a structured ``file_path``
field for the first and an opaque shell pipeline for the second.

This step inserts BEFORE step 320 (blast) and BEFORE step 340 (sLLM)
and publishes a canonical ``{normalized_tool, target_path, intent}``
to ``ctx.extras`` so downstream steps + Haiku can reason against the
intent regardless of syntactic surface.

It does NOT short-circuit; it only enriches context.
"""

from __future__ import annotations

import re
import shlex
from typing import Literal

import numpy as np

from aegis.firewall.core import FirewallContext, StepResult
from aegis.schema import ATVInput

# Sub-commands that *read* a file as their primary arg.
_READ_LIKE: frozenset[str] = frozenset({
    "cat", "less", "more", "head", "tail",
    "grep", "rg", "ag",
    "wc", "file", "stat",
    "md5sum", "sha256sum", "sha3sum", "shasum",
    "diff", "cmp",
    "awk", "sed",
    "jq", "yq",
    "xxd", "hexdump",
})

# Sub-commands that *write* / *modify* a file as their primary arg.
_WRITE_LIKE: frozenset[str] = frozenset({
    "tee", "dd", "cp", "mv", "touch", "chmod", "chown",
})

# Sub-commands that *list* a directory as their primary arg.
_LIST_LIKE: frozenset[str] = frozenset({
    "ls", "tree", "find", "fd", "locate",
})

# Sub-commands that *delete* a path as their primary arg.
_DELETE_LIKE: frozenset[str] = frozenset({
    "rm", "rmdir", "unlink",
})


NormalizedTool = Literal[
    "read_file", "write_file", "list_directory", "delete_file", "execute_shell",
    "call_external_api", "send_email", "db_query", "db_mutation",
    "transfer_funds",
]


def _first_arg_path(tokens: list[str]) -> str | None:
    """First token after the command-word that looks like a path.

    Skips flags (``-l``, ``--all``) and stops at shell metacharacters
    like ``|``, ``>``, ``;``.
    """
    for tok in tokens[1:]:
        if tok in ("|", ">", ">>", ";", "&&", "||", "&"):
            break
        if tok.startswith("-"):
            continue
        if tok in ("$", "$()", "`"):
            continue
        return tok
    return None


def _normalize_bash_command(command: str) -> tuple[NormalizedTool, str | None] | None:
    """Inspect a Bash command and return (normalized_tool, path) if it
    matches a known read/write/list/delete shape. Else None."""
    try:
        tokens = shlex.split(command, comments=True, posix=True)
    except ValueError:
        tokens = command.split()

    if not tokens:
        return None

    first = tokens[0]
    if first in _READ_LIKE:
        return ("read_file", _first_arg_path(tokens))
    if first in _WRITE_LIKE:
        return ("write_file", _first_arg_path(tokens))
    if first in _LIST_LIKE:
        return ("list_directory", _first_arg_path(tokens))
    if first in _DELETE_LIKE:
        return ("delete_file", _first_arg_path(tokens))
    return None


def _extract_command(tool_args_json: str) -> str | None:
    if not tool_args_json:
        return None
    m = re.search(r'"command"\s*:\s*"((?:[^"\\]|\\.)*)"', tool_args_json)
    if not m:
        return None
    return m.group(1).encode("utf-8").decode("unicode_escape")


def _extract_path_field(tool_args_json: str) -> str | None:
    if not tool_args_json:
        return None
    for field in ("file_path", "path", "filename"):
        m = re.search(rf'"{field}"\s*:\s*"((?:[^"\\]|\\.)*)"', tool_args_json)
        if m:
            return m.group(1).encode("utf-8").decode("unicode_escape")
    return None


def run(atv: np.ndarray, inp: ATVInput, ctx: FirewallContext) -> StepResult:
    """Publish ``ctx.extras['normalized_tool']`` and ``['target_path']``.

    For native tools (``read_file``, ``write_file``, ``list_directory``,
    ``delete_file``), pass through.

    For ``execute_shell``, parse the command and try to map it to one
    of the natives. If the command doesn't match any known read/write
    shape, leave normalized_tool == "execute_shell".

    Never blocks.
    """
    tool = inp.tool_name
    path: str | None = None

    if tool == "execute_shell":
        cmd = _extract_command(inp.tool_args_json or "")
        if cmd:
            normalized = _normalize_bash_command(cmd)
            if normalized is not None:
                normalized_tool, path = normalized
                ctx.extras["normalized_tool"] = normalized_tool
                if path:
                    ctx.extras["target_path"] = path
                return StepResult(
                    None,
                    "",
                    f"step312: shell -> {normalized_tool} (path={path or '?'})",
                )

    # Native tool — pass through with structured fields.
    path = _extract_path_field(inp.tool_args_json or "")
    ctx.extras["normalized_tool"] = tool
    if path:
        ctx.extras["target_path"] = path

    return StepResult(None, "", f"step312: passthrough (tool={tool}, path={path or '?'})")
