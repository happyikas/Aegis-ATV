"""Prompt-cache stabilisation lint (v3.8) — observe and diagnose
why an agent session's prompt-cache hit rate is below its potential.

Two analysis modes
------------------

**Dynamic** — observe a real Claude Code transcript:

* per-turn cache efficiency = ``cache_read / (cache_read +
  cache_creation + input_tokens)``
* detect "cache breaks" — turns where efficiency drops sharply
  (default ≥ 30 percentage points). Each break is attributed to a
  likely cause (new tool registered, system-prompt change,
  unknown) by walking the turn metadata around the break.
* aggregate the observed hit rate vs the theoretical maximum
  (efficiency that would have held if no break had occurred) and
  the token-savings the user is leaving on the table.

**Static** — analyse a system-prompt / tool-catalog string for the
classic "broken cache" anti-patterns: dates, UUIDs, time-of-day
phrases, epoch-ms timestamps, "Today is …" / "Generated at …"
preludes. Each finding pins the offending byte range and explains
what to do (move below a ``cache_control`` marker, push into the
user message, etc.).

Privacy posture
---------------
The report carries pure metadata: token counts, ratios, character
offsets, and at most a 60-char excerpt of each static-lint match.
No raw prompt body, no tool arguments, no user content beyond the
truncated excerpt. Aligns with Aegis's audit-chain default-off
policy for raw prompt capture (PR #47 ``UserPromptSubmit``).

Patent linkage
--------------
Claim 33 (placement / scheduling advisor surface) — sibling head:
the lint is the *diagnostic* projection of the same ATV cost band
that the placement advisor uses for its prescriptive advice.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

# Default sensitivity for break detection. 30 pp is a balance:
# small per-turn jitter (5 - 15 pp) doesn't trip; a real cache
# invalidation usually produces a 40 - 80 pp drop.
DEFAULT_BREAK_THRESHOLD_PP: float = 30.0

# Maximum chars of a static-lint match we'll surface in the report.
# Keeps PII / secrets out of the diagnostic output.
STATIC_EXCERPT_MAX_CHARS: int = 60

# Pattern names whose match value MIGHT be a secret (UUIDs are
# frequently used as session tokens / API keys, epoch_ms is rarely a
# secret on its own but combined with other context can fingerprint).
# When a finding's pattern is in this set, the excerpt is REDACTED —
# only enough characters survive to confirm shape, but not enough to
# leak the secret if the report ends up in a log / support ticket.
_REDACT_PATTERNS: frozenset[str] = frozenset({
    "uuid",
    "epoch_ms",
})


def _redact_excerpt(pattern_name: str, raw: str) -> str:
    """Mask an anti-pattern match when it could be a secret.

    Strategy: keep enough characters to recognise the shape (so the
    finding is debuggable) but mask the bulk of the value with ``×``
    so it can't be reversed back to the original token.

    * ``uuid`` (``a1b2c3d4-e5f6-7890-abcd-ef1234567890``):
      keep first 8 chars + dash, mask the rest as ``×``s.
      → ``a1b2c3d4-××××-××××-××××-××××××××××××``
    * ``epoch_ms`` (13-digit number):
      keep first 4 digits, mask the rest.
      → ``1714×××××××××``
    * other patterns are returned unchanged — date / time / phrase
      markers are not secrets.
    """
    if pattern_name not in _REDACT_PATTERNS or not raw:
        return raw
    if pattern_name == "uuid":
        # UUID structure: 8-4-4-4-12. Keep first 9 (8 + dash), mask rest.
        if len(raw) < 9:
            return "×" * len(raw)
        return raw[:9] + "".join("×" if c != "-" else "-" for c in raw[9:])
    if pattern_name == "epoch_ms":
        if len(raw) <= 4:
            return "×" * len(raw)
        return raw[:4] + "×" * (len(raw) - 4)
    return raw

Severity = Literal["error", "warning", "info"]


# ──────────────────────────────────────────────────────────────────────
# Per-turn data
# ──────────────────────────────────────────────────────────────────────


@dataclass
class TurnEfficiency:
    """One assistant turn's cache picture."""

    turn_idx: int                          # 0-based among assistant turns
    input_tokens: int                      # fresh (not cached) input
    cache_read: int                        # cached prefix bytes hit
    cache_creation: int                    # bytes newly added to cache
    output_tokens: int
    total_input: int                       # = input + cache_read + cache_creation
    efficiency: float                      # cache_read / total_input
    n_tool_uses: int = 0                   # tool_use blocks emitted this turn
    tool_names: tuple[str, ...] = ()       # distinct tool names this turn


