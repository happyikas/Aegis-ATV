"""CLI entry: ``python -m demo.sllm_rag_eval``."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .cases import cases as load_cases
from .render import render_markdown_report, render_terminal_summary
from .runner import (
    DEFAULT_CONFIGURATIONS,
    Configuration,
    run_configuration,
)

_REPO = Path(__file__).resolve().parents[2]
_DEFAULT_REPORT = _REPO / "docs" / "RAG_SLLM_BENCHMARK_REPORT.md"


def _parse(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="python -m demo.sllm_rag_eval",
        description=(
            "30-case RAG + sLLM benchmark for the v3.0 judge stack."
        ),
    )
    p.add_argument(
        "--mode", default="all",
        help=(
            "Comma-separated config slugs to run, or 'all'. "
            "Available: dummy-norag, dummy-rag, sllm-norag, sllm-rag, "
            "haiku-norag, haiku-rag."
        ),
    )
    p.add_argument(
        "--limit", type=int, default=0,
        help="run only the first N cases (default: 0 = all 30)",
    )
    p.add_argument(
        "--report", type=Path, default=_DEFAULT_REPORT,
        help="markdown report output path",
    )
    return p.parse_args(argv)


def _select_configs(mode: str) -> list[Configuration]:
    if mode == "all":
        return list(DEFAULT_CONFIGURATIONS)
    requested = {s.strip() for s in mode.split(",") if s.strip()}
    by_slug = {c.slug: c for c in DEFAULT_CONFIGURATIONS}
    out: list[Configuration] = []
    for slug in requested:
        if slug not in by_slug:
            raise SystemExit(
                f"unknown config slug {slug!r}. Available: "
                + ", ".join(by_slug)
            )
        out.append(by_slug[slug])
    return out


def main(argv: list[str] | None = None) -> int:
    ns = _parse(argv if argv is not None else sys.argv[1:])
    configs = _select_configs(ns.mode)
    cases = load_cases()
    if ns.limit > 0:
        cases = cases[: ns.limit]

    print(f"\n[sllm-rag-eval] {len(cases)} cases × "
          f"{len(configs)} configurations\n")

    reports = []
    for cfg in configs:
        print(f"  running {cfg.slug} …")
        rep = run_configuration(cfg, cases)
        reports.append(rep)
        if rep.skipped:
            print(f"    skipped: {rep.skip_reason}")
        else:
            print(
                f"    {rep.n_correct}/{rep.n_total} correct "
                f"(accuracy {rep.accuracy*100:.0f}%, "
                f"recall {rep.mean_recall*100:.0f}%, "
                f"{rep.total_ms/max(rep.n_total,1):.0f} ms/case)"
            )
    print()

    print(render_terminal_summary(reports))

    ns.report.parent.mkdir(parents=True, exist_ok=True)
    ns.report.write_text(
        render_markdown_report(cases, reports), encoding="utf-8",
    )
    print(f"\n[sllm-rag-eval] report → {ns.report}\n")

    runnable = [r for r in reports if not r.skipped]
    if not runnable:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
