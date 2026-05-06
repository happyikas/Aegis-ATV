"""Terminal + Markdown rendering for the Mac mini validation suite.

Pure formatting — no I/O is performed here. Both renderers consume
``TestCase`` and ``TestResult`` lists and return strings.
"""
from __future__ import annotations

import os
from collections import Counter
from collections.abc import Iterable
from typing import TextIO

from .case import TestCase, TestResult

# ANSI styling (auto-disabled when stdout is not a TTY or NO_COLOR is set).

_USE_COLOR_DEFAULT = os.environ.get("NO_COLOR") is None


def _color(use: bool, code: str, text: str) -> str:
    if not use:
        return text
    return f"\x1b[{code}m{text}\x1b[0m"


def _bold(use: bool, text: str) -> str:
    return _color(use, "1", text)


def _dim(use: bool, text: str) -> str:
    return _color(use, "2", text)


def _green(use: bool, text: str) -> str:
    return _color(use, "32", text)


def _red(use: bool, text: str) -> str:
    return _color(use, "31", text)


def _cyan(use: bool, text: str) -> str:
    return _color(use, "36", text)


def _yellow(use: bool, text: str) -> str:
    return _color(use, "33", text)


# Word-wrap helper — simple greedy wrap, no hyphenation.

def _wrap(text: str, width: int, indent: str = "") -> list[str]:
    if not text:
        return [indent]
    words = text.split()
    lines: list[str] = []
    cur = indent
    for w in words:
        if not cur.strip():
            cur = indent + w
        elif len(cur) + 1 + len(w) <= width:
            cur += " " + w
        else:
            lines.append(cur)
            cur = indent + w
    if cur.strip():
        lines.append(cur)
    return lines


# Per-case terminal rendering


_DIVIDER = "═"
_SUBDIVIDER = "─"


def render_case(
    case: TestCase, result: TestResult, *, width: int = 78, color: bool | None = None,
) -> str:
    use_color = _USE_COLOR_DEFAULT if color is None else color
    bar = _DIVIDER * width
    sub = _SUBDIVIDER * width
    status_label = "PASS" if result.passed else "FAIL"
    status_glyph = "✓" if result.passed else "✗"
    status_colored = (
        _green(use_color, f"{status_glyph} {status_label}")
        if result.passed
        else _red(use_color, f"{status_glyph} {status_label}")
    )

    title_line = f"{_bold(use_color, case.cid)}  │  {case.title}"

    lines: list[str] = [
        bar,
        title_line,
        sub,
    ]

    lines.append(_cyan(use_color, "SCENARIO"))
    lines.extend(_wrap(case.scenario, width=width - 2, indent="  "))
    lines.append("")

    lines.append(_cyan(use_color, "EXECUTION"))
    exec_summary = case.execution_summary or (
        f"compose_advice_heuristic({case.test_type})"
    )
    lines.extend(_wrap(exec_summary, width=width - 2, indent="  "))
    lines.append("")

    lines.append(_cyan(use_color, "RESULT") + f"   {status_colored}")
    lines.append(
        f"  decision      = {result.decision!r}  "
        + _dim(use_color, f"({result.duration_ms:.1f} ms)")
    )

    if not result.advisors:
        lines.append("  advisors      = (none)")
    else:
        lines.append(
            f"  advisors      = {len(result.advisors)} firing"
        )
        for a in result.advisors[:3]:
            lines.append(
                "    - " + _bold(use_color, str(a["advisor"]))
                + f"  prio={a['priority']}"
                + f"  verbs={','.join(a['verbs']) or '(none)'}"
            )
            steps = a.get("steps", []) or []
            if steps:
                top = steps[0]
                params = top.get("parameters") or {}
                params_brief = ", ".join(
                    f"{k}={_truncate(repr(v), 28)}" for k, v in list(params.items())[:2]
                ) or "(no params)"
                lines.append(_dim(use_color, f"      step: {top.get('verb')}({params_brief})"))
                impact = top.get("expected_impact") or ""
                if impact:
                    impact_short = impact if len(impact) <= width - 14 else impact[: width - 17] + "..."
                    lines.append(_dim(use_color, f"      impact: {impact_short}"))
        if len(result.advisors) > 3:
            lines.append(
                _dim(use_color, f"    + {len(result.advisors) - 3} more")
            )

    if not result.passed:
        lines.append("")
        lines.append(_red(use_color, "  MISSES"))
        for m in result.misses:
            lines.extend(_wrap(m, width=width - 4, indent="    - "))

    lines.append(bar)
    return "\n".join(lines)


def _truncate(s: str, n: int) -> str:
    if len(s) <= n:
        return s
    return s[: n - 1] + "…"


# Group + headline summary

