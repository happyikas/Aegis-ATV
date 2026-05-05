"""Aegis-ATV Performance Dashboard Demo
=========================================

Synthesises a realistic 10-session audit chain and renders it
through the same code path as ``aegis status --performance``.

What it shows
-------------

The dashboard rolls up Stop-hook ``session_retrospective`` records
(PR #46) into one operator-facing picture:

* sessions count + time window
* cumulative dollars + tokens (input / output / cache_read / cache_write)
* weighted cache_hit_rate vs per-session-mean cache_hit_rate (the
  two metrics often disagree dramatically — long sessions dominate
  the weighted, while short sessions pull the mean down)
* inefficiency totals (backtracks / redundant / errors / compactions
  / retries)
* per-session distribution (sessions w/ signals, avg cost)
* top inefficient tools (post_analysis-derived)
* suggested next actions (cache_lint, closed-loop comparator)

Run
---

::

    uv run python demo/performance_dashboard_demo.py
    uv run python demo/performance_dashboard_demo.py --keep   # preserve audit
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

# ruff: noqa: E402  -- imports follow sys.path bootstrap above.
from aegis.performance.dashboard import build_performance_summary

# ──────────────────────────────────────────────────────────────────────
# Synthetic audit chain
# ──────────────────────────────────────────────────────────────────────


# 86_400_000_000_000 ns = 1 day
_DAY_NS = 86_400 * 1_000_000_000


def _stop(
    *,
    aid: str, ts_ns: int, dollars: float, hit_rate: float,
    in_tok: float, out_tok: float, cr: float, cc: float,
    n_success: int, n_back: int = 0, n_red: int = 0, n_err: int = 0,
) -> dict[str, Any]:
    return {
        "ts_ns": ts_ns,
        "aid": aid,
        "tool": "(stop)",
        "hook": "Stop",
        "explain": {
            "session_retrospective": {
                "aid": aid,
                "cumulative_billed_dollars": dollars,
                "cache_hit_rate": hit_rate,
                "input_tokens_total": in_tok,
                "output_tokens_total": out_tok,
                "cache_read_tokens_total": cr,
                "cache_creation_tokens_total": cc,
                "n_tool_success": n_success,
                "n_tool_failure": 0,
                "n_backtracks": n_back,
                "n_redundant": n_red,
                "n_is_error": n_err,
            }
        },
    }


def _post(
    *, aid: str, tool: str,
    backtrack: bool = False, redundant: bool = False, is_error: bool = False,
) -> dict[str, Any]:
    pa: dict[str, Any] = {"classification": {"is_error": is_error}}
    if backtrack:
        pa["backtrack"] = {
            "reverted_trace_id": "x", "file_path": "/foo.py",
            "matched_string_hash": "abcd",
        }
    if redundant:
        pa["redundant_of"] = "earlier-trace-id"
    return {
        "aid": aid, "tool": tool, "hook": "PostToolUse",
        "explain": {"post_analysis": pa},
    }


def _precompact(*, aid: str) -> dict[str, Any]:
    return {
        "aid": aid, "hook": "PreCompact",
        "explain": {"compaction": {"aid": aid, "n_turns_before": 30}},
    }


def _retry(*, aid: str) -> dict[str, Any]:
    return {
        "aid": aid, "hook": "UserPromptSubmit",
        "explain": {
            "user_retry": {
                "prompt_hash": "deadbeef" * 2,
                "prompt_size_bytes": 80,
                "is_retry": True,
            }
        },
    }


def build_synthetic_audit(path: Path) -> None:
    """10-session chain spanning a week, with realistic variation:

    * one big session (long, high cache hit, with 1 backtrack)
    * three medium sessions
    * six short sessions (some clean, some with signals)
    """
    base_ts = 1_775_000_000 * 1_000_000_000  # ~ 2026-04-29

    records: list[dict[str, Any]] = []

    # Session 1 — big, mostly cached
    aid = "sess-001"
    records.append(_stop(
        aid=aid, ts_ns=base_ts + 0 * _DAY_NS, dollars=2.50,
        hit_rate=0.92, in_tok=15_000, out_tok=8_000,
        cr=180_000, cc=12_000, n_success=42, n_back=1,
    ))
    records.append(_post(aid=aid, tool="Edit", backtrack=True))
    records.extend(
        _post(aid=aid, tool="Read") for _ in range(8)
    )

    # Sessions 2-4 — medium
    for i, hit_rate in enumerate([0.75, 0.80, 0.65]):
        aid = f"sess-00{2+i}"
        records.append(_stop(
            aid=aid, ts_ns=base_ts + (1 + i) * _DAY_NS,
            dollars=0.40 + 0.05 * i, hit_rate=hit_rate,
            in_tok=4_000, out_tok=2_000,
            cr=12_000 * (1 + i), cc=1_500,
            n_success=18, n_back=0, n_red=1 if i == 1 else 0,
        ))
        if i == 1:
            records.append(_post(aid=aid, tool="Bash", redundant=True))

    # Sessions 5-10 — short
    short_configs: list[tuple[float, float, int, int, int]] = [
        # (dollars, hit_rate, n_success, n_red, n_err)
        (0.05, 0.10, 4, 0, 0),
        (0.08, 0.30, 6, 0, 0),
        (0.04, 0.05, 3, 0, 0),
        (0.12, 0.40, 8, 0, 1),       # short with one tool error
        (0.07, 0.20, 5, 1, 0),       # one redundant
        (0.06, 0.15, 4, 0, 0),
    ]
    for i, (d, hr, ns, nr, ne) in enumerate(short_configs):
        aid = f"sess-{5 + i:03d}"
        records.append(_stop(
            aid=aid, ts_ns=base_ts + (4 + i) * _DAY_NS,
            dollars=d, hit_rate=hr,
            in_tok=600, out_tok=400, cr=300, cc=80,
            n_success=ns, n_red=nr, n_err=ne,
        ))
        if nr:
            records.append(_post(aid=aid, tool="Bash", redundant=True))
        if ne:
            records.append(_post(aid=aid, tool="Bash", is_error=True))

    # 2 compactions and 1 user retry sprinkled in
    records.append(_precompact(aid="sess-001"))
    records.append(_precompact(aid="sess-002"))
    records.append(_retry(aid="sess-005"))

    with path.open("w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")


# ──────────────────────────────────────────────────────────────────────
# Renderer (delegates to the same one used by `aegis status --performance`)
# ──────────────────────────────────────────────────────────────────────


def render(audit_path: Path) -> None:
    summary = build_performance_summary(audit_path)
    # Delegate to the CLI's renderer so the demo + the CLI produce
    # identical output.
    import aegis_cli as cli  # type: ignore[import-not-found]

    cli._render_performance_dashboard(summary)


# ──────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Aegis-ATV performance dashboard walkthrough.",
    )
    p.add_argument(
        "--keep", action="store_true",
        help="Preserve the synthetic audit chain on exit (for jq inspection).",
    )
    args = p.parse_args(argv)

    workdir = Path(tempfile.mkdtemp(prefix="aegis-perf-dashboard-"))
    audit_path = workdir / "audit.jsonl"

    print(f"\033[2mworking dir: {workdir}\033[0m")
    print("\033[2mbuilding synthetic 10-session audit chain...\033[0m")
    build_synthetic_audit(audit_path)
    print()
    print("\033[1m── Aegis Status Performance Dashboard ──"
          "──────────────────────────────\033[0m")
    try:
        render(audit_path)
        if args.keep:
            print()
            print(f"\033[32m--keep: {audit_path}\033[0m")
            print(
                f"\033[2mtry:  uv run python tools/aegis_cli.py status "
                f"--performance --json --audit {audit_path}\033[0m"
            )
        return 0
    finally:
        if not args.keep:
            shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
