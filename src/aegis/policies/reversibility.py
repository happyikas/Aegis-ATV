"""Reversibility classifier (v0.5.22).

Earlier autonomy idea ($1$): "``clean_rate`` is statistical; what
we really want is a principled safety signal — *can this action
be undone if it turns out to be wrong?*"

This module classifies (tool, tool_args_json) into one of four
levels:

* **trivial**       — read-only / no side effects (always safe to skip approval)
* **reversible**    — recoverable by trivial mechanism (rm a created file)
* **costly**        — recoverable but with effort (git restore for an Edit)
* **irreversible**  — destructive: ``rm -rf``, force-push, kubectl delete,
                      package publish, email send. **NEVER auto-bypassed**.

The policy is loaded from ``policies/reversibility.json``.
First-matching-rule-wins via regex on tool name + tool_args_json.
A default level (``"reversible"``) catches unmatched inputs so a
new tool doesn't accidentally get treated as irreversible.

### Integration with autonomy

:func:`aegis.autonomy.runtime.apply_autonomy_bypass` consults
this classifier as the FIRST gate after the env-on check. If the
action is irreversible, the bypass is refused regardless of trust
score, drift, or ε-greedy — the operator always stays in the loop
for destructive actions.

### Hot-path safety

* Regex patterns are compiled once at module load.
* Policy load is cached at first use (no per-call file I/O).
* All paths swallow exceptions and return the default (safe)
  level rather than raising. A corrupted policy file degrades
  gracefully to "everything is reversible" which keeps the
  autonomy module operating; the safety net here is that the
  autonomy never-trust filter still applies independently."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

REVERSIBILITY_LEVELS: Final[tuple[str, ...]] = (
    "trivial",
    "reversible",
    "costly",
    "irreversible",
)


# ──────────────────────────────────────────────────────────────────
# Policy resolution
# ──────────────────────────────────────────────────────────────────


def reversibility_policy_path() -> Path:
    """Return the path to the active reversibility policy file.

    Honours ``AEGIS_REVERSIBILITY_POLICY`` for tests / overrides;
    defaults to ``${AEGIS_POLICY_DIR}/reversibility.json`` then to
    the bundled ``policies/reversibility.json`` shipping with the
    package."""
    explicit = os.environ.get("AEGIS_REVERSIBILITY_POLICY", "").strip()
    if explicit:
        return Path(explicit)
    policy_dir = os.environ.get("AEGIS_POLICY_DIR", "").strip()
    if policy_dir:
        candidate = Path(policy_dir) / "reversibility.json"
        if candidate.exists():
            return candidate
    # Fall back to the source-tree default (this file is in
    # src/aegis/policies/, the JSON ships in policies/).
    return Path(__file__).resolve().parents[3] / "policies" / "reversibility.json"


# ──────────────────────────────────────────────────────────────────
# Compiled rule
# ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _CompiledRule:
    tool_re: re.Pattern[str]
    args_re: re.Pattern[str] | None
    level: str
    why: str


@dataclass(frozen=True)
class _CompiledPolicy:
    rules: tuple[_CompiledRule, ...] = field(default_factory=tuple)
    default_level: str = "reversible"


_POLICY_CACHE: dict[str, _CompiledPolicy] = {}


def _load_policy(path: Path) -> _CompiledPolicy:
    """Compile + cache the policy. Cache key is the path string
    so test overrides see fresh compiles."""
    key = str(path)
    cached = _POLICY_CACHE.get(key)
    if cached is not None:
        return cached
    if not path.exists():
        compiled = _CompiledPolicy()
        _POLICY_CACHE[key] = compiled
        return compiled
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        compiled = _CompiledPolicy()
        _POLICY_CACHE[key] = compiled
        return compiled

    rules_raw = payload.get("rules", [])
    if not isinstance(rules_raw, list):
        compiled = _CompiledPolicy()
        _POLICY_CACHE[key] = compiled
        return compiled

    compiled_rules: list[_CompiledRule] = []
    for raw in rules_raw:
        if not isinstance(raw, dict):
            continue
        level = str(raw.get("level", "")).strip()
        if level not in REVERSIBILITY_LEVELS:
            continue
        tool_pat = str(raw.get("tool_pattern", "") or "").strip()
        if not tool_pat:
            continue
        try:
            tool_re = re.compile(tool_pat)
        except re.error:
            continue
        args_pat_raw = raw.get("args_pattern")
        args_re: re.Pattern[str] | None = None
        if isinstance(args_pat_raw, str) and args_pat_raw.strip():
            try:
                args_re = re.compile(args_pat_raw, re.IGNORECASE)
            except re.error:
                continue
        compiled_rules.append(_CompiledRule(
            tool_re=tool_re,
            args_re=args_re,
            level=level,
            why=str(raw.get("why", ""))[:200],
        ))

    default_level = str(payload.get("default_level", "reversible"))
    if default_level not in REVERSIBILITY_LEVELS:
        default_level = "reversible"
    compiled = _CompiledPolicy(
        rules=tuple(compiled_rules), default_level=default_level,
    )
    _POLICY_CACHE[key] = compiled
    return compiled


def _clear_policy_cache() -> None:
    """For tests."""
    _POLICY_CACHE.clear()


# ──────────────────────────────────────────────────────────────────
# Public classification API
# ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ReversibilityClassification:
    """Result of classifying one action.

    ``why`` is the human-readable explanation from the matched
    rule (empty when the default level applied). ``matched`` is
    True iff a rule fired; useful for tests and for the diagnostic
    CLI."""

    level: str
    why: str = ""
    matched: bool = False


def classify_reversibility(
    tool_name: str,
    tool_args_json: str = "",
    *,
    policy_path: Path | None = None,
) -> ReversibilityClassification:
    """Classify one action. Never raises — degrades to the
    default level on any error.

    Search order: the rules array in declaration order. First
    matching rule wins; this lets the policy file express ordered
    fallthroughs (specific rule for ``rm -rf`` before the generic
    ``rm`` rule)."""
    if not tool_name:
        return ReversibilityClassification(level="reversible")
    actual_path = (
        policy_path if policy_path is not None
        else reversibility_policy_path()
    )
    try:
        policy = _load_policy(actual_path)
    except Exception:  # noqa: BLE001 — never raise from hot path
        return ReversibilityClassification(level="reversible")
    args = tool_args_json or ""
    for rule in policy.rules:
        if not rule.tool_re.search(tool_name):
            continue
        if rule.args_re is not None and not rule.args_re.search(args):
            continue
        return ReversibilityClassification(
            level=rule.level, why=rule.why, matched=True,
        )
    return ReversibilityClassification(level=policy.default_level)


def is_irreversible(
    tool_name: str,
    tool_args_json: str = "",
    *,
    policy_path: Path | None = None,
) -> bool:
    """Convenience: is this action ``irreversible``? Used by the
    autonomy bypass as a hard gate."""
    classification = classify_reversibility(
        tool_name, tool_args_json, policy_path=policy_path,
    )
    return classification.level == "irreversible"


__all__ = [
    "REVERSIBILITY_LEVELS",
    "ReversibilityClassification",
    "classify_reversibility",
    "is_irreversible",
    "reversibility_policy_path",
]
