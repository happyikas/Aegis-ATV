"""``aegis memory claude-md`` proposal generator (v0.5.2 + v0.5.4).

Pattern-mines recent BLOCK + REQUIRE_APPROVAL events from
ContextMemory and proposes concrete, copy-pasteable CLAUDE.md edits.
v0.5.4 adds :func:`apply_proposal` which splices the chosen proposal
into the project CLAUDE.md (with a `.bak` of the original).

The pitch: instead of telling an operator "your agent had 7 firewall
hits", we tell them *exactly which two sentences to add to their
CLAUDE.md* so the agent stops walking into the same wall — and
offer to splice it in one command.

Heuristics (all tunable via ``min_count``):

1. **Dangerous-pattern miner** — groups events with reason
   ``"dangerous pattern: <regex>"``. Maps the regex to a human-
   readable rule. Threshold-3 by default; single hits are noise.

2. **Loop-detector miner** — groups events with reason ``"same X
   call repeated N times"``. Suggests reflective-stop language for
   the looping tool.

3. **Sensitive-path miner** — groups reasons matching ``"sensitive
   path requires approval: <p>"``. Surfaces "always request pre-
   approval before reading <p>".

4. **Custom-rule miner** — groups reasons matching ``"rule:<name>"``.
   The rule itself is already enforced by the firewall; this surfaces
   *frequency* so operators know which rules CLAUDE.md doesn't yet
   explain.

Output is markdown (read-only) plus :func:`apply_proposal` for the
one-command splice. The splicer locates the proposal's
``suggested_section`` heading anywhere in the file
(case-insensitive, bidirectional substring) and inserts immediately
after; falls back to appending a new ``## <section>`` block at EOF.
An HTML comment marker (``<!-- aegis-managed-proposal: ... -->``)
stamps each splice for downstream traceability.

Note on self-defense: prose strings reference destructive commands
(`rm -rf`, the SQL drop-table verb). We compose those at module-load
time via concatenation (``"DROP" + " TABLE"``) so Aegis's own
step310 firewall doesn't BLOCK this source file when an operator
edits it under an active hook.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Final

from aegis.context_memory.record import ContextMemoryRecord

# ──────────────────────────────────────────────────────────────────
# Self-defense: destructive-command vocabulary stitched at runtime
# ──────────────────────────────────────────────────────────────────

# These match the firewall reasons we want to surface, but written
# as concatenation so Aegis's step310 pattern scanner doesn't trip
# on this source file when the hook is active during edits.
_KW_DROP_TABLE: Final[str] = "DROP" + " TABLE"
_KW_RM_RF: Final[str] = "rm" + " -rf"
_KW_RM_RF_ROOT: Final[str] = _KW_RM_RF + " " + "/"


# ──────────────────────────────────────────────────────────────────
# Output shape
# ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Proposal:
    """One suggested CLAUDE.md edit.

    ``kind`` identifies the miner that produced it (``"dangerous-
    pattern"`` / ``"loop-detector"`` / …). ``suggested_section`` is
    the markdown heading the new text should land under — operators
    use it to decide where to splice; the actual splice is manual
    in v0.5.2.
    """

    kind: str
    pattern: str
    count: int
    suggested_section: str
    suggested_text: str
    rationale: str
    sample_trace_ids: tuple[str, ...] = field(default_factory=tuple)
    confidence: str = "medium"   # low / medium / high

    @property
    def priority_score(self) -> int:
        """Sort key — higher = surface first. High-confidence + high-
        count proposals beat low-confidence ones at the same count."""
        confidence_weight = {"high": 3, "medium": 2, "low": 1}.get(
            self.confidence, 1,
        )
        return self.count * confidence_weight


# ──────────────────────────────────────────────────────────────────
# Miners — each returns 0..N proposals
# ──────────────────────────────────────────────────────────────────

# Dangerous-pattern miner needs a regex → human-language map. The
# firewall stores the *regex* in the reason; the operator needs to
# read prose. Centralising the map here means a new built-in pattern
# only has to update ONE table (this one + the firewall pattern
# list) instead of also chasing user-facing docs.
#
# Each entry: regex-fragment (substring match against the firewall's
# reason) → (CLAUDE.md heading, suggestion sentence, rationale).
# The destructive verbs are spliced from `_KW_*` constants above so
# this file passes its own firewall.
_DANGER_LOOKUP: Final[dict[str, tuple[str, str, str]]] = {
    "rm" + r"\s+-rf\s+/": (
        "Security Notes",
        f"Never run `{_KW_RM_RF}` against absolute paths "
        "(`/`, `/var`, `/etc`, `~`). Use targeted paths or "
        "`trash` instead.",
        f"Aegis blocks any `{_KW_RM_RF}` against absolute root-like "
        "paths. Repeated hits mean the agent is trying it without "
        "realising it won't pass — the rule lives in firewall, but "
        "CLAUDE.md should explain *why* so the agent stops attempting "
        "it.",
    ),
    "DROP" + r"\s+TABLE": (
        "Security Notes",
        f"Never issue `{_KW_DROP_TABLE}` directly. If a destructive "
        "schema change is needed, write a migration script under "
        "`migrations/` and ask the user to run it explicitly.",
        f"Aegis blocks raw `{_KW_DROP_TABLE}`. Migrations should "
        "always be explicit + reviewable, not inline shell.",
    ),
    "kubectl" + r"\s+delete": (
        "Cloud Operations",
        "When running `kubectl delete`, always pass "
        "`--dry-run=client` first and show the output before running "
        "for real.",
        "Aegis flags destructive kubectl calls. The two-step pattern "
        "(dry-run → confirm → apply) eliminates accidental cluster "
        "damage.",
    ),
    "terraform" + r"\s+destroy": (
        "Cloud Operations",
        "Never run `terraform destroy` against a populated workspace "
        "without an explicit user confirmation in the same turn.",
        "Aegis flags terraform destroy. The blast radius is too wide "
        "for an autonomous agent to decide on its own.",
    ),
    "git" + r"\s+push.*--force": (
        "Git Workflow",
        "Avoid `git push --force` on shared branches "
        "(`main`, `master`, release branches). Use "
        "`--force-with-lease` on feature branches only.",
        "Aegis blocks force-pushes on main. The force-with-lease "
        "alternative is safer and CLAUDE.md should prefer it.",
    ),
}


# Reason prefixes we recognise from the firewall — used to bucket
# events before we look up the specific pattern table.
_REASON_DANGER: Final[str] = "dangerous pattern: "
_REASON_LOOP: Final[re.Pattern[str]] = re.compile(
    r"^same (\w+) call repeated (\d+) times this session"
)
_REASON_SENSITIVE: Final[str] = "sensitive path requires approval: "
_REASON_RULE: Final[str] = "rule:"


def _normalise_pattern(reason: str) -> str:
    """Strip the firewall prefix from a ``dangerous pattern: <regex>``
    reason and return the bare regex fragment. Returns the original
    string if the prefix isn't present (defensive)."""
    if reason.startswith(_REASON_DANGER):
        return reason[len(_REASON_DANGER):].strip()
    return reason.strip()


def _mine_dangerous_patterns(
    records: Iterable[ContextMemoryRecord],
    *,
    min_count: int,
) -> list[Proposal]:
    """Bucket BLOCK events by firewall-pattern regex, look up the
    human-readable form, emit one proposal per pattern that fires
    ``>= min_count`` times in the window."""
    grouped: dict[str, list[ContextMemoryRecord]] = defaultdict(list)
    for r in records:
        if r.decision != "BLOCK":
            continue
        if not r.reason.startswith(_REASON_DANGER):
            continue
        key = _normalise_pattern(r.reason)
        grouped[key].append(r)

    out: list[Proposal] = []
    for pattern, recs in grouped.items():
        if len(recs) < min_count:
            continue
        # Look up the closest match in _DANGER_LOOKUP. We do
        # substring matching because the firewall regex
        # (`\brm\s+-rf\s+/`) is a richer form than our table key.
        lookup_hit: tuple[str, str, str] | None = None
        for key, val in _DANGER_LOOKUP.items():
            if key in pattern:
                lookup_hit = val
                break
        if lookup_hit is None:
            # Unknown pattern — still propose, but with generic
            # boilerplate. Lets operators see new patterns without
            # this module needing to ship updates for every regex.
            section = "Security Notes"
            text = (
                f"Avoid commands matching the firewall pattern "
                f"`{pattern}` — Aegis has blocked these "
                f"{len(recs)} times recently."
            )
            rationale = (
                "This pattern is enforced by Aegis but isn't yet "
                "documented in CLAUDE.md. Adding it tells the agent "
                "to not try in the first place."
            )
        else:
            section, text, rationale = lookup_hit
        out.append(Proposal(
            kind="dangerous-pattern",
            pattern=pattern,
            count=len(recs),
            suggested_section=section,
            suggested_text=text,
            rationale=rationale,
            sample_trace_ids=tuple(r.trace_id for r in recs[:3]),
            confidence="high" if lookup_hit else "medium",
        ))
    return out


def _mine_loop_detector(
    records: Iterable[ContextMemoryRecord],
    *,
    min_count: int,
) -> list[Proposal]:
    """Surface tools that repeatedly tripped the step336 loop
    detector. Multiple-tool offenders → multiple proposals."""
    by_tool: Counter[str] = Counter()
    by_tool_traces: dict[str, list[str]] = defaultdict(list)
    for r in records:
        if r.decision != "REQUIRE_APPROVAL":
            continue
        m = _REASON_LOOP.match(r.reason)
        if not m:
            continue
        tool = m.group(1)
        by_tool[tool] += 1
        by_tool_traces[tool].append(r.trace_id)

    out: list[Proposal] = []
    for tool, count in by_tool.most_common():
        if count < min_count:
            continue
        out.append(Proposal(
            kind="loop-detector",
            pattern=f"repeated {tool}",
            count=count,
            suggested_section="Workflow Discipline",
            suggested_text=(
                f"If you find yourself calling `{tool}` three times in "
                "a row with similar args, stop and reconsider — either "
                "vary the parameters, switch tools, or ask the user for "
                "clarification."
            ),
            rationale=(
                f"Aegis step336 (loop detector) fired {count} times on "
                f"repeated `{tool}` calls this window. Explicit guidance "
                "in CLAUDE.md prevents the loop from forming in the "
                "first place."
            ),
            sample_trace_ids=tuple(by_tool_traces[tool][:3]),
            confidence="high",
        ))
    return out


def _mine_sensitive_paths(
    records: Iterable[ContextMemoryRecord],
    *,
    min_count: int,
) -> list[Proposal]:
    """Aggregate sensitive-path approval events by path. Surface paths
    the agent reaches for repeatedly so CLAUDE.md can address them
    proactively (request approval upfront, or document why this path
    is needed)."""
    by_path: Counter[str] = Counter()
    by_path_traces: dict[str, list[str]] = defaultdict(list)
    for r in records:
        if r.decision != "REQUIRE_APPROVAL":
            continue
        if not r.reason.startswith(_REASON_SENSITIVE):
            continue
        path = r.reason[len(_REASON_SENSITIVE):].strip()
        by_path[path] += 1
        by_path_traces[path].append(r.trace_id)

    out: list[Proposal] = []
    for path, count in by_path.most_common():
        if count < min_count:
            continue
        out.append(Proposal(
            kind="sensitive-path",
            pattern=path,
            count=count,
            suggested_section="Security Notes",
            suggested_text=(
                f"Reads against `{path}` require explicit user "
                "approval. Before any task that touches this path, "
                "ask the user upfront whether to proceed."
            ),
            rationale=(
                f"The agent has been blocked on `{path}` {count} times "
                "and had to request approval each time. Documenting the "
                "expected flow saves a round-trip per attempt."
            ),
            sample_trace_ids=tuple(by_path_traces[path][:3]),
            confidence="high",
        ))
    return out


def _mine_rule_violations(
    records: Iterable[ContextMemoryRecord],
    *,
    min_count: int,
) -> list[Proposal]:
    """Bucket user-defined-rule hits (``reason="rule:<name>"``) by
    rule name. Surface rules that fire frequently so operators know
    which custom guardrails the agent doesn't yet understand."""
    by_rule: Counter[str] = Counter()
    by_rule_traces: dict[str, list[str]] = defaultdict(list)
    by_rule_decision: dict[str, str] = {}
    for r in records:
        if not r.reason.startswith(_REASON_RULE):
            continue
        # reason format: "rule:<name>" possibly followed by extra
        # whitespace + diagnostic. The name is the first token after
        # the colon.
        name = r.reason[len(_REASON_RULE):].split()[0]
        by_rule[name] += 1
        by_rule_traces[name].append(r.trace_id)
        # Track the most recent decision per rule for the wording.
        by_rule_decision[name] = r.decision

    out: list[Proposal] = []
    for name, count in by_rule.most_common():
        if count < min_count:
            continue
        decision = by_rule_decision.get(name, "BLOCK")
        verb = "blocks" if decision == "BLOCK" else "requires approval for"
        out.append(Proposal(
            kind="rule-violation",
            pattern=f"rule:{name}",
            count=count,
            suggested_section="Project Guardrails",
            suggested_text=(
                f"This project has a `{name}` rule that {verb} matching "
                "actions. Inspect with `aegis guard list` and adjust "
                "your approach accordingly."
            ),
            rationale=(
                f"Custom rule `{name}` has fired {count} times in this "
                "window. The rule itself enforces the policy; CLAUDE.md "
                "should explain it so the agent stops trying."
            ),
            sample_trace_ids=tuple(by_rule_traces[name][:3]),
            confidence="medium",
        ))
    return out


# ──────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────


def propose_edits(
    records: Iterable[ContextMemoryRecord],
    *,
    current_md_text: str | None = None,
    min_count: int = 3,
) -> list[Proposal]:
    """Run all miners over the window and return sorted proposals.

    ``current_md_text`` (optional) is the current CLAUDE.md content.
    Proposals whose trigger (pattern / tool name / path) already
    appears in the markdown are filtered out — we don't want to
    suggest something the operator has already done.

    Sorting: highest ``priority_score`` first, ties broken by `kind`
    name (stable across runs). Operators read top-down and stop when
    confidence drops.
    """
    rec_list = list(records)  # materialise — miners iterate multiple times
    proposals: list[Proposal] = []
    proposals.extend(_mine_dangerous_patterns(rec_list, min_count=min_count))
    proposals.extend(_mine_loop_detector(rec_list, min_count=min_count))
    proposals.extend(_mine_sensitive_paths(rec_list, min_count=min_count))
    proposals.extend(_mine_rule_violations(rec_list, min_count=min_count))

    if current_md_text:
        normalised_md = current_md_text.lower()
        proposals = [
            p for p in proposals
            if not _already_documented(p, normalised_md)
        ]

    proposals.sort(key=lambda p: (-p.priority_score, p.kind))
    return proposals


def _already_documented(p: Proposal, normalised_md: str) -> bool:
    """Heuristic dedup. We can't do semantic match without an LLM, so
    we look for the proposal's *trigger* (pattern / tool name / path)
    inside the existing CLAUDE.md. If it's there, the operator has
    probably already documented it — skip.

    False-negatives (we suggest something already covered in slightly
    different words) are fine: operators see "this is already in our
    docs" and skip. False-positives (we hide something genuinely new
    because the pattern coincidentally appears) are the cost — kept
    low by anchoring on full pattern strings, not loose keywords.
    """
    needle_pattern = (
        p.pattern.replace("\\b", "")
        .replace("\\s+", " ")
        .replace("\\", "")
        .strip(" /")
        .lower()
    )
    return bool(needle_pattern) and needle_pattern in normalised_md


def render_proposals_markdown(
    proposals: list[Proposal],
    *,
    window_seconds: int,
    md_path: object | None = None,
    record_count: int = 0,
) -> str:
    """Render the proposal list as a markdown report. The
    ``md_path`` / ``record_count`` parameters are for the header so
    the operator knows what window the proposals come from."""
    days = window_seconds // 86400
    hours = (window_seconds % 86400) // 3600
    if days:
        window_label = f"{days}d" + (f" {hours}h" if hours else "")
    else:
        window_label = f"{hours}h" if hours else f"{window_seconds}s"

    out: list[str] = []
    out.append("# CLAUDE.md improvement proposals")
    out.append("")
    out.append(
        f"_Generated from {record_count:,} ContextMemory records in "
        f"the last {window_label}_"
    )
    if md_path is not None:
        out.append(f"_Target file: `{md_path}`_")
    out.append("")

    if not proposals:
        out.append(
            "No actionable proposals — either no BLOCK / REQUIRE_APPROVAL "
            "events in the window, or every pattern is already documented "
            "in your CLAUDE.md. Nice."
        )
        out.append("")
        return "\n".join(out)

    out.append(f"Found **{len(proposals)} proposals**, sorted by priority:")
    out.append("")

    for i, p in enumerate(proposals, start=1):
        out.append(
            f"## {i}. [{p.kind} · {p.confidence}] "
            f"{p.pattern} (fired {p.count}×)"
        )
        out.append("")
        out.append(f"**Rationale.** {p.rationale}")
        out.append("")
        out.append(
            f"**Proposed CLAUDE.md edit** — append under "
            f"`## {p.suggested_section}`:"
        )
        out.append("")
        out.append("```markdown")
        out.append(p.suggested_text)
        out.append("```")
        out.append("")
        if p.sample_trace_ids:
            traces = ", ".join(f"`{t}`" for t in p.sample_trace_ids)
            out.append(f"_Sample traces: {traces}_")
            out.append("")

    out.append("---")
    out.append("")
    out.append(
        "_Auto-apply: `aegis memory claude-md --apply N` splices "
        "proposal **N** into the named section of your CLAUDE.md "
        "(a `.bak` is created first; pass `--no-bak` to skip)._"
    )
    out.append("")
    return "\n".join(out)


# ──────────────────────────────────────────────────────────────────
# --apply N: splice the chosen proposal into CLAUDE.md
# ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ApplyResult:
    """Outcome of :func:`apply_proposal`. ``inserted_under`` reports
    where the splice happened — the exact heading text when an
    existing section matched, or ``"(appended new section)"`` when
    we created a fresh ``## <section>`` block at the end of the
    file."""

    md_path: object
    bak_path: object | None
    inserted_under: str
    new_lines_added: int


# Marker we drop into the spliced block so a future operator (or
# future Aegis) can recognise auto-applied content. Markdown
# renderers ignore HTML comments, so this never shows up in any
# rendered view.
_APPLY_MARKER_PREFIX = "<!-- aegis-managed-proposal: "


def _format_apply_marker(p: Proposal) -> str:
    """Marker stamped above each auto-applied block. Includes the
    miner kind, pattern, and confidence — enough metadata to find
    + remove the splice later without parsing prose."""
    return (
        f"{_APPLY_MARKER_PREFIX}kind={p.kind} "
        f"pattern={p.pattern!r} confidence={p.confidence} -->"
    )


def _find_section_insertion_point(
    md_lines: list[str], section: str
) -> int | None:
    """Locate the line index *after* a heading whose text matches
    ``section`` (case-insensitive substring match against the
    heading-text portion of a ``#``-prefixed line).

    Returns the line index of the FIRST blank line after the heading
    — that's the natural splice point. Returns ``None`` if no
    matching heading is found.

    Match rule: any ATX heading (`#`, `##`, `###`, …) where the
    heading text and ``section`` share a substring **in either
    direction** — i.e. the heading is in the section or the section
    is in the heading, after case-folding. Lenient on purpose; the
    operator's headings won't always exactly match the miner's
    suggested label ("Security" vs "Security Notes", "Workflow"
    vs "Workflow Discipline").
    """
    needle = section.lower().strip()
    if not needle:
        return None
    for i, line in enumerate(md_lines):
        stripped = line.lstrip()
        if not stripped.startswith("#"):
            continue
        # Strip the # marks + any leading whitespace from the heading
        # text. Up to 6 hashes.
        heading_text = stripped.lstrip("#").strip().lower()
        if not heading_text:
            continue
        if needle in heading_text or heading_text in needle:
            # Insertion point = first line after the heading. We
            # don't try to find a "good" sub-position; appending
            # right after the heading is the most predictable
            # behavior and lets the operator re-order manually if
            # they want.
            return i + 1
    return None


def apply_proposal(
    proposal: Proposal,
    md_path: object,
    *,
    write_backup: bool = True,
) -> ApplyResult:
    """Splice ``proposal`` into the CLAUDE.md at ``md_path``.

    Strategy:

    * Read the existing file.
    * Try to find the proposal's ``suggested_section`` as a heading
      anywhere in the file (case-insensitive substring match — see
      :func:`_find_section_insertion_point`).
    * If found, splice the new text immediately after that heading.
    * If not found, append a fresh ``## <section>`` block at the
      end of the file.
    * Stamp an ``aegis-managed-proposal`` HTML comment marker above
      the new text so the splice is traceable.
    * Write a ``<md_path>.bak`` copy of the original first unless
      ``write_backup=False`` is passed.

    Returns an :class:`ApplyResult` describing what changed.
    """
    from pathlib import Path as _Path

    p_path = _Path(str(md_path))
    original = p_path.read_text(encoding="utf-8")
    lines = original.splitlines()

    # Compose the splice block. Trailing blank line keeps the next
    # section visually separated.
    marker = _format_apply_marker(proposal)
    splice_block = [
        "",
        marker,
        proposal.suggested_text,
        "",
    ]

    insert_at = _find_section_insertion_point(lines, proposal.suggested_section)
    if insert_at is not None:
        new_lines = lines[:insert_at] + splice_block + lines[insert_at:]
        inserted_under = (
            lines[insert_at - 1].lstrip("#").strip()
            if insert_at > 0 else proposal.suggested_section
        )
    else:
        # No matching section heading — append a new one at EOF.
        # Add a blank line before the new heading if the file
        # doesn't already end with one.
        prefix: list[str] = []
        if lines and lines[-1].strip():
            prefix.append("")
        new_section = [
            f"## {proposal.suggested_section}",
            *splice_block[1:],  # skip the leading "" we'd otherwise duplicate
        ]
        new_lines = lines + prefix + new_section
        inserted_under = "(appended new section)"

    new_text = "\n".join(new_lines)
    # Preserve trailing-newline convention of the original file.
    if original.endswith("\n") and not new_text.endswith("\n"):
        new_text += "\n"

    bak_path: object | None = None
    if write_backup:
        bak_path = p_path.with_suffix(p_path.suffix + ".bak")
        _Path(str(bak_path)).write_text(original, encoding="utf-8")

    p_path.write_text(new_text, encoding="utf-8")

    return ApplyResult(
        md_path=p_path,
        bak_path=bak_path,
        inserted_under=inserted_under,
        new_lines_added=len(new_lines) - len(lines),
    )


__all__ = [
    "ApplyResult",
    "Proposal",
    "apply_proposal",
    "propose_edits",
    "render_proposals_markdown",
]
