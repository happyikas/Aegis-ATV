"""User-authored rules — Hookify-compatible markdown rule store.

Most users want to add custom block patterns without touching Python.
This package gives them a markdown-based authoring path:

    ~/.aegis/rules/block-production-rm.md
    ~/.aegis/rules/no-force-push.md
    …

Each file = one rule. Frontmatter declares metadata
(name / severity / enabled), body sections carry the pattern + the
block message. The same format Hookify uses, so
``aegis rule import <hookify-rule.md>`` is a zero-loss copy.

Storage is **append-friendly** — adding / disabling a rule never
requires restarting the firewall. The matcher reads the directory
on each call.

License model
-------------
* Solo Free — up to **3 user rules** total (the built-in firewall
  is unaffected).
* Pro / Team / Enterprise — unlimited.

The limit is checked at write time only; existing rules above the
limit stay loaded so a tier downgrade doesn't silently drop
protection.

Public API
----------
* :class:`Rule` — frozen dataclass for one rule
* :func:`rules_dir` / :func:`list_rules` / :func:`load_rule` /
  :func:`save_rule` / :func:`delete_rule` / :func:`set_enabled`
* :func:`suggest_regex` — natural-language → regex heuristic
* :func:`evaluate` — match input against all enabled rules
"""

from __future__ import annotations

from aegis.rules.matcher import evaluate
from aegis.rules.nl_to_regex import suggest_regex
from aegis.rules.schema import (
    VALID_SEVERITIES,
    Rule,
    parse_markdown,
    serialize_markdown,
)
from aegis.rules.storage import (
    SOLO_FREE_RULE_LIMIT,
    RuleError,
    delete_rule,
    list_rules,
    load_rule,
    rules_dir,
    save_rule,
    set_enabled,
)

__all__ = [
    "SOLO_FREE_RULE_LIMIT",
    "VALID_SEVERITIES",
    "Rule",
    "RuleError",
    "delete_rule",
    "evaluate",
    "list_rules",
    "load_rule",
    "parse_markdown",
    "rules_dir",
    "save_rule",
    "serialize_markdown",
    "set_enabled",
    "suggest_regex",
]