@dataclass
class CacheBreak:
    """A turn where cache efficiency dropped sharply vs the prior turn."""

    turn_idx: int
    before_efficiency: float
    after_efficiency: float
    drop_pp: float
    tokens_lost_estimate: int              # what cache_read WOULD have been
    attribution: str
    suggestion: str


@dataclass
class StaticLintFinding:
    """One regex hit in a system prompt / tool catalog string."""

    position: int
    pattern_name: str
    matched_excerpt: str                   # ≤ STATIC_EXCERPT_MAX_CHARS
    severity: Severity
    suggestion: str


@dataclass
class CacheLintReport:
    """Combined diagnostic — flat shape suitable for JSON / CLI output."""

    transcript_path: str | None = None
    n_turns: int = 0
    turns: list[TurnEfficiency] = field(default_factory=list)
    breaks: list[CacheBreak] = field(default_factory=list)
    static_findings: list[StaticLintFinding] = field(default_factory=list)

    observed_cache_hit_rate: float = 0.0
    theoretical_max_cache_hit_rate: float = 0.0
    potential_token_savings: int = 0


# ──────────────────────────────────────────────────────────────────────
# Static lint — anti-pattern catalog
# ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _AntiPattern:
    name: str
    regex: re.Pattern[str]
    severity: Severity
    suggestion: str


_PATTERNS: tuple[_AntiPattern, ...] = (
    _AntiPattern(
        name="date_iso",
        regex=re.compile(r"\b\d{4}-\d{2}-\d{2}\b"),
        severity="warning",
        suggestion=(
            "ISO date in stable region — invalidates cache daily at "
            "midnight. Move below the cache_control marker or into "
            "the user message."
        ),
    ),
    _AntiPattern(
        name="time_of_day",
        regex=re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?(?:\s*[AaPp][Mm])?\b"),
        severity="warning",
        suggestion=(
            "Time-of-day string — value changes every request and "
            "invalidates the entire prefix above this point."
        ),
    ),
    _AntiPattern(
        name="uuid",
        regex=re.compile(
            r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
            r"[0-9a-f]{4}-[0-9a-f]{12}\b",
            re.I,
        ),
        severity="error",
        suggestion=(
            "UUID — random per request → 0 % cache hit possible above "
            "this point. Move out of the cached region (into the user "
            "message or tool input)."
        ),
    ),
    _AntiPattern(
        name="today_phrase",
        regex=re.compile(r"\bToday is\b", re.I),
        severity="warning",
        suggestion=(
            "Date-bound phrase. The line that contains it likely also "
            "carries a date string — both belong below cache_control."
        ),
    ),
    _AntiPattern(
        name="current_time_phrase",
        regex=re.compile(
            r"\b(Current time|Generated at|Timestamp|Right now|As of)\b",
            re.I,
        ),
        severity="warning",
        suggestion=(
            "Time-bound preamble — content this introduces will change "
            "every request and break the cache."
        ),
    ),
    _AntiPattern(
        name="epoch_ms",
        regex=re.compile(r"\b\d{13}\b"),
        severity="info",
        suggestion=(
            "13-digit number — looks like an epoch-ms timestamp. If it "
            "is, it changes per request and breaks the cache."
        ),
    ),
    _AntiPattern(
        name="request_id",
        regex=re.compile(r"\brequest[_-]id\b", re.I),
        severity="warning",
        suggestion=(
            "Likely a per-request identifier; cached region should not "
            "contain request-scoped fields."
        ),
    ),
)


def analyze_system_prompt(text: str) -> list[StaticLintFinding]:
    """Apply every anti-pattern regex to ``text`` and return findings.

    Findings are returned in document order. Multiple matches of the
    same pattern produce multiple findings (each with its own offset).
    """
    findings: list[StaticLintFinding] = []
    if not text:
        return findings
    for pat in _PATTERNS:
        for m in pat.regex.finditer(text):
            raw_match = m.group(0)
            # Redact secret-shaped patterns BEFORE truncation — so even
            # if the redaction is verbose we still respect the size cap.
            excerpt = _redact_excerpt(pat.name, raw_match)
            if len(excerpt) > STATIC_EXCERPT_MAX_CHARS:
                excerpt = excerpt[: STATIC_EXCERPT_MAX_CHARS - 1] + "…"
            findings.append(
                StaticLintFinding(
                    position=m.start(),
                    pattern_name=pat.name,
                    matched_excerpt=excerpt,
                    severity=pat.severity,
                    suggestion=pat.suggestion,
                )
            )
    findings.sort(key=lambda f: f.position)
    return findings


# ──────────────────────────────────────────────────────────────────────
# Dynamic lint — transcript walk
# ──────────────────────────────────────────────────────────────────────