def render_summary(
    results: Iterable[TestResult], *, color: bool | None = None,
) -> str:
    use_color = _USE_COLOR_DEFAULT if color is None else color
    rs = list(results)
    by_cat: dict[str, list[TestResult]] = {}
    for r in rs:
        by_cat.setdefault(r.category, []).append(r)
    n_total = len(rs)
    n_pass = sum(1 for r in rs if r.passed)

    lines: list[str] = []
    lines.append(_bold(use_color, "═" * 78))
    lines.append(
        _bold(use_color, "  Aegis Mac mini validation — summary")
    )
    lines.append(_bold(use_color, "═" * 78))
    headline = (
        f"  Total: {n_total}    "
        f"Pass: {_green(use_color, str(n_pass))}    "
        f"Fail: {_red(use_color, str(n_total - n_pass))}    "
        f"Pass-rate: {n_pass / max(n_total, 1) * 100:.0f}%"
    )
    lines.append(headline)
    lines.append("")
    lines.append("  By category")
    lines.append(
        f"    {'Category':<14} {'Cases':>6} {'Pass':>6} {'Fail':>6} {'Pass%':>7}"
    )
    for cat in ("cost", "performance", "security"):
        cases = by_cat.get(cat, [])
        cp = sum(1 for r in cases if r.passed)
        if not cases:
            continue
        lines.append(
            f"    {cat:<14} {len(cases):>6} "
            f"{_green(use_color, f'{cp:>6}')} "
            f"{_red(use_color, f'{len(cases) - cp:>6}') if len(cases) - cp else f'{0:>6}'} "
            f"{cp / len(cases) * 100:>6.0f}%"
        )
    lines.append("")

    advisor_freq: Counter[str] = Counter()
    verb_freq: Counter[str] = Counter()
    for r in rs:
        for a in r.advisors:
            advisor_freq[a["advisor"]] += 1
            for v in a["verbs"]:
                verb_freq[v] += 1
    if advisor_freq:
        lines.append("  Advisor frequency (across all results)")
        for adv, cnt in advisor_freq.most_common():
            lines.append(f"    {adv:<22} {cnt:>3}")
        lines.append("")
    if verb_freq:
        lines.append("  Verb frequency (across all action_steps)")
        for verb, cnt in verb_freq.most_common():
            lines.append(f"    {verb:<22} {cnt:>3}")
        lines.append("")

    fails = [r for r in rs if not r.passed]
    if fails:
        lines.append(_red(use_color, "  Failures"))
        for r in fails:
            lines.append(f"    {r.cid:<10} {r.title[:50]:<50}")
            for m in r.misses:
                lines.append(f"      - {m}")
        lines.append("")

    lines.append(_bold(use_color, "═" * 78))
    return "\n".join(lines)


# Markdown rendering

def render_markdown(
    cases: list[TestCase], results: list[TestResult],
) -> str:
    by_cat: dict[str, list[tuple[TestCase, TestResult]]] = {
        "cost": [], "performance": [], "security": [],
    }
    pairs = list(zip(cases, results, strict=False))
    for c, r in pairs:
        by_cat.setdefault(c.category, []).append((c, r))

    n_total = len(pairs)
    n_pass = sum(1 for _, r in pairs if r.passed)

    lines: list[str] = [
        "# Aegis Mac mini Validation Report",
        "",
        "Driver: `python -m demo.macmini all`",
        "",
        "Self-contained 90-case validation suite covering Cost, "
        "Performance and Security advisors.",
        "",
        "## Headline",
        "",
        f"- **Total cases**: {n_total}",
        f"- **Pass**: {n_pass} ({n_pass / max(n_total, 1) * 100:.0f}%)",
        f"- **Fail**: {n_total - n_pass}",
        "",
        "## By category",
        "",
        "| Category | Cases | Pass | Fail | Pass% |",
        "|----------|-------|------|------|-------|",
    ]
    for cat in ("cost", "performance", "security"):
        cs = by_cat.get(cat, [])
        if not cs:
            continue
        cp = sum(1 for _, r in cs if r.passed)
        lines.append(
            f"| {cat} | {len(cs)} | {cp} | {len(cs) - cp} | "
            f"{cp / len(cs) * 100:.0f}% |"
        )
    lines.append("")

    advisor_freq: Counter[str] = Counter()
    verb_freq: Counter[str] = Counter()
    for _, r in pairs:
        for a in r.advisors:
            advisor_freq[a["advisor"]] += 1
            for v in a["verbs"]:
                verb_freq[v] += 1

    if advisor_freq:
        lines += ["## Advisor frequency", "",
                  "| Advisor | Count |", "|---------|-------|"]
        for adv_name, cnt in advisor_freq.most_common():
            lines.append(f"| `{adv_name}` | {cnt} |")
        lines.append("")

    if verb_freq:
        lines += ["## Verb frequency", "",
                  "| Verb | Count |", "|------|-------|"]
        for verb, cnt in verb_freq.most_common():
            lines.append(f"| `{verb}` | {cnt} |")
        lines.append("")

    for cat in ("cost", "performance", "security"):
        cs = by_cat.get(cat, [])
        if not cs:
            continue
        lines += [f"## {cat.title()} ({len(cs)} cases)", ""]
        for c, r in cs:
            mark = "PASS" if r.passed else "FAIL"
            lines.append(f"### {c.cid} — {c.title}  ({mark})")
            lines.append("")
            lines.append(f"**Scenario**: {c.scenario}")
            lines.append("")
            lines.append(f"**Execution**: `{c.execution_summary}`")
            lines.append("")
            lines.append(
                f"**Result**: decision=`{r.decision}` "
                f"({r.duration_ms:.1f} ms)"
            )
            if r.advisors:
                for a in r.advisors:
                    verbs = ",".join(a["verbs"]) or "(none)"
                    lines.append(
                        f"- `{a['advisor']}` (prio={a['priority']}) "
                        f"verbs={verbs}"
                    )
            else:
                lines.append("- (no advisor fired)")
            if not r.passed:
                lines.append("")
                lines.append("**Misses**:")
                for m in r.misses:
                    lines.append(f"- {m}")
            lines.append("")

    lines += [
        "## Reproduction",
        "",
        "```bash",
        "uv run python -m demo.macmini all",
        "```",
        "",
        "Or per-category:",
        "",
        "```bash",
        "uv run python -m demo.macmini cost",
        "uv run python -m demo.macmini performance",
        "uv run python -m demo.macmini security",
        "```",
        "",
    ]
    return "\n".join(lines)


def write_stream(text: str, stream: TextIO) -> None:
    stream.write(text)
    stream.write("\n")
    stream.flush()
