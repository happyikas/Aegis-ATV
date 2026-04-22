"""Step 310 — Argument Inspection (PLAN 6.4 + DOGFOOD Rec #2 + Rec #4).

Three layered checks (first match wins, in priority order):

1. **Sensitive-path classification (DOGFOOD Rec #2)** — extract a path
   from ``tool_args_json`` and look it up in
   ``policies/sensitive_paths.json``. Hard BLOCK for credential/key
   paths (~/.aws/credentials, ~/.ssh/id_rsa, /etc/shadow, *.pem, ...).
   REQUIRE_APPROVAL for sensitive-prefix paths (~/.ssh, ~/.aws,
   .env*, secrets/**). Closes dogfood findings FN-1 and FN-2.

2. **Dangerous regex with surface-class split (DOGFOOD Rec #4)** —
   classify the tool's arg surface as either ``execution`` (the args
   ARE a command, e.g. ``execute_shell.command``) or ``content``
   (the args ARE a payload, e.g. ``write_file.content``). Patterns
   matched in execution surfaces -> BLOCK. Patterns matched in
   content surfaces -> REQUIRE_APPROVAL. Closes dogfood findings
   FP-1 and FP-2.

3. **Prompt-injection threshold** — unchanged from original step 310.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import numpy as np

from aegis.config import settings
from aegis.firewall.core import FirewallContext, StepResult
from aegis.schema import ATVInput

# ─────────────────────────────────────────────────────────────────────
# Static dangerous-pattern regex (unchanged from original)
# ─────────────────────────────────────────────────────────────────────
DANGEROUS_PATTERNS: list[str] = [
    r"\brm\s+-rf\s+/",
    r"DROP\s+TABLE",
    r"/etc/(shadow|passwd)",
    r"\bsudo\s+",
    r"\b(exec|system)\s*\(",
]

INJECTION_THRESHOLD = 0.7

# DOGFOOD Rec #5 thresholds — applied to every populated safety_flags
# category, not just prompt_injection.
_SAFETY_THRESHOLDS: dict[str, float] = {
    "prompt_injection": INJECTION_THRESHOLD,
    "data_exfiltration": INJECTION_THRESHOLD,
    "sql_injection": INJECTION_THRESHOLD,
    "path_traversal": INJECTION_THRESHOLD,
    "pii_exposure": 0.85,        # higher bar: PII signals are noisier
    "toxicity": 0.90,            # higher bar: toxicity often non-actionable
}

_COMPILED = [re.compile(p, re.IGNORECASE) for p in DANGEROUS_PATTERNS]


# ─────────────────────────────────────────────────────────────────────
# DOGFOOD Rec #4: surface-class split
# ─────────────────────────────────────────────────────────────────────
# Tool names whose tool_args_json carries a content payload (not a
# command). Patterns matched in these surfaces de-escalate to
# REQUIRE_APPROVAL because the agent is writing/quoting a string ABOUT
# the pattern, not executing it.
_CONTENT_SURFACE_TOOLS: frozenset[str] = frozenset({
    "write_file",
    # NOTE: read_file, list_directory, db_query are NOT content surfaces —
    # their args carry path/SQL that DOES get executed/dereferenced.
    # delete_file is also NOT a content surface (it acts on the path).
})


def _surface_class(tool_name: str) -> Literal["execution", "content"]:
    """Classify the tool's arg surface."""
    return "content" if tool_name in _CONTENT_SURFACE_TOOLS else "execution"


# ─────────────────────────────────────────────────────────────────────
# DOGFOOD Rec #2: sensitive-path policy
# ─────────────────────────────────────────────────────────────────────
@lru_cache(maxsize=4)
def _load_sensitive_paths(policy_dir_str: str) -> dict[str, Any]:
    path = Path(policy_dir_str) / "sensitive_paths.json"
    if not path.exists():
        return {"block": {"patterns": [], "exceptions": []}, "approve": {"patterns": []}}
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


def reset_sensitive_paths_cache() -> None:
    """Test helper — clear the lru_cache so policies/ edits take effect."""
    _load_sensitive_paths.cache_clear()


def _expand_path(p: str) -> str:
    """Expand ~ in a path so matching works against absolute home paths."""
    return os.path.expanduser(p)


def _match_path_glob(path: str, pattern: str) -> bool:
    """Match a path against a single glob with ~/ + ** support.

    fnmatch.fnmatchcase doesn't natively support ``**``; we approximate
    by translating ``**`` to ``*`` for the leading anchor and relying
    on the prefix property: ``~/.ssh/**`` should match
    ``/Users/x/.ssh/something/nested``.
    """
    expanded_pattern = _expand_path(pattern)
    expanded_path = _expand_path(path)
    # Convert ** to a more permissive form for fnmatch
    if "**" in expanded_pattern:
        # Anchor-prefix match: pattern up to ** must be a prefix of the path.
        prefix = expanded_pattern.split("**", 1)[0]
        suffix = expanded_pattern.split("**", 1)[1]
        if not expanded_path.startswith(prefix):
            return False
        # Remaining path after prefix must match the suffix (using fnmatch).
        remaining = expanded_path[len(prefix) :]
        return fnmatch.fnmatchcase(remaining, "*" + suffix) or fnmatch.fnmatchcase(remaining, suffix.lstrip("/"))
    return fnmatch.fnmatchcase(expanded_path, expanded_pattern)


