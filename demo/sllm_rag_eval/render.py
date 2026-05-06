"""Terminal + Markdown rendering for the 30-case benchmark."""
from __future__ import annotations

from collections import Counter

from .cases import BenchmarkCase
from .runner import CaseResult, ConfigurationReport

# ── Terminal rendering ────────────────────────────────────────────────


def render_terminal_summary(reports: list[ConfigurationReport]) -> str:
    lines: list[str] = []
    bar = "═" * 78
    lines.append(bar)
    lines.append("  Aegis RAG + sLLM evaluation — 30-case benchmark")
    lines.append(bar)
    lines.append("")

    runnable = [r for r in reports if not r.skipped]
    skipped = [r for r in reports if r.skipped]

    lines.append(
        f"{'config':<20} {'cases':>6} {'correct':>8} {'acc':>6} "
        f"{'recall':>7} {'mean ms':>8}"
    )
    lines.append("─" * 78)
    for r in runnable:
        mean_ms = r.total_ms / r.n_total if r.n_total else 0.0
        lines.append(
            f"{r.config.slug:<20} {r.n_total:>6} {r.n_correct:>8} "
            f"{r.accuracy * 100:>5.0f}% {r.mean_recall * 100:>6.0f}% "
            f"{mean_ms:>7.1f}"
        )
    if skipped:
        lines.append("")
        lines.append("Skipped configurations:")
        for r in skipped:
            lines.append(f"  - {r.config.slug:<20} ({r.skip_reason})")

    if not runnable:
        lines.append("(no configurations runnable in this environment)")
        return "\n".join(lines)

    lines.append("")
    lines.append("Per-case predictions (✓ = correct)")
    lines.append("─" * 78)

    case_ids = [r.cid for r in runnable[0].results]
    header = f"  {'case':<8} {'expected':<18}"
    for r in runnable:
        header += f" {r.config.slug[:14]:<14}"
    lines.append(header)
    by_cid: dict[str, dict[str, CaseResult]] = {}
    for rep in runnable:
        for res in rep.results:
            by_cid.setdefault(res.cid, {})[rep.config.slug] = res

    for cid in case_ids:
        row = by_cid[cid]
        first = next(iter(row.values()))
        cells = f"  {cid:<8} {first.expected:<18}"
        for rep in runnable:
            case_res = row.get(rep.config.slug)
            if case_res is None:
                cells += f" {'-':<14}"
                continue
            mark = "✓" if case_res.correct else "✗"
            cells += f" {mark} {case_res.predicted[:11]:<12}"
        lines.append(cells)

    lines.append("")
    lines.append("Per-difficulty accuracy")
    lines.append("─" * 78)
    diff_table = _difficulty_table(runnable)
    lines.extend(diff_table)

    lines.append("")
    lines.append(bar)
    return "\n".join(lines)


def _difficulty_table(reports: list[ConfigurationReport]) -> list[str]:
    """Group case results by difficulty, per configuration."""
    from .cases import cases as _cases
    case_to_difficulty = {c.cid: c.difficulty for c in _cases()}
    out: list[str] = []
    header = f"  {'difficulty':<12}"
    for r in reports:
        header += f" {r.config.slug[:14]:<14}"
    out.append(header)
    for difficulty in ("easy", "medium", "hard"):
        row = f"  {difficulty:<12}"
        for r in reports:
            n = correct = 0
            for res in r.results:
                if case_to_difficulty.get(res.cid) == difficulty:
                    n += 1
                    if res.correct:
                        correct += 1
            pct = (correct / n * 100) if n else 0
            row += f" {correct}/{n} ({pct:.0f}%) ".ljust(15)
        out.append(row)
    return out


# ── Markdown rendering ────────────────────────────────────────────────


