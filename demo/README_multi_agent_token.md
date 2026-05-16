# Multi-agent token-optimization demo

End-to-end demonstration of how Aegis ATV monitors token usage across multiple agents and surfaces optimization advice. Four agent personas with deliberately distinct cost profiles seed a synthetic ContextMemory store; Aegis's analytics layer then produces a doctor report, generates concrete CLAUDE.md proposals, auto-applies the top one, and finally reads the applied marker back via `memory diff` for the round-trip audit.

## Run it

```bash
python demo/multi_agent_token_demo.py --reset
# or:  python -m demo.multi_agent_token_demo --reset
```

The `--reset` flag wipes `/tmp/aegis-demo/` and re-seeds from scratch.

## What the demo simulates

| Agent | Calls | Cost | Pattern |
|---|---|---|---|
| **agent-research** | 28 | $0.07 | Explorer — Reads + a few WebSearches. Baseline. |
| **agent-greedy** | 23 | **$0.31** | Token-heavy — 15× WebSearch with no caching. |
| **agent-loopy** | 10 | $0.008 | Coder with bug — 5× repeated Bash trips step336. |
| **agent-cautious** | 8 | $0.005 | Disciplined small calls — no flags. |
| **Total** | **69** | **$0.39** | |

Each call becomes one row in ContextMemory with the appropriate `tool_name`, `decision`, `cost_usd`, `latency_ms`, `reason`, and `recommended_advisors` fields — exactly what the real Aegis firewall would write at runtime.

## The five demo steps

```
┌─────────────────────────────────────────────────────────────┐
│ Step 1  $ aegis memory show                                 │
│           → 69 records, 25 KB, oldest/newest ts range       │
│                                                             │
│ Step 2  $ aegis doctor --since 2h                           │
│           → Cost / Performance / Security report with       │
│             per-tool latency + cumulative spend per agent   │
│                                                             │
│ Step 3  $ aegis memory claude-md --since 2h                 │
│           → 5 proposals:                                    │
│              #1 high-cost-tool  WebSearch  ($0.35)   [high] │
│              #2 advisor-rec     context-compactor    [med]  │
│              #3 advisor-rec     cost-optimizer       [med]  │
│              #4 loop-detector   repeated Bash        [high] │
│              #5 advisor-rec     loop-breaker         [med]  │
│                                                             │
│ Step 4  $ aegis memory claude-md --apply 1                  │
│           → Splices proposal #1 into CLAUDE.md under        │
│             `## Cost Discipline`. Writes .bak first.        │
│                                                             │
│ Step 5  $ aegis memory diff                                 │
│           → Reads the `<!-- aegis-managed-proposal -->`     │
│             marker back, confirming the splice survived.    │
└─────────────────────────────────────────────────────────────┘
```

## Architecture demonstrated

```
agents → tool calls
        │
        ▼
┌─────────────────────────────────────────────────────┐
│ ContextMemory ( ~/.aegis/context_memory.jsonl )     │
│   • One row per tool call (30-subfield ATV-2080-v1) │
│   • Cost, latency, decision, advisor recommendations│
│   • Persistent, gzip-rotated (v0.5.7)               │
└─────────────────────────────────────────────────────┘
        │
        ▼  (read by analytics surfaces)
        │
┌───────────────┬────────────────┬─────────────────────┐
│ aegis doctor  │ memory claude-md│ memory diff         │
│ (window stats)│ (6 miners ->    │ (applied-proposal   │
│               │  proposals)     │  reverse-lookup)    │
└───────────────┴────────────────┴─────────────────────┘
        │             │                  │
        │             ▼                  ▼
        │      ┌──────────────┐   ┌──────────────┐
        │      │ --apply N    │   │ CI artifact  │
        │      │  splice into │   │ (--json)     │
        │      │  CLAUDE.md   │   └──────────────┘
        │      └──────────────┘
        ▼
┌───────────────────────────────┐
│ Markdown report               │
│ (paste into PR / docs)        │
└───────────────────────────────┘
```

## What the demo proves about Aegis ATV

* **Multi-agent attribution works.** Each persona's pattern surfaces independently in the analytics — no global average that would hide agent-greedy's cost concentration.

* **Cost-driver detection works.** The high-cost-tool miner (`$0.01 default threshold, tunable via --min-tool-cost-usd`) correctly identifies WebSearch as agent-greedy's bottleneck.

* **Loop detection works.** The step336 loop-detector reason string ("same X call repeated 3 times") propagates through ContextMemory and the memory-claude-md miner re-extracts it as a reflective-stop proposal.

* **Closed-loop automation works.** Step 4 (`--apply 1`) modifies the project CLAUDE.md with a traceable HTML-comment marker; Step 5 (`memory diff`) parses the marker back out — the lifecycle is round-trip without operator-side bookkeeping.

## Customizing the demo

Edit `_make_personas()` in `multi_agent_token_demo.py` to add personas or tweak call counts / costs. The dataclass shape:

```python
Call(
    tool="WebSearch",           # any tool name
    decision="ALLOW",           # ALLOW / BLOCK / REQUIRE_APPROVAL
    cost_usd=0.020,             # dollar cost — feeds high-cost-tool miner
    latency_ms=420.0,           # for perf rollup
    reason="...",               # firewall reason text
                                # (e.g. "same X call repeated 3 times...")
    advisors=("cost-optimizer",) # which advisors the sLLM judge recommended
)
```

The miner thresholds:

| Miner | Threshold env / flag | Default |
|---|---|---|
| high-cost-tool | `--min-tool-cost-usd USD` | `$0.01` cumulative |
| All miners | `--min-count N` | `3` calls per pattern |
| Loop-detector | hardcoded — fires on `reason` match | n/a |

## Cleanup

```bash
# Wipe the demo store + start fresh on next run:
python demo/multi_agent_token_demo.py --reset

# Or just delete the directory:
#   delete /tmp/aegis-demo
```

The demo never touches `~/.aegis/` so it's safe to run repeatedly without affecting the operator's real audit chain or ContextMemory.