def _stream_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    """Yield each well-formed JSON record from a JSONL file. Skips
    blank lines and records that fail to parse (matches the
    transcript_reader contract — never crash on malformed input)."""
    try:
        fh = path.open(encoding="utf-8")
    except OSError:
        return
    with fh:
        for raw in fh:
            line = raw.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _is_assistant(rec: dict[str, Any]) -> bool:
    kind = rec.get("type") or rec.get("role") or ""
    return kind in ("assistant", "assistant_message", "model_response", "claude")


def _extract_usage(rec: dict[str, Any]) -> dict[str, int] | None:
    raw_msg = rec.get("message")
    msg: dict[str, Any] = raw_msg if isinstance(raw_msg, dict) else {}
    u = msg.get("usage") or rec.get("usage")
    if not isinstance(u, dict):
        return None
    return {
        "input": int(u.get("input_tokens", 0) or 0),
        "output": int(u.get("output_tokens", 0) or 0),
        "cache_read": int(u.get("cache_read_input_tokens", 0) or 0),
        "cache_creation": int(u.get("cache_creation_input_tokens", 0) or 0),
    }


def _extract_tool_names(rec: dict[str, Any]) -> tuple[str, ...]:
    """Distinct tool_use names emitted by this assistant turn."""
    raw_msg = rec.get("message")
    msg: dict[str, Any] = raw_msg if isinstance(raw_msg, dict) else {}
    content = msg.get("content") or []
    names: list[str] = []
    if isinstance(content, list):
        for c in content:
            if isinstance(c, dict) and c.get("type") == "tool_use":
                name = str(c.get("name", ""))
                if name and name not in names:
                    names.append(name)
    return tuple(names)


def _walk_assistant_turns(path: Path) -> list[TurnEfficiency]:
    """Build per-assistant-turn TurnEfficiency rows."""
    rows: list[TurnEfficiency] = []
    for rec in _stream_jsonl(path):
        if not _is_assistant(rec):
            continue
        u = _extract_usage(rec)
        if u is None:
            continue
        total = u["input"] + u["cache_read"] + u["cache_creation"]
        eff = (u["cache_read"] / total) if total > 0 else 0.0
        names = _extract_tool_names(rec)
        rows.append(
            TurnEfficiency(
                turn_idx=len(rows),
                input_tokens=u["input"],
                cache_read=u["cache_read"],
                cache_creation=u["cache_creation"],
                output_tokens=u["output"],
                total_input=total,
                efficiency=eff,
                n_tool_uses=len(names),
                tool_names=names,
            )
        )
    return rows


def _attribute_break(
    prev: TurnEfficiency,
    curr: TurnEfficiency,
    *,
    seen_tools: set[str],
) -> tuple[str, str]:
    """Heuristic: what changed between ``prev`` and ``curr`` that most
    plausibly invalidated the cached prefix?

    ``seen_tools`` is the cumulative set of tool names emitted in
    every turn before ``curr`` — so we only flag a tool as "new" when
    it appears for the FIRST TIME in the entire session, not merely
    different from the prior turn.

    Returns (attribution, suggestion) — both human-readable strings."""
    # 1. Genuinely new tool name (first appearance in the session)?
    new_tools = [n for n in curr.tool_names if n not in seen_tools]
    if new_tools:
        return (
            f"new tool registered ({', '.join(new_tools)}) — tool "
            "catalog hash changed, invalidating the entire prefix",
            "Register all MCP servers / tools at session start, "
            "and place a `cache_control` marker AFTER the tool catalog "
            "block so subsequent turns reuse the cached system + tools.",
        )

    # 2. Sudden growth of input_tokens when prior turns stayed small —
    # something injected dynamic content into the prompt prefix.
    if curr.input_tokens > 4 * max(prev.input_tokens, 1):
        return (
            f"input_tokens jumped from {prev.input_tokens:,} to "
            f"{curr.input_tokens:,} — large dynamic content was "
            "inserted ABOVE the cached region",
            "Inspect the system prompt or pre-history block for "
            "newly-injected text (dates, IDs, dynamic tool config). "
            "Move it below cache_control.",
        )

    # 3. cache_creation spiked while cache_read collapsed — classic
    # "old prefix expired or was overwritten" case.
    if curr.cache_creation > 2 * prev.cache_creation and curr.cache_creation > 1000:
        return (
            "cache_creation spiked — Anthropic re-cached a large new "
            "prefix this turn, suggesting the prior prefix was "
            "evicted (TTL > 5 min) or substantively changed",
            "If sessions are long-running, consider extending cache "
            "TTL via Anthropic's 1-hour cache feature, or pace tool "
            "calls so prefix is touched within 5 minutes.",
        )

    # 4. Default fallback.
    return (
        "no obvious change between turns — possibly silent prompt "
        "drift (whitespace / ordering / formatting)",
        "Diff the request bodies of turns N-1 and N; the smallest "
        "byte change above the cache_control marker invalidates "
        "everything below it.",
    )