def render_markdown_report(
    cases: list[BenchmarkCase], reports: list[ConfigurationReport],
) -> str:
    runnable = [r for r in reports if not r.skipped]
    skipped = [r for r in reports if r.skipped]

    lines: list[str] = [
        "# Aegis RAG + sLLM 30-Case Benchmark Report",
        "",
        "Driver: `python -m demo.sllm_rag_eval`",
        "",
        "Self-contained 30-case benchmark for the v3.0 RAG-grounded "
        "judge stack. Each case is a natural-language tool-call "
        "summary with a ground-truth verdict (BLOCK / "
        "REQUIRE_APPROVAL / ALLOW) and the chunk IDs we expect RAG "
        "to retrieve. The runner drives every case through every "
        "configured (judge × RAG) combination and reports accuracy, "
        "retrieval recall, and per-case predictions.",
        "",
        "## Configurations exercised",
        "",
        "| Slug | Description | Status |",
        "|------|-------------|--------|",
    ]
    for r in reports:
        status = "skipped" if r.skipped else "ran"
        if r.skipped:
            lines.append(
                f"| `{r.config.slug}` | {r.config.description} | "
                f"skipped ({r.skip_reason}) |"
            )
        else:
            lines.append(
                f"| `{r.config.slug}` | {r.config.description} | {status} |"
            )
    lines.append("")

    if runnable:
        lines += [
            "## Headline accuracy",
            "",
            "| config | cases | correct | accuracy | retrieval recall | mean ms |",
            "|--------|-------|---------|----------|------------------|---------|",
        ]
        for r in runnable:
            mean_ms = r.total_ms / r.n_total if r.n_total else 0.0
            lines.append(
                f"| `{r.config.slug}` | {r.n_total} | {r.n_correct} | "
                f"{r.accuracy*100:.0f}% | {r.mean_recall*100:.0f}% | "
                f"{mean_ms:.1f} |"
            )
        lines.append("")

        lines += [
            "## Per-difficulty accuracy",
            "",
            "| difficulty | " + " | ".join(
                r.config.slug for r in runnable
            ) + " |",
            "|------------|" + "|".join("---" for _ in runnable) + "|",
        ]
        case_to_difficulty = {c.cid: c.difficulty for c in cases}
        for diff in ("easy", "medium", "hard"):
            cells = [diff]
            for r in runnable:
                n = correct = 0
                for res in r.results:
                    if case_to_difficulty.get(res.cid) == diff:
                        n += 1
                        if res.correct:
                            correct += 1
                pct = (correct / n * 100) if n else 0
                cells.append(f"{correct}/{n} ({pct:.0f}%)")
            lines.append("| " + " | ".join(cells) + " |")
        lines.append("")

    lines += [
        "## Per-case predictions",
        "",
        "| case | expected | summary | "
        + " | ".join(r.config.slug for r in runnable)
        + " |",
        "|------|----------|---------|"
        + "|".join("---" for _ in runnable)
        + "|",
    ]
    by_cid_rep: dict[str, dict[str, CaseResult]] = {}
    for rep in runnable:
        for res in rep.results:
            by_cid_rep.setdefault(res.cid, {})[rep.config.slug] = res
    for c in cases:
        cells = [
            f"`{c.cid}`",
            f"`{c.expected_decision}`",
            c.summary[:60] + ("…" if len(c.summary) > 60 else ""),
        ]
        for rep in runnable:
            case_res = by_cid_rep.get(c.cid, {}).get(rep.config.slug)
            if case_res is None:
                cells.append("—")
            else:
                glyph = "✓" if case_res.correct else "✗"
                cells.append(f"{glyph} `{case_res.predicted}`")
        lines.append("| " + " | ".join(cells) + " |")
    lines.append("")

    if any(r.config.rag_enabled and not r.skipped for r in reports):
        lines += [
            "## Retrieval analysis (RAG-enabled configurations)",
            "",
            "Per-case: which expected chunks were actually retrieved.",
            "",
        ]
        for rep in runnable:
            if not rep.config.rag_enabled:
                continue
            lines.append(f"### `{rep.config.slug}`")
            lines.append("")
            lines.append("| case | expected chunks | retrieved | recall |")
            lines.append("|------|-----------------|-----------|--------|")
            for res in rep.results:
                expected = ",".join(
                    next(c.expected_chunk_ids for c in cases if c.cid == res.cid)
                ) or "—"
                retrieved = ",".join(res.retrieved_chunk_ids[:2])
                if len(res.retrieved_chunk_ids) > 2:
                    retrieved += f",… ({len(res.retrieved_chunk_ids) - 2} more)"
                retrieved = retrieved or "—"
                lines.append(
                    f"| `{res.cid}` | {expected} | {retrieved} | "
                    f"{res.retrieval_recall*100:.0f}% |"
                )
            lines.append("")

    if skipped:
        lines += ["## Skipped configurations", ""]
        for r in skipped:
            lines.append(
                f"- `{r.config.slug}` — {r.skip_reason}"
            )
        lines.append("")
        lines += [
            "To run the full matrix:",
            "",
            "```bash",
            "# Local sLLM judge (Phi-3.5-mini recommended for RAG):",
            "uv run aegis pull-model --model phi3-mini",
            "export AEGIS_JUDGE_MODEL_PATH=$PWD/models/Phi-3.5-mini-instruct-Q4_K_M.gguf",
            "",
            "# Cloud Haiku judge:",
            "export ANTHROPIC_API_KEY=sk-ant-...",
            "",
            "# Re-run:",
            "uv run python -m demo.sllm_rag_eval",
            "```",
            "",
        ]

    # Decision distribution per config
    if runnable:
        lines += [
            "## Decision distribution",
            "",
            "How many ALLOW / BLOCK / REQUIRE_APPROVAL each judge "
            "configuration produced (ground truth: 10 each).",
            "",
            "| config | ALLOW | BLOCK | REQUIRE_APPROVAL | ERROR |",
            "|--------|-------|-------|------------------|-------|",
        ]
        for rep in runnable:
            counts: Counter[str] = Counter(r.predicted for r in rep.results)
            lines.append(
                f"| `{rep.config.slug}` | {counts.get('ALLOW', 0)} | "
                f"{counts.get('BLOCK', 0)} | "
                f"{counts.get('REQUIRE_APPROVAL', 0)} | "
                f"{counts.get('ERROR', 0)} |"
            )
        lines.append("")

    # Analysis hooks — auto-derive the key insights from the data.
    if runnable:
        lines += _render_analysis_section(cases, runnable)

    return "\n".join(lines)


