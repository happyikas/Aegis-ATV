"""Rule storage — markdown files under ``~/.aegis/rules/``.

Each rule lives in its own ``.md`` file; the file's stem must match
the rule's ``name`` field. Listing is a directory glob; mutation is
atomic write to a tempfile + rename.

License gate
------------
Solo Free is capped at :data:`SOLO_FREE_RULE_LIMIT` (3) ``user`` /
``hookify`` rules at write time. ``builtin`` rules and pre-existing
rules above the cap are not affected — only **new** writes are
gated. Pro / Team / Enterprise are unlimited.

Env override
------------
``AEGIS_RULES_DIR`` — override the default ``~/.aegis/rules``.
Useful for tests + multi-tenant sidecar layouts.
"""

from __future__ import annotations

import os
from pathlib import Path

from aegis.rules.schema import (
    Rule,
    SchemaError,
    parse_markdown,
    serialize_markdown,
)

SOLO_FREE_RULE_LIMIT: int = 3
"""Maximum number of user-authored rules in Solo Free tier."""


class RuleError(Exception):
    """Raised on storage-level failures (collision, quota, missing)."""


# ── path resolution ──────────────────────────────────────────────


def rules_dir() -> Path:
    """Return the rules directory (created lazily by callers)."""
    raw = os.environ.get("AEGIS_RULES_DIR", "").strip()
    if raw:
        return Path(raw)
    return Path.home() / ".aegis" / "rules"


def _path_for(name: str, *, base: Path | None = None) -> Path:
    return (base or rules_dir()) / f"{name}.md"


# ── read ─────────────────────────────────────────────────────────


def list_rules(*, base: Path | None = None) -> list[Rule]:
    """Return all rules in the directory, sorted by name.

    Skips files that fail to parse (logged to stderr by callers).
    Empty directory → empty list. Missing directory → empty list.
    """
    d = base or rules_dir()
    if not d.exists():
        return []
    out: list[Rule] = []
    for path in sorted(d.glob("*.md")):
        try:
            text = path.read_text(encoding="utf-8")
            rule = parse_markdown(text)
        except (OSError, SchemaError):
            continue
        out.append(rule)
    return out


def load_rule(name: str, *, base: Path | None = None) -> Rule | None:
    """Return one rule by name, or ``None`` if missing / malformed."""
    p = _path_for(name, base=base)
    if not p.exists():
        return None
    try:
        return parse_markdown(p.read_text(encoding="utf-8"))
    except (OSError, SchemaError):
        return None


# ── write ────────────────────────────────────────────────────────


def save_rule(
    rule: Rule,
    *,
    overwrite: bool = False,
    license_tier: str = "free",
    base: Path | None = None,
) -> Path:
    """Atomically write ``rule`` to ``<rules_dir>/<name>.md``.

    Parameters
    ----------
    overwrite:
        When ``False`` (default), raise :class:`RuleError` if a rule
        with the same name already exists. When ``True``, replace.
    license_tier:
        ``free`` enforces :data:`SOLO_FREE_RULE_LIMIT`. Pro+ tiers
        skip the gate. The check counts rules currently on disk —
        a re-save (overwrite) of an existing rule doesn't increase
        the count.
    base:
        Directory override (tests + sidecar callers).
    """
    d = base or rules_dir()
    d.mkdir(parents=True, exist_ok=True)
    target = _path_for(rule.name, base=d)

    if target.exists() and not overwrite:
        raise RuleError(
            f"rule {rule.name!r} already exists at {target}. "
            "Re-run with --force to overwrite.",
        )

    # License gate — count user/hookify rules currently on disk.
    if license_tier.lower() == "free":
        existing = [
            r for r in list_rules(base=d)
            if r.source in ("user", "hookify")
            and r.name != rule.name  # don't count self when overwriting
        ]
        if (
            rule.source in ("user", "hookify")
            and len(existing) >= SOLO_FREE_RULE_LIMIT
        ):
            raise RuleError(
                f"Solo Free tier limited to {SOLO_FREE_RULE_LIMIT} user "
                f"rules; you already have {len(existing)}. Upgrade to "
                "Pro+ for unlimited rules (PRICING.md).",
            )

    # Atomic write: write to .tmp then rename
    tmp = target.with_suffix(".md.tmp")
    tmp.write_text(serialize_markdown(rule), encoding="utf-8")
    tmp.replace(target)
    return target


def delete_rule(name: str, *, base: Path | None = None) -> bool:
    """Delete ``<name>.md`` from the rules dir. Returns ``True`` if
    a file was removed; ``False`` if it didn't exist."""
    p = _path_for(name, base=base)
    if not p.exists():
        return False
    try:
        p.unlink()
        return True
    except OSError:
        return False


def set_enabled(
    name: str,
    enabled: bool,
    *,
    base: Path | None = None,
) -> bool:
    """Toggle ``enabled`` on an existing rule.

    Returns ``True`` on success, ``False`` if the rule doesn't exist
    or can't be parsed.
    """
    rule = load_rule(name, base=base)
    if rule is None:
        return False
    if rule.enabled == enabled:
        return True  # idempotent
    from dataclasses import replace
    updated = replace(rule, enabled=enabled)
    try:
        save_rule(
            updated,
            overwrite=True,
            license_tier="enterprise",  # toggle never trips quota
            base=base,
        )
        return True
    except (RuleError, SchemaError):
        return False


__all__ = [
    "SOLO_FREE_RULE_LIMIT",
    "RuleError",
    "delete_rule",
    "list_rules",
    "load_rule",
    "rules_dir",
    "save_rule",
    "set_enabled",
]
