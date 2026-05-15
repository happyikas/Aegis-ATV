"""Rule schema — markdown frontmatter + body sections.

Format (Hookify-compatible)::

    ---
    name: block-production-rm
    severity: critical
    enabled: true
    source: user
    ---
    # Description
    Block any bash command that targets the production folder.

    # Pattern (regex)
    \\brm\\s+.*?/production/

    # Message
    Block: production folder is read-only by policy.

* Frontmatter: YAML-like ``key: value`` lines between ``---`` markers.
  We parse a minimal subset (strings + bools) without a full YAML
  dep so the rule format stays simple + diff-friendly.
* Body sections are ``# Header`` blocks. We recognise three:
  Description, Pattern, Message. Other headers are kept as
  ``extra_sections`` for forward compatibility.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# ── constants ────────────────────────────────────────────────────

VALID_SEVERITIES: frozenset[str] = frozenset({"critical", "warning", "info"})
"""Accepted ``severity`` values. ``critical`` BLOCKs, ``warning``
triggers REQUIRE_APPROVAL, ``info`` annotates without blocking."""

VALID_SOURCES: frozenset[str] = frozenset({"user", "hookify", "builtin"})


# ── dataclass ────────────────────────────────────────────────────


@dataclass(frozen=True)
class Rule:
    """One user-authored rule.

    Frozen so callers can pass it freely. ``replace`` works for
    targeted updates (enable / disable / rename).
    """

    name: str
    severity: str         # critical / warning / info
    enabled: bool
    description: str
    pattern: str          # regex source
    message: str
    source: str = "user"  # user / hookify / builtin
    extra_sections: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Validate at construction so invalid Rules can't exist
        if not self.name.strip():
            raise SchemaError("rule name cannot be empty")
        if not _NAME_RE.fullmatch(self.name):
            raise SchemaError(
                f"rule name {self.name!r} must match [a-z0-9-]+ "
                "(lowercase, digits, hyphens)",
            )
        if self.severity not in VALID_SEVERITIES:
            raise SchemaError(
                f"unknown severity {self.severity!r}; "
                f"expected one of {sorted(VALID_SEVERITIES)}",
            )
        if self.source not in VALID_SOURCES:
            raise SchemaError(
                f"unknown source {self.source!r}; "
                f"expected one of {sorted(VALID_SOURCES)}",
            )
        if not self.pattern.strip():
            raise SchemaError("pattern (regex) cannot be empty")
        # Validate the regex compiles
        try:
            re.compile(self.pattern)
        except re.error as e:
            raise SchemaError(
                f"pattern {self.pattern!r} is not a valid regex: {e}",
            ) from e

    @property
    def is_blocking(self) -> bool:
        """``critical`` severity blocks; warning/info do not."""
        return self.severity == "critical"


class SchemaError(ValueError):
    """Raised on invalid rule construction or parse failure."""


_NAME_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?")


# ── parse / serialize ────────────────────────────────────────────


def parse_markdown(text: str) -> Rule:
    """Parse a markdown rule file into a :class:`Rule`.

    Raises :class:`SchemaError` on malformed input. Strict by design
    — silent fallbacks on rule definitions would let typos blackhole
    block behaviour.
    """
    frontmatter, body = _split_frontmatter(text)
    sections = _parse_sections(body)

    fm = _parse_frontmatter(frontmatter)
    name = fm.get("name", "").strip()
    severity = fm.get("severity", "critical").strip()
    enabled_raw = fm.get("enabled", "true").strip().lower()
    enabled = enabled_raw in ("true", "yes", "1", "on")
    source = fm.get("source", "user").strip()

    description = sections.pop("description", "").strip()
    pattern = sections.pop("pattern", "").strip()
    # Strip "(regex)" or "(regex pattern)" qualifiers in headers
    if not pattern:
        for key in list(sections):
            if key.lower().startswith("pattern"):
                pattern = sections.pop(key).strip()
                break
    message = sections.pop("message", "").strip()
    if not message:
        for key in list(sections):
            if key.lower().startswith("custom message"):
                message = sections.pop(key).strip()
                break

    # Strip code-block fences if user wrapped the pattern in ```
    pattern = _strip_code_fence(pattern)

    if not message:
        message = f"Blocked by rule: {name}"

    return Rule(
        name=name or "unnamed",
        severity=severity,
        enabled=enabled,
        description=description,
        pattern=pattern,
        message=message,
        source=source,
        extra_sections=sections,
    )


def serialize_markdown(rule: Rule) -> str:
    """Round-trip the rule back to its on-disk markdown form."""
    lines: list[str] = [
        "---",
        f"name: {rule.name}",
        f"severity: {rule.severity}",
        f"enabled: {'true' if rule.enabled else 'false'}",
        f"source: {rule.source}",
        "---",
        "",
    ]
    if rule.description:
        lines.append("# Description")
        lines.append("")
        lines.append(rule.description.strip())
        lines.append("")
    lines.append("# Pattern (regex)")
    lines.append("")
    lines.append("```")
    lines.append(rule.pattern.strip())
    lines.append("```")
    lines.append("")
    lines.append("# Message")
    lines.append("")
    lines.append(rule.message.strip())
    lines.append("")
    for k, v in rule.extra_sections.items():
        lines.append(f"# {k}")
        lines.append("")
        lines.append(v.strip())
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# ── helpers ──────────────────────────────────────────────────────


_FENCE_RE = re.compile(
    r"^```(?:[a-z]+)?\s*\n(?P<body>.*?)\n```\s*$",
    re.DOTALL,
)


def _strip_code_fence(text: str) -> str:
    """If ``text`` is wrapped in a markdown code fence, return the
    inner body. Otherwise return the original."""
    m = _FENCE_RE.match(text.strip())
    if m:
        return m.group("body").strip()
    return text.strip()


def _split_frontmatter(text: str) -> tuple[str, str]:
    """Split ``text`` into ``(frontmatter_text, body_text)``.

    Accepts both ``---`` block style and frontmatter-less files
    (returns empty frontmatter then). Strict-ish: missing closing
    ``---`` is a parse error.
    """
    stripped = text.lstrip()
    if not stripped.startswith("---"):
        return "", text
    rest = stripped[3:]
    # Look for the closing ---
    end_idx = rest.find("\n---")
    if end_idx == -1:
        raise SchemaError("frontmatter not closed with --- delimiter")
    fm = rest[:end_idx].lstrip("\n")
    body = rest[end_idx + 4:].lstrip("\n")
    return fm, body


def _parse_frontmatter(text: str) -> dict[str, str]:
    """Parse minimal YAML-like ``key: value`` pairs."""
    out: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        out[key.strip().lower()] = value.strip().strip("\"'")
    return out


def _parse_sections(text: str) -> dict[str, str]:
    """Parse ``# Header`` blocks into a dict of ``{lower_header: body}``.

    Header lookup is case-insensitive. Body retains internal newlines
    until the next header.
    """
    sections: dict[str, str] = {}
    current: str | None = None
    buf: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line
        if line.startswith("# "):
            if current is not None:
                sections[current] = "\n".join(buf).strip()
            current = line[2:].strip().lower()
            buf = []
        else:
            buf.append(line)
    if current is not None:
        sections[current] = "\n".join(buf).strip()
    return sections


def normalise_name(raw: str) -> str:
    """Convert a free-form name to the ``[a-z0-9-]+`` slug form.

    Used by :func:`aegis.rules.nl_to_regex.suggest_rule_name` and
    by the CLI ``--name`` validator. Falls back to ``custom-rule`` if
    the input is empty or has no alphanumerics.
    """
    s = raw.strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = s.strip("-")
    return s or "custom-rule"


# Re-export for parsers that want the unbound dict type
def empty_extras() -> dict[str, Any]:
    return {}
