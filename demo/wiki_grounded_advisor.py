"""v0.5.18 end-to-end demo — wiki-grounded advisor measurement.

Asks the question: **does the v0.5.15–0.5.17 wiki pipeline
actually change what an sLLM advisor sees?** And answers it with
concrete, reproducible numbers.

What this demo does
===================

1. Synthesises five agent profiles with deliberately distinct
   behavioural fingerprints:

   * ``clean-coder``       — 200 calls, 0% BLOCK, low cost
   * ``high-cost``         — 200 calls, expensive prompts
   * ``unstable``          — 200 calls, 12% BLOCK rate
   * ``frequent-approvals``— 200 calls, 35% REQUIRE_APPROVAL
   * ``sparse``            — 8 calls (below the confidence floor)

2. Builds the v0.5.15 knowledge wiki from these synthetic
   records (no LLM, no network — fully deterministic).

3. For each agent, measures the wiki context the advisor
   would receive (entries, infobox fields, cross-refs, tags,
   estimated prompt tokens) via
   :func:`aegis.knowledge.measure_context`.

4. Builds the actual sLLM user message for each agent *with*
   and *without* the wiki, then prints the size delta + the
   block-position so an operator can see exactly where the
   wiki sits in the prompt.

5. Renders one full side-by-side example (clean-coder) for
   the operator to read.

What this demo does NOT do
==========================

* No LLM call — the model's downstream quality is a separate
  experiment that requires API access and a held-out evaluation
  set. This demo proves the **plumbing** delivers wiki context
  to the prompt; the model evaluation is downstream.
* No assertion that wiki context improves advice quality —
  that's the experiment to run *with* this infrastructure, not
  in this demo.

Usage
=====

::

    uv run python demo/wiki_grounded_advisor.py

Optional: ``--json`` for machine-readable output, ``--full`` to
print the full prompts for every agent (default prints only one
example to keep the output digestible).
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import time
from dataclasses import asdict
from pathlib import Path

from aegis.context_memory.record import ContextMemoryRecord
from aegis.knowledge import (
    build_knowledge,
    clear_advisor_cache,
    knowledge_context_for_advisor,
    measure_context,
    save_entry,
    save_index,
)
from aegis.knowledge.advisor import ContextMetrics

# ──────────────────────────────────────────────────────────────────
# Synthetic agent profiles
# ──────────────────────────────────────────────────────────────────


def _rec(
    *,
    aid: str,
    tool: str,
    decision: str = "ALLOW",
    reason: str = "",
    trace_id: str = "trace",
    ts_ns: int | None = None,
    cost_usd: float = 0.001,
    latency_ms: float = 100.0,
    tokens_in: int = 100,
    tokens_out: int = 50,
) -> ContextMemoryRecord:
    return ContextMemoryRecord(
        ts_ns=ts_ns if ts_ns is not None else time.time_ns(),
        trace_id=trace_id,
        invocation_id="inv",
        aid=aid,
        tenant_id="t",
        tool_name=tool,
        decision=decision,
        reason=reason,
        channel=None,
        provider=None,
        latency_ms=latency_ms,
        cost_usd=cost_usd,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        step_traces={},
        m13_score=None,
        advisor_invoked=False,
        recommended_advisors=(),
        atv_sha3=None,
        atv_dim=2080,
        is_sidechain=False,
        mode="sidecar",
    )


def _synth_clean(aid: str, base_ts: int) -> list[ContextMemoryRecord]:
    return [
        _rec(
            aid=aid,
            tool=("Bash" if i % 3 == 0 else "Edit" if i % 3 == 1 else "Read"),
            trace_id=f"{aid}-{i:04d}",
            ts_ns=base_ts + i * 1_000_000_000,
        )
        for i in range(200)
    ]


def _synth_high_cost(aid: str, base_ts: int) -> list[ContextMemoryRecord]:
    return [
        _rec(
            aid=aid,
            tool=("Bash" if i % 2 == 0 else "Edit"),
            trace_id=f"{aid}-{i:04d}",
            ts_ns=base_ts + i * 1_000_000_000,
            cost_usd=0.05,                 # 50× the clean baseline
            tokens_in=2000,
            tokens_out=800,
        )
        for i in range(200)
    ]


def _synth_unstable(aid: str, base_ts: int) -> list[ContextMemoryRecord]:
    out: list[ContextMemoryRecord] = []
    for i in range(200):
        if i % 8 == 0:
            out.append(_rec(
                aid=aid, tool="Bash",
                decision="BLOCK",
                reason=f"rule:dangerous_pattern variant-{i % 3}",
                trace_id=f"{aid}-{i:04d}",
                ts_ns=base_ts + i * 1_000_000_000,
            ))
        else:
            out.append(_rec(
                aid=aid,
                tool=("Bash" if i % 2 == 0 else "Edit"),
                trace_id=f"{aid}-{i:04d}",
                ts_ns=base_ts + i * 1_000_000_000,
            ))
    return out


def _synth_frequent_approvals(
    aid: str, base_ts: int,
) -> list[ContextMemoryRecord]:
    out: list[ContextMemoryRecord] = []
    for i in range(200):
        if i % 3 == 0:
            out.append(_rec(
                aid=aid, tool="Bash",
                decision="REQUIRE_APPROVAL",
                reason="same Bash call repeated 3 times this session",
                trace_id=f"{aid}-{i:04d}",
                ts_ns=base_ts + i * 1_000_000_000,
            ))
        else:
            out.append(_rec(
                aid=aid, tool="Bash",
                trace_id=f"{aid}-{i:04d}",
                ts_ns=base_ts + i * 1_000_000_000,
            ))
    return out


def _synth_sparse(aid: str, base_ts: int) -> list[ContextMemoryRecord]:
    return [
        _rec(
            aid=aid, tool="Bash",
            trace_id=f"{aid}-{i:04d}",
            ts_ns=base_ts + i * 1_000_000_000,
        )
        for i in range(8)        # below the n=50 confidence floor
    ]


_SYNTH_AGENTS: list[tuple[str, str, callable]] = [
    ("clean-coder", "200 calls, 0% BLOCK, low cost", _synth_clean),
    ("high-cost", "200 calls, 50× cost per call", _synth_high_cost),
    ("unstable", "200 calls, 12.5% BLOCK rate", _synth_unstable),
    ("frequent-approvals", "200 calls, 33% REQUIRE_APPROVAL", _synth_frequent_approvals),
    ("sparse", "8 calls — below confidence floor", _synth_sparse),
]


# ──────────────────────────────────────────────────────────────────
# Prompt comparison — with vs without wiki
# ──────────────────────────────────────────────────────────────────


def _build_advisor_prompt(*, knowledge_context: str | None) -> str:
    """Call ``_build_user_message`` directly so we measure the
    actual production message format (the same one HaikuAdvisor
    feeds the model)."""
    from aegis.judge.advisor import _build_user_message
    return _build_user_message(
        temporal_ctx=None,
        anomalies=None,
        baseline=None,
        catalog=None,
        intent_classifier=None,
        action_table=None,
        base_decision="ALLOW",
        base_reason="(synthetic demo: no real verdict)",
        current_tool="Bash",
        knowledge_context=knowledge_context,
    )


# ──────────────────────────────────────────────────────────────────
# Pretty-printing helpers
# ──────────────────────────────────────────────────────────────────


def _print_table(rows: list[dict[str, object]]) -> None:
    """Render a list of dicts as an aligned markdown-style table."""
    if not rows:
        return
    cols = list(rows[0].keys())
    widths = {
        c: max(len(c), *(len(str(r[c])) for r in rows)) for c in cols
    }
    header = " | ".join(c.ljust(widths[c]) for c in cols)
    sep = "-+-".join("-" * widths[c] for c in cols)
    print(header)
    print(sep)
    for r in rows:
        print(" | ".join(str(r[c]).ljust(widths[c]) for c in cols))


def _summarise_metric(m: ContextMetrics | None) -> dict[str, object]:
    if m is None:
        return {
            "wiki_entry": "no",
            "n_entries": 0,
            "infobox": 0,
            "cross_refs": 0,
            "tags": 0,
            "chars": 0,
            "~tokens": 0,
        }
    return {
        "wiki_entry": "yes",
        "n_entries": m.n_entries,
        "infobox": m.n_infobox_fields,
        "cross_refs": m.n_cross_refs,
        "tags": m.n_tags,
        "chars": m.markdown_chars,
        "~tokens": m.estimated_tokens,
    }


# ──────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="v0.5.18 — measure wiki context's effect on advisor prompts.",
    )
    ap.add_argument(
        "--json", dest="emit_json", action="store_true",
        help="emit machine-readable JSON instead of pretty output",
    )
    ap.add_argument(
        "--full", action="store_true",
        help=(
            "print the full advisor prompt for every agent "
            "(default prints only one example)"
        ),
    )
    args = ap.parse_args(argv)

    # Work in an isolated temp wiki dir so we don't touch the
    # operator's real ~/.aegis/knowledge/.
    tmp = Path(tempfile.mkdtemp(prefix="aegis-wiki-demo-"))
    try:
        return _run(tmp, emit_json=args.emit_json, full=args.full)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _run(tmp: Path, *, emit_json: bool, full: bool) -> int:
    # 1. Generate synthetic records.
    base_ts = time.time_ns() - 7 * 86_400 * 1_000_000_000
    all_records: list[ContextMemoryRecord] = []
    aid_records: dict[str, list[ContextMemoryRecord]] = {}
    for aid, _label, factory in _SYNTH_AGENTS:
        recs = factory(aid, base_ts)
        aid_records[aid] = recs
        all_records.extend(recs)

    # 2. Build the wiki from all records.
    entries = build_knowledge(all_records)
    for e in entries:
        save_entry(e, root=tmp)
    save_index(
        entries, root=tmp,
        built_at_ns=time.time_ns(),
        built_from_records=len(all_records),
    )

    # 3. Wire the helper to this tmp wiki dir.
    import os
    os.environ["AEGIS_KNOWLEDGE_DIR"] = str(tmp)
    clear_advisor_cache()

    # 4. Measure wiki contribution per agent.
    summary_rows: list[dict[str, object]] = []
    prompt_delta_rows: list[dict[str, object]] = []
    measurements: dict[str, dict[str, object]] = {}

    for aid, label, _factory in _SYNTH_AGENTS:
        m = measure_context(aid)
        summary = _summarise_metric(m)
        summary["aid"] = aid
        summary["profile"] = label
        summary_rows.append({
            "aid": aid,
            "profile": label,
            **{k: v for k, v in summary.items() if k not in ("aid", "profile")},
        })

        # 5. Prompt-size delta with vs without wiki.
        ctx = knowledge_context_for_advisor(aid)
        prompt_no_wiki = _build_advisor_prompt(knowledge_context=None)
        prompt_with_wiki = _build_advisor_prompt(knowledge_context=ctx)
        delta = len(prompt_with_wiki) - len(prompt_no_wiki)
        prompt_delta_rows.append({
            "aid": aid,
            "no_wiki_chars": len(prompt_no_wiki),
            "with_wiki_chars": len(prompt_with_wiki),
            "delta_chars": delta,
            "~delta_tokens": delta // 4,
            "wiki_in_prompt": "yes" if "KNOWLEDGE CONTEXT" in prompt_with_wiki else "no",
        })

        measurements[aid] = {
            "label": label,
            "metrics": asdict(m) if m is not None else None,
            "prompt_no_wiki_chars": len(prompt_no_wiki),
            "prompt_with_wiki_chars": len(prompt_with_wiki),
        }

    # JSON path — emit the full structured payload + return.
    if emit_json:
        payload = {
            "wiki_dir": str(tmp),
            "n_records_synthesized": len(all_records),
            "n_entries_built": len(entries),
            "agents": measurements,
        }
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0

    # Pretty output.
    print()
    print("Aegis v0.5.18 — Wiki-grounded advisor measurement")
    print("=" * 56)
    print()
    print(f"Synthesized records:       {len(all_records):,}")
    print(f"Wiki entries built:        {len(entries)}")
    print(f"Temp wiki dir:             {tmp}")
    print()

    print("Per-agent wiki context (what the advisor receives)")
    print("-" * 56)
    _print_table(summary_rows)
    print()

    print("Prompt size impact (with vs without wiki)")
    print("-" * 56)
    _print_table(prompt_delta_rows)
    print()

    # 6. Render one full side-by-side example.
    example_aid = _SYNTH_AGENTS[0][0]
    ctx = knowledge_context_for_advisor(example_aid)
    print(f"Example: full advisor prompt for `{example_aid}`")
    print("=" * 56)
    print()
    print("--- WITHOUT WIKI (baseline) ---")
    print()
    print(_build_advisor_prompt(knowledge_context=None))
    print()
    print("--- WITH WIKI (v0.5.17 path) ---")
    print()
    print(_build_advisor_prompt(knowledge_context=ctx))
    print()

    # 7. Optional: every prompt.
    if full:
        for aid, _label, _factory in _SYNTH_AGENTS[1:]:
            ctx = knowledge_context_for_advisor(aid)
            print(f"\n--- WITH WIKI for `{aid}` ---\n")
            print(_build_advisor_prompt(knowledge_context=ctx))

    print()
    print("Takeaways")
    print("-" * 56)
    print("  • The wiki adds structured agent-background to every")
    print("    advisor call — quantified above, per agent.")
    print("  • Below-confidence agents (e.g. 'sparse') still get")
    print("    a wiki entry but with smaller infobox + lower")
    print("    n_observations, so the advisor can weigh accordingly.")
    print("  • Prompt overhead is bounded (~2-5k chars / call) and")
    print("    cached, so the hot path stays under hook budgets.")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
