"""Rule matcher — apply a list of :class:`Rule` to an input string.

The matcher is intentionally simple: regex match (with re.IGNORECASE)
against the input. The CLI uses this for ``aegis rule test``; the
firewall integration will use it in step310 v2 (not in this MVP).

Return shape
------------
:func:`evaluate` returns a list of :class:`MatchResult` — one per
*enabled* rule that matched. Disabled rules are skipped. Rules with
invalid regex (shouldn't happen since :class:`Rule.__post_init__`
validates at construction) are skipped silently.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from aegis.rules.schema import Rule


@dataclass(frozen=True)
class MatchResult:
    """One rule matched the input."""

    rule: Rule
    matched_text: str    # the slice that matched (first match only)
    span: tuple[int, int]


def evaluate(text: str, rules: list[Rule]) -> list[MatchResult]:
    """Return all enabled rules that match ``text``.

    Order matches the input list order — callers can sort by
    severity if they want critical-first. Disabled rules are
    silently skipped.
    """
    out: list[MatchResult] = []
    for rule in rules:
        if not rule.enabled:
            continue
        try:
            m = re.search(rule.pattern, text, flags=re.IGNORECASE)
        except re.error:
            continue
        if m is None:
            continue
        out.append(MatchResult(
            rule=rule,
            matched_text=m.group(0),
            span=(m.start(), m.end()),
        ))
    return out


def any_blocking(matches: list[MatchResult]) -> bool:
    """Convenience: True iff any matched rule is ``critical``."""
    return any(m.rule.is_blocking for m in matches)


__all__ = ["MatchResult", "any_blocking", "evaluate"]