def _render_analysis_section(
    cases: list[BenchmarkCase],
    runnable: list[ConfigurationReport],
) -> list[str]:
    """Auto-derived findings: which configs do best / worst, and where
    RAG helps vs hurts. Pure read of the data — no opinions baked in."""
    out: list[str] = ["## Analysis", ""]

    # Best / worst overall
    by_acc = sorted(runnable, key=lambda r: r.accuracy, reverse=True)
    out.append(
        f"- **Best accuracy**: `{by_acc[0].config.slug}` at "
        f"{by_acc[0].accuracy*100:.0f}% ({by_acc[0].n_correct}/"
        f"{by_acc[0].n_total} correct)."
    )
    if len(by_acc) > 1:
        out.append(
            f"- **Worst accuracy**: `{by_acc[-1].config.slug}` at "
            f"{by_acc[-1].accuracy*100:.0f}%."
        )

    # RAG delta — pair each (judge, norag) with its (judge, rag) twin.
    by_slug = {r.config.slug: r for r in runnable}
    rag_deltas: list[tuple[str, float, float]] = []
    for r in runnable:
        if r.config.rag_enabled:
            twin = by_slug.get(f"{r.config.judge}-norag")
            if twin and not twin.skipped:
                rag_deltas.append((r.config.judge, twin.accuracy, r.accuracy))
    if rag_deltas:
        out.append("- **RAG accuracy delta** (with-RAG minus without-RAG):")
        for judge, before, after in rag_deltas:
            sign = "+" if after >= before else ""
            out.append(
                f"  - `{judge}`: {before*100:.0f}% → {after*100:.0f}% "
                f"({sign}{(after-before)*100:.0f} pp)"
            )

    # Per-difficulty winner
    case_to_difficulty = {c.cid: c.difficulty for c in cases}
    out.append("- **Per-difficulty winner**:")
    for diff in ("easy", "medium", "hard"):
        per_config: list[tuple[str, int, int]] = []
        for r in runnable:
            n = correct = 0
            for res in r.results:
                if case_to_difficulty.get(res.cid) == diff:
                    n += 1
                    if res.correct:
                        correct += 1
            per_config.append((r.config.slug, correct, n))
        per_config.sort(key=lambda x: -(x[1] / x[2] if x[2] else 0))
        slug, c, n = per_config[0]
        pct = (c / n * 100) if n else 0
        out.append(
            f"  - `{diff}` cases: best is `{slug}` at "
            f"{c}/{n} ({pct:.0f}%)"
        )

    # Over- / under-cautious diagnosis
    out.append("- **Decision bias** (vs ground truth: 10 ALLOW / 10 BLOCK / 10 REQUIRE_APPROVAL):")
    for r in runnable:
        counts: Counter[str] = Counter(res.predicted for res in r.results)
        allows = counts.get("ALLOW", 0)
        blocks = counts.get("BLOCK", 0)
        approvals = counts.get("REQUIRE_APPROVAL", 0)
        if allows >= 25:
            label = "extremely permissive (always ALLOW)"
        elif blocks >= 20:
            label = "very strict (over-blocks)"
        elif approvals >= 20:
            label = "over-cautious (over-approves)"
        elif allows == 0 and blocks + approvals == 30:
            label = "all-flagging (no ALLOW)"
        else:
            label = "balanced"
        out.append(
            f"  - `{r.config.slug}`: ALLOW={allows} BLOCK={blocks} "
            f"REQUIRE_APPROVAL={approvals} → {label}"
        )

    out.append("")
    return out