def _classify_path(path: str, policy: dict[str, Any]) -> Literal["block", "approve", "ok"]:
    """Classify a single path against the sensitive-paths policy."""
    block = policy.get("block", {})
    block_pats = block.get("patterns", [])
    block_excepts = block.get("exceptions", [])

    # Exceptions win over block — check first.
    for exc in block_excepts:
        if _match_path_glob(path, exc):
            # Falls through to approve check.
            break
    else:
        for pat in block_pats:
            if _match_path_glob(path, pat):
                return "block"

    approve_pats = policy.get("approve", {}).get("patterns", [])
    for pat in approve_pats:
        if _match_path_glob(path, pat):
            return "approve"

    return "ok"


def _extract_paths_from_args(tool_args_json: str) -> list[str]:
    """Pull out anything path-shaped from tool_args_json.

    Looks for ``"path"`` and ``"file_path"`` fields (used by Read,
    Edit, Write tools) plus any string value that begins with ``/``,
    ``~/``, or ``./`` and looks like a path. Tolerates malformed JSON.
    """
    if not tool_args_json:
        return []
    paths: list[str] = []

    # Structured fields
    for field in ("path", "file_path", "filename"):
        m = re.search(rf'"{field}"\s*:\s*"((?:[^"\\]|\\.)*)"', tool_args_json)
        if m:
            paths.append(m.group(1).encode("utf-8").decode("unicode_escape"))

    # For execute_shell, scan the command for path-shaped tokens (file
    # arguments). This lets `cat ~/.aws/credentials` get classified.
    cmd_match = re.search(r'"command"\s*:\s*"((?:[^"\\]|\\.)*)"', tool_args_json)
    if cmd_match:
        cmd = cmd_match.group(1).encode("utf-8").decode("unicode_escape")
        # Token-shaped path candidates: starts with / or ~/ and has at least one more component.
        for tok in re.findall(r"(?<![\w/])(/[^\s'\";>|]+|~/[^\s'\";>|]+)", cmd):
            paths.append(tok)

    return paths


# ─────────────────────────────────────────────────────────────────────
# Main step
# ─────────────────────────────────────────────────────────────────────
def run(atv: np.ndarray, inp: ATVInput, ctx: FirewallContext) -> StepResult:
    payload = inp.tool_args_json or ""

    # 1. Sensitive-path classification (DOGFOOD Rec #2)
    sens_policy = _load_sensitive_paths(settings.aegis_policy_dir)
    for path in _extract_paths_from_args(payload):
        verdict = _classify_path(path, sens_policy)
        if verdict == "block":
            return StepResult(
                "BLOCK",
                f"sensitive-path block: {path}",
                f"step310: sensitive-path block ({path})",
            )
        if verdict == "approve":
            return StepResult(
                "REQUIRE_APPROVAL",
                f"sensitive path requires approval: {path}",
                f"step310: sensitive-path approve ({path})",
            )

    # 2. Dangerous regex with surface-class split (DOGFOOD Rec #4)
    surface = _surface_class(inp.tool_name)
    for pat, compiled in zip(DANGEROUS_PATTERNS, _COMPILED, strict=True):
        if compiled.search(payload):
            if surface == "content":
                return StepResult(
                    "REQUIRE_APPROVAL",
                    f"dangerous pattern in content surface: {pat}",
                    f"step310: content-surface pattern ({pat}) — APPROVAL required, not BLOCK",
                )
            return StepResult(
                "BLOCK",
                f"dangerous pattern: {pat}",
                f"step310: static pattern hit ({pat})",
            )

    # 3. Safety-flag thresholds (DOGFOOD Rec #5).
    #    Pre-DOGFOOD this only consumed prompt_injection; the safety
    #    classifier was populating data_exfiltration / sql_injection /
    #    path_traversal too but no step ever read them. That's how
    #    FN-3 (a printf with a curl POST exfil command in its argument)
    #    slipped through with data_exfiltration=0.85 ignored.
    for cat, threshold in _SAFETY_THRESHOLDS.items():
        score = float(inp.safety_flags.get(cat, 0.0))
        if score > threshold:
            # In a content surface, soften to REQUIRE_APPROVAL for the
            # same reason as the regex case above (writing about !=
            # executing).
            decision = "REQUIRE_APPROVAL" if surface == "content" else "BLOCK"
            return StepResult(
                decision,
                f"{cat} score {score:.2f} > {threshold}",
                f"step310: safety {cat} ({score:.2f}>{threshold}) on {surface} surface",
            )

    return StepResult(
        None,
        "",
        f"step310: ok (inj={float(inp.safety_flags.get('prompt_injection', 0.0)):.2f})",
    )
