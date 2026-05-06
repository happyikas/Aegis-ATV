"""CLI entry point: ``python -m demo.macmini [cost|performance|security|all]``.

Designed for stock Mac mini execution — no API keys, no GPU, no
external services. All providers are pinned to ``dummy`` by
``demo.macmini.runner.setup_environment()``.

Output:

* Live terminal stream — one rich block per case (scenario / execution
  / result), then a headline summary.
* Markdown report at ``docs/MACMINI_VALIDATION_REPORT.md``.

Exit code: 0 if every case passes, 1 otherwise.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .case import TestCase, TestResult
from .render import (
    render_case,
    render_markdown,
    render_summary,
    write_stream,
)
from .runner import _build_cases, run_case, setup_environment

_REPO = Path(__file__).resolve().parents[2]
_DEFAULT_REPORT = _REPO / "docs" / "MACMINI_VALIDATION_REPORT.md"

_VALID = ("cost", "performance", "security", "all")


def _parse(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="python -m demo.macmini",
        description=(
            "Aegis Mac mini validation suite — 90 cases across "
            "Cost / Performance / Security advisors."
        ),
    )
    p.add_argument(
        "category", nargs="?", default="all", choices=_VALID,
        help="which category to run (default: all)",
    )
    p.add_argument(
        "--no-color", action="store_true",
        help="disable ANSI styling in terminal output",
    )
    p.add_argument(
        "--no-cases", action="store_true",
        help="skip per-case detail (summary only)",
    )
    p.add_argument(
        "--report", type=Path, default=_DEFAULT_REPORT,
        help="markdown report output path",
    )
    p.add_argument(
        "--width", type=int, default=78,
        help="terminal output width (default: 78)",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    ns = _parse(argv if argv is not None else sys.argv[1:])
    use_color = None if not ns.no_color else False

    setup_environment()
    cases: list[TestCase] = _build_cases(ns.category)

    write_stream(
        f"\n[macmini] running {len(cases)} cases "
        f"(category={ns.category})",
        sys.stdout,
    )

    results: list[TestResult] = []
    for case in cases:
        result = run_case(case)
        results.append(result)
        if not ns.no_cases:
            write_stream(
                render_case(case, result, width=ns.width, color=use_color),
                sys.stdout,
            )

    write_stream(render_summary(results, color=use_color), sys.stdout)

    ns.report.parent.mkdir(parents=True, exist_ok=True)
    ns.report.write_text(render_markdown(cases, results), encoding="utf-8")
    write_stream(f"\n[macmini] report written → {ns.report}\n", sys.stdout)

    return 0 if all(r.passed for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