def _detect_breaks(
    turns: list[TurnEfficiency], *, threshold_pp: float,
) -> list[CacheBreak]:
    """Walk consecutive turn pairs; flag those whose efficiency
    dropped by ≥ ``threshold_pp`` percentage points.

    Tracks the cumulative tool-name set as it walks forward so the
    "new tool registered" attribution only fires on a genuinely-new
    tool, not one that's merely absent from the IMMEDIATELY PREVIOUS
    turn.
    """
    breaks: list[CacheBreak] = []
    if len(turns) < 2:
        return breaks
    threshold_ratio = threshold_pp / 100.0
    seen_tools: set[str] = set(turns[0].tool_names)
    for i in range(1, len(turns)):
        prev = turns[i - 1]
        curr = turns[i]
        # Only flag if there was meaningful prior efficiency to lose.
        if prev.efficiency >= 0.40:
            drop = prev.efficiency - curr.efficiency
            if drop >= threshold_ratio:
                # Token savings: what cache_read WOULD have been at
                # prev's efficiency.
                expected_cache_read = int(prev.efficiency * curr.total_input)
                tokens_lost = max(0, expected_cache_read - curr.cache_read)
                attribution, suggestion = _attribute_break(
                    prev, curr, seen_tools=seen_tools,
                )
                breaks.append(
                    CacheBreak(
                        turn_idx=curr.turn_idx,
                        before_efficiency=prev.efficiency,
                        after_efficiency=curr.efficiency,
                        drop_pp=drop * 100.0,
                        tokens_lost_estimate=tokens_lost,
                        attribution=attribution,
                        suggestion=suggestion,
                    )
                )
        # Update cumulative tool-set AFTER attribution — so attribution
        # sees the tool-set as it stood at break time.
        seen_tools.update(curr.tool_names)
    return breaks


def _aggregate(
    turns: list[TurnEfficiency], breaks: list[CacheBreak],
) -> tuple[float, float, int]:
    """Returns (observed_hit_rate, theoretical_max, potential_savings)."""
    total_in = sum(t.total_input for t in turns)
    total_cache_read = sum(t.cache_read for t in turns)
    if total_in == 0:
        return 0.0, 0.0, 0
    observed = total_cache_read / total_in
    potential = sum(b.tokens_lost_estimate for b in breaks)
    theoretical_total_read = total_cache_read + potential
    theoretical_max = min(1.0, theoretical_total_read / total_in)
    return observed, theoretical_max, potential


def analyze_transcript(
    transcript_path: Path,
    *,
    break_threshold_pp: float = DEFAULT_BREAK_THRESHOLD_PP,
    system_prompt: str | None = None,
) -> CacheLintReport:
    """Build a CacheLintReport for one Claude Code transcript.

    ``system_prompt`` is optional — when given, the static lint runs
    over that string and findings are folded into the same report.
    Belt-and-braces: missing / unreadable transcript yields an empty
    report rather than raising.
    """
    turns = _walk_assistant_turns(transcript_path)
    breaks = _detect_breaks(turns, threshold_pp=break_threshold_pp)
    observed, theoretical, potential = _aggregate(turns, breaks)

    static_findings: list[StaticLintFinding] = []
    if system_prompt:
        static_findings = analyze_system_prompt(system_prompt)

    return CacheLintReport(
        transcript_path=str(transcript_path) if transcript_path else None,
        n_turns=len(turns),
        turns=turns,
        breaks=breaks,
        static_findings=static_findings,
        observed_cache_hit_rate=observed,
        theoretical_max_cache_hit_rate=theoretical,
        potential_token_savings=potential,
    )


# ──────────────────────────────────────────────────────────────────────
# Serialisation
# ──────────────────────────────────────────────────────────────────────


def report_to_dict(report: CacheLintReport) -> dict[str, Any]:
    """Flat dict suitable for `aegis cache-lint --json`."""
    return {
        "transcript_path": report.transcript_path,
        "n_turns": report.n_turns,
        "observed_cache_hit_rate": report.observed_cache_hit_rate,
        "theoretical_max_cache_hit_rate": (
            report.theoretical_max_cache_hit_rate
        ),
        "potential_token_savings": report.potential_token_savings,
        "turns": [asdict(t) for t in report.turns],
        "breaks": [asdict(b) for b in report.breaks],
        "static_findings": [asdict(s) for s in report.static_findings],
    }
