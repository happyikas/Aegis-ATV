# Coding-AI token-waste detection demo

Five Claude-Code-shaped agents attempt **the same coding task** — fixing a bug in `src/aegis/firewall/step336_loop.py` — with five different efficiency profiles. `agent-baseline` is the gold standard; the other four exhibit a specific token-waste anti-pattern. The demo lets Aegis ATV measure exactly how much each anti-pattern costs vs the baseline, then surface the proposals that would close the gap.

## Run

```bash
python demo/coding_agent_token_waste.py --reset
```

## Synthetic input (token + cost per agent)

| Agent | Calls | Tokens | Cost $ | vs baseline | Anti-pattern |
|---|---:|---:|---:|---:|---|
| `agent-baseline` | 5 | 5,950 | **$0.0329** | — | Locate → scoped Read → Edit → focused test |
| `agent-rereader` | 13 | 67,200 | $0.4548 | **13.8×** | Re-Reads same 500-line file 8× |
| `agent-overscope` | 9 | 227,800 | $0.9414 | **28.7×** | Reads 5000-line files in full |
| `agent-loop-stuck` | 13 | 19,450 | $0.1190 | **3.6×** | 6× identical pytest runs |
| `agent-toolbox-confused` | 9 | 42,600 | $0.5106 | **15.5×** | WebSearch + huge `find` instead of Grep |
| **Total** | **49** | **363,000** | **$2.06** | | |

→ **Optimal cost** (5 agents × baseline): $0.16
→ **Wasted**: $1.89 (**92% of total spend**)

## What Aegis surfaces (Step 3 — 11 proposals from the demo run)

The `aegis memory claude-md` miners produced **11 proposals**, sorted by priority. Top 6:

| # | Miner | Trigger | Confidence | Surfaced waste |
|---|---|---|---|---|
| 1 | `high-cost-tool` | **Read** (12×) accumulated **$1.14** | high | agent-rereader + agent-overscope |
| 2 | `high-cost-tool` | **Bash** (12×) accumulated $0.32 | high | agent-toolbox (find ` | xargs cat`) |
| 3 | `advisor-recommendation` | **context-compactor** (11×) | medium | over-fetching context |
| 4 | `advisor-recommendation` | **loop-breaker** (10×) | medium | duplicate work |
| 5 | `high-cost-tool` | **Edit** (9×) | medium | repeated patching attempts |
| 6 | `loop-detector` | **repeated Read** (6×) | high | agent-rereader |
| 7 | `advisor-recommendation` | **cost-optimizer** (7×) | medium | wrong tool / expensive paths |
| 8 | `loop-detector` | **repeated Bash** (4×) | high | agent-loop-stuck |
| 9 | `advisor-recommendation` | **test-runner** (4×) | medium | test-execution anti-pattern |
| 10 | `high-cost-tool` | **Grep** (3×) | medium | very minor |
| 11 | `high-cost-tool` | **WebSearch** (3×) accumulated $0.21 | medium | agent-toolbox |

## Anti-pattern → Aegis surface mapping

```
┌──────────────────────────────────────────────────────────────────────┐
│ agent-rereader  (Re-Read 8× same file)                               │
│   ↓                                                                  │
│   ContextMemory rows: 8× Read calls with identical tool_args         │
│   ↓                                                                  │
│   step336 fires on Read repetition #3 → REQUIRE_APPROVAL             │
│   ↓                                                                  │
│   loop-detector miner picks up 6 repeated calls                      │
│   advisor-recommendation miner: context-compactor recommended 6×     │
│   high-cost-tool miner: Read = $1.14 cumulative (top spender)        │
│   ↓                                                                  │
│   Proposals:                                                          │
│     "If you find yourself calling Read 3× in a row, stop..."         │
│     "Read is expensive — check if recent result is reusable..."      │
└──────────────────────────────────────────────────────────────────────┘
```

Same pattern for the other anti-patterns:

* **agent-overscope** → context-compactor + cost-optimizer (8 advisor recommendations)
* **agent-loop-stuck** → loop-detector + loop-breaker (5 repeated pytest)
* **agent-toolbox-confused** → high-cost-tool (WebSearch + huge Bash) + cost-optimizer (7×)

## Why this demo is hard for non-Aegis approaches

A naive log analyzer or LLM-billing dashboard can show "you spent $2.06 last hour". That's it. Aegis ATV instead:

1. **Per-agent attribution** — agent-overscope (29× baseline) is the worst, not agent-loop-stuck (only 4× baseline). Without attribution you optimize the wrong agent.

2. **Per-call cost ranking** — the 4 Reads at $0.225 each are visible as the top cost drivers. You don't have to grep logs to find them.

3. **Pattern detection** — "Read repeated 6×" + "Bash pytest repeated 4×" are concrete patterns, not just "Read is expensive". The CLAUDE.md proposals target the *pattern*, not the tool.

4. **Tool-substitution hints** — `cost-optimizer` advisor was recommended 7× on the toolbox-confused agent. Aegis explicitly suggests "use Grep instead of WebSearch / Bash find" via the advisor pipeline.

5. **Closed-loop fix** — `aegis memory claude-md --apply N` puts the optimization into CLAUDE.md → future sessions on the same project don't repeat the mistake. No analyzer-dashboard does this.

## Customizing

Edit `_agents()` in `coding_agent_token_waste.py`:

```python
Agent(
    aid="my-agent",
    headline="What this agent does well or poorly",
    failure_mode="Specific waste pattern",
    calls=(
        Call("Read", tokens_in=50_000, tokens_out=5_000,
             advisors=("context-compactor",),
             note="Reads entire file when slice would do"),
        # ...
    ),
)
```

The `Call` factory computes cost from real Claude Sonnet pricing ($3/1M input + $15/1M output). Tweaking token counts changes which miners surface that pattern.

## Cleanup

```bash
# Re-run with --reset to wipe /tmp/aegis-coding-waste/
python demo/coding_agent_token_waste.py --reset
```

The demo never touches `~/.aegis/` so it's safe to run repeatedly.
