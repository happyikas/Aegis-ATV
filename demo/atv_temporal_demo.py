"""Aegis ATV Temporal Narrative Demo (PR-θ)
=============================================

Walks the agent's recent activity from a synthetic Claude Code
transcript + audit chain, then prints the **multi-turn TEMPORAL
TRAJECTORY narrative** — the "video" view that closes Gap #4
(action_history was hash-only) from PR #58's diagnostic.

The narrative is what the sLLM will read in the eventual
ActionAdvice judge (PR-ζ). It contains:

* per-turn rows with ↩BACKTRACK / ♻REDUNDANT / ✗ERROR sigils
* cumulative-token + cache-hit-rate trajectories
* aggregate inefficiency counts in the window
* distinct-tools list

Run
---

::

    uv run python demo/atv_temporal_demo.py
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

# ruff: noqa: E402  -- imports follow sys.path bootstrap above.
from aegis.atv.builder import build_atv
from aegis.atv.serializer import atv_to_prompt
from aegis.atv.temporal import load_recent_history
from aegis.schema import ATVHeader, ATVInput, CostEfficiencyMetrics

_BOLD = "\033[1m"
_DIM = "\033[2m"
_BLUE = "\033[34m"
_GREEN = "\033[32m"
_YELLOW = "\033[33m"
_RED = "\033[31m"
_RESET = "\033[0m"


SESSION_ID = "demo-temporal-0001"


def _build_synthetic_session(workdir: Path) -> tuple[Path, Path]:
    """Synthesise a transcript + audit chain modelling a realistic
    "agent gets stuck" pattern: two Edit calls (the second reverts
    the first → BACKTRACK), then pytest fails, then Bash 'rm' attempt."""
    ts_path = workdir / "transcript.jsonl"
    audit_path = workdir / "audit.jsonl"

    # 6 transcript turns. Tokens grow; cache hits high until
    # the agent panics on Bash and cache drops.
    turns = [
        # tool, args, in_tok, out_tok, cache_read, cache_creation
        ("Read",  '{"file_path": "src/auth.py"}',           1000, 200, 0,    1000),
        ("Grep",  '{"pattern": "def login", "path": "src"}', 200, 100, 1000, 100),
        ("Edit",  '{"file_path": "src/auth.py"}',            300, 150, 1100, 100),
        ("Edit",  '{"file_path": "src/auth.py"}',            200,  80, 1300, 50),   # reverts -3
        ("Bash",  '{"command": "uv run pytest tests/auth/"}', 400, 200, 1500, 0),    # fails
        ("Bash",  '{"command": "rm -rf .pytest_cache/"}',     2000, 50, 200,  1500), # cache drops
    ]
    with ts_path.open("w") as fh:
        for tool, args, in_t, out_t, cr, cc in turns:
            rec = {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{
                        "type": "tool_use",
                        "name": tool,
                        "id": f"tu_{tool}_{in_t}",
                        "input": json.loads(args),
                    }],
                    "usage": {
                        "input_tokens": in_t,
                        "output_tokens": out_t,
                        "cache_read_input_tokens": cr,
                        "cache_creation_input_tokens": cc,
                    },
                },
            }
            fh.write(json.dumps(rec) + "\n")

    # Audit chain: PreToolUse (decision) + PostToolUse (status + signals)
    audit_records = [
        # ts_ns, hook, tool, decision, status, post_analysis
        (1_001, "pre",  "Read", "ALLOW", None,    None),
        (1_002, "post", "Read", None,   "success", {}),
        (1_003, "pre",  "Grep", "ALLOW", None,    None),
        (1_004, "post", "Grep", None,   "success", {}),
        (1_005, "pre",  "Edit", "ALLOW", None,    None),
        (1_006, "post", "Edit", None,   "success", {}),
        (1_007, "pre",  "Edit", "ALLOW", None,    None),
        (1_008, "post", "Edit", None,   "success", {
            "backtrack": {"file_path": "src/auth.py"},
        }),
        (1_009, "pre",  "Bash", "ALLOW", None,    None),
        (1_010, "post", "Bash", None,   "failure", {
            "classification": {"is_error": True},
        }),
        (1_011, "pre",  "Bash", "REQUIRE_APPROVAL", None, None),
        (1_012, "post", "Bash", None,   "success", {
            "redundant_of": "earlier-bash-trace",
        }),
    ]
    with audit_path.open("w") as fh:
        for ts, hook, tool, decision, status, pa in audit_records:
            if hook == "pre":
                rec = {
                    "ts_ns": ts, "aid": SESSION_ID, "tool": tool,
                    "decision": decision, "reason": "demo",
                }
            else:
                rec = {
                    "ts_ns": ts, "aid": SESSION_ID, "tool": tool,
                    "hook": "PostToolUse", "status": status,
                    "explain": {"post_analysis": pa or {}},
                }
            fh.write(json.dumps(rec) + "\n")

    return ts_path, audit_path


def main() -> int:
    workdir = Path(tempfile.mkdtemp(prefix="aegis-temporal-demo-"))

    print(f"{_BOLD}Aegis Temporal Narrative Demo (PR-θ){_RESET}")
    print(f"{_DIM}building synthetic 6-turn 'agent gets stuck' session in {workdir}{_RESET}")
    ts_path, audit_path = _build_synthetic_session(workdir)

    try:
        print()
        print(f"{_BOLD}{_BLUE}── 1. Load recent history (window=6){_RESET}")
        ctx = load_recent_history(
            transcript_path=ts_path,
            audit_path=audit_path,
            session_id=SESSION_ID,
            window_size=6,
        )
        print(
            f"  loaded: {len(ctx.history)} turns, "
            f"backtracks={ctx.n_backtracks}, "
            f"redundant={ctx.n_redundant}, "
            f"errors={ctx.n_errors}, "
            f"failures={ctx.n_failures}"
        )

        # ── 2. Standalone narrative ──
        print()
        print(f"{_BOLD}{_BLUE}── 2. TEMPORAL TRAJECTORY narrative (the sLLM 'video'){_RESET}")
        from aegis.atv.temporal import serialize_temporal
        print(f"{_DIM}{serialize_temporal(ctx)}{_RESET}")

        # ── 3. Combined with atv_to_prompt ──
        print()
        print(
            f"{_BOLD}{_BLUE}── 3. atv_to_prompt(temporal=ctx) "
            f"— full sLLM-ready prompt{_RESET}"
        )
        # Use a minimal ATVInput so the demo runs without other state.
        inp = ATVInput(
            header=ATVHeader(
                trace_id="t" * 32, span_id="s" * 16,
                tenant_id="alice", aid=SESSION_ID, timestamp_ns=1,
            ),
            tool_name="Bash",
            tool_args_json='{"command": "rm -rf .pytest_cache/"}',
            plan_text="Cleanup pytest cache before retrying.",
            cost_estimate=CostEfficiencyMetrics(
                cumulative_tokens=12_480,
                cache_hit_rate=0.10,
                context_utilization_ratio=0.42,
            ),
        )
        atv = build_atv(inp)
        out = atv_to_prompt(atv, inp, mode="enriched", temporal=ctx)
        print(f"{_DIM}{out.text}{_RESET}")
        print()
        print(
            f"  prompt size: {len(out)} chars / {out.line_count()} lines"
        )

        # ── 4. Verdict ──
        print()
        print(f"{_BOLD}{_BLUE}── 4. What the sLLM would see (summary){_RESET}")
        print(
            f"  • {len(ctx.history)} turns rendered chronologically with "
            "decision + outcome + signals"
        )
        print(
            f"  • cumulative_tokens trajectory: "
            f"{ctx.cumulative_token_trajectory[0]:,} → "
            f"{ctx.cumulative_token_trajectory[-1]:,}"
        )
        if ctx.cache_hit_rate_max_drop_pp > 5:
            print(
                f"  • {_YELLOW}cache_hit_rate dropped "
                f"{ctx.cache_hit_rate_max_drop_pp:.0f} pp within window"
                f"{_RESET}"
            )
        if ctx.n_backtracks:
            print(
                f"  • {_RED}{ctx.n_backtracks} backtrack(s) — "
                f"agent reverted its own work{_RESET}"
            )
        if ctx.n_errors:
            print(
                f"  • {_RED}{ctx.n_errors} tool error(s){_RESET}"
            )
        if ctx.n_redundant:
            print(
                f"  • {_YELLOW}{ctx.n_redundant} redundant call(s){_RESET}"
            )
        print()
        print(
            f"  {_DIM}Next PR (-ε): turn this multi-turn signal into burn-in "
            f"baseline anomaly tags. Then PR-ζ wires the sLLM action "
            f"recommendation head.{_RESET}"
        )

        return 0
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
