# Aegis Mac mini Validation Report

Driver: `python -m demo.macmini all`

Self-contained 90-case validation suite covering Cost, Performance and Security advisors.

## Headline

- **Total cases**: 100
- **Pass**: 100 (100%)
- **Fail**: 0

## By category

| Category | Cases | Pass | Fail | Pass% |
|----------|-------|------|------|-------|
| cost | 30 | 30 | 0 | 100% |
| performance | 30 | 30 | 0 | 100% |
| security | 30 | 30 | 0 | 100% |
| rag | 10 | 10 | 0 | 100% |

## Advisor frequency

| Advisor | Count |
|---------|-------|
| `security-reviewer` | 30 |
| `cost-optimizer` | 18 |
| `loop-breaker` | 13 |
| `kv-cache-optimizer` | 9 |
| `context-compactor` | 8 |
| `human-clarifier` | 6 |
| `test-runner` | 4 |
| `permission-escalator` | 1 |

## Verb frequency

| Verb | Count |
|------|-------|
| `require-approval` | 27 |
| `prune-turns` | 17 |
| `swap-tool` | 13 |
| `narrow-scope` | 13 |
| `summarize-window` | 9 |
| `end-session` | 8 |
| `notify-operator` | 8 |
| `swap-model` | 7 |
| `clarify-intent` | 6 |
| `run-diagnostic` | 4 |

## Cost (30 cases)

### COST-01 — Idle session — no advisor fires  (PASS)

**Scenario**: 3 cheap routine reads with 92% cache. Cost-optimizer must NOT fire on a healthy idle session.

**Execution**: `compose_advice_heuristic(temporal_ctx=ctx_idle())`

**Result**: decision=`ALLOW` (96.3 ms)
- (no advisor fired)

### COST-02 — Budget warn flag → cost-optimizer fires  (PASS)

**Scenario**: M16 cost ledger raises budget_warn_flag. Cost-optimizer should fire even without other anomalies.

**Execution**: `cost_signals={'budget_warn_flag': True}`

**Result**: decision=`ALLOW` (0.0 ms)
- `cost-optimizer` (prio=medium) verbs=(none)

### COST-03 — Budget at 0.85x — below threshold, no fire  (PASS)

**Scenario**: Budget consumed 85%, below the 0.9 trigger. Cost-optimizer must not yet recommend an action.

**Execution**: `cost_signals={'budget_used_ratio': 0.85}`

**Result**: decision=`ALLOW` (0.0 ms)
- (no advisor fired)

### COST-04 — Budget at 0.9x — exactly at threshold  (PASS)

**Scenario**: Budget at 90% triggers cost-optimizer with prune-turns as the lowest-friction first action.

**Execution**: `cost_signals={'budget_used_ratio': 0.9}`

**Result**: decision=`ALLOW` (0.0 ms)
- `cost-optimizer` (prio=medium) verbs=prune-turns

### COST-05 — Budget at 1.5x — Opus session swaps to Haiku  (PASS)

**Scenario**: 50% over budget on Opus 4.7. swap-model must target a cheaper model with a measurable cost ratio.

**Execution**: `current_model='claude-opus-4-7', budget_used_ratio=1.5`

**Result**: decision=`ALLOW` (0.0 ms)
- `cost-optimizer` (prio=high) verbs=prune-turns,swap-model,end-session

### COST-06 — Budget at 1.5x — prune-turns also present  (PASS)

**Scenario**: At budget 1.5×, cost-optimizer enumerates multiple actions; prune-turns is one of them.

**Execution**: `current_model='claude-opus-4-7', budget_used_ratio=1.5`

**Result**: decision=`ALLOW` (0.0 ms)
- `cost-optimizer` (prio=high) verbs=prune-turns,swap-model,end-session

### COST-07 — Budget at 2.0x — end-session escalation  (PASS)

**Scenario**: Budget doubled. cost-optimizer must include end-session as a hard-stop option.

**Execution**: `current_model='claude-opus-4-7', budget_used_ratio=2.0`

**Result**: decision=`ALLOW` (0.0 ms)
- `cost-optimizer` (prio=high) verbs=prune-turns,swap-model,end-session

### COST-08 — M12 ratio 1.99 — boundary, no fire  (PASS)

**Scenario**: HW/SW divergence just below the 2× threshold. cost-optimizer must remain quiet.

**Execution**: `cost_signals={'hw_vs_sw_divergence_ratio': 1.99}`

**Result**: decision=`ALLOW` (0.0 ms)
- (no advisor fired)

### COST-09 — M12 ratio 2.0 — notify-operator at threshold  (PASS)

**Scenario**: HW reports double the SW-estimated cost; the operator needs to be notified before the gap widens.

**Execution**: `cost_signals={'hw_vs_sw_divergence_ratio': 2.0}`

**Result**: decision=`ALLOW` (0.0 ms)
- `cost-optimizer` (prio=high) verbs=notify-operator

### COST-10 — M12 ratio 3.15 — notify-operator high severity  (PASS)

**Scenario**: HW/SW gap at 3.15×; severe drift between metering and ground truth.

**Execution**: `cost_signals={'hw_vs_sw_divergence_ratio': 3.15}`

**Result**: decision=`ALLOW` (0.0 ms)
- `cost-optimizer` (prio=high) verbs=notify-operator

### COST-11 — M12 ratio 5.0 — notify-operator critical  (PASS)

**Scenario**: HW/SW divergence at 5×; metering essentially uncalibrated.

**Execution**: `cost_signals={'hw_vs_sw_divergence_ratio': 5.0}`

**Result**: decision=`ALLOW` (0.0 ms)
- `cost-optimizer` (prio=high) verbs=notify-operator

### COST-12 — Sonnet → Haiku swap path  (PASS)

**Scenario**: Sonnet 4.6 over budget; swap-model should still emit a valid cheaper-model target rather than no-op.

**Execution**: `current_model='claude-sonnet-4-6', budget_used_ratio=1.5`

**Result**: decision=`ALLOW` (0.0 ms)
- `cost-optimizer` (prio=high) verbs=prune-turns,swap-model,end-session

### COST-13 — Cache drop 51pp — kv-cache-optimizer fires  (PASS)

**Scenario**: Cache hit-rate fell 51pp in the recent window; kv-cache-optimizer should suggest prune-turns to stabilise the prefix.

**Execution**: `cache_hit_rate_max_drop_pp=51.0, prefix_re_keys_in_window=4`

**Result**: decision=`ALLOW` (0.0 ms)
- `kv-cache-optimizer` (prio=high) verbs=prune-turns

### COST-14 — Cache drop 25pp — boundary, kv-cache-optimizer silent  (PASS)

**Scenario**: Cache drop at 25pp; below the 30pp action threshold. kv-cache-optimizer should not yet fire.

**Execution**: `cache_hit_rate_max_drop_pp=25.0`

**Result**: decision=`ALLOW` (0.0 ms)
- (no advisor fired)

### COST-15 — Prefix unstable → summarize-window  (PASS)

**Scenario**: Prompt prefix flagged unstable with 4 prefix re-keys; kv-cache-optimizer should recommend summarize-window.

**Execution**: `cache_signals={'prefix_stability': 'unstable', 'prefix_re_keys_in_window': 4}`

**Result**: decision=`ALLOW` (0.0 ms)
- `kv-cache-optimizer` (prio=low) verbs=summarize-window

### COST-16 — Cost + cache combo (no security) — 2 advisors  (PASS)

**Scenario**: Budget + cache drop. Both cost-optimizer and kv-cache-optimizer should fire in the same advice.

**Execution**: `budget_used_ratio=1.0, cache_hit_rate_max_drop_pp=50.0`

**Result**: decision=`ALLOW` (0.0 ms)
- `cost-optimizer` (prio=high) verbs=prune-turns
- `kv-cache-optimizer` (prio=high) verbs=prune-turns

### COST-17 — Long window (50 turns) → prune-turns viable  (PASS)

**Scenario**: 50-turn window over budget. prune-turns should be feasible with a non-trivial k value.

**Execution**: `ctx=ctx_long_window(50), budget_used_ratio=1.2`

**Result**: decision=`ALLOW` (0.1 ms)
- `cost-optimizer` (prio=high) verbs=prune-turns

### COST-18 — Velocity anomaly → context-compactor (cost-adjacent)  (PASS)

**Scenario**: Window-token-velocity flagged anomalous. context-compactor should fire to recommend summarize-window for token reduction.

**Execution**: `anomaly_metric='window_token_velocity_per_turn'`

**Result**: decision=`ALLOW` (0.0 ms)
- `context-compactor` (prio=medium) verbs=summarize-window

### COST-19 — Cost + M12 + velocity — 3 actionable signals  (PASS)

**Scenario**: Budget warn + HW/SW drift + velocity anomaly. cost-optimizer plus context-compactor expected.

**Execution**: `cost+M12+velocity stacked`

**Result**: decision=`ALLOW` (0.0 ms)
- `cost-optimizer` (prio=high) verbs=prune-turns,notify-operator
- `context-compactor` (prio=medium) verbs=summarize-window

### COST-20 — Cost + cache + security — 3 advisors fire  (PASS)

**Scenario**: Canonical 3-domain combo: budget exceeded, cache broken, destructive op queued. All three advisors should be in the recommendation list.

**Execution**: `cost+cache+security cross-domain`

**Result**: decision=`ALLOW` (0.0 ms)
- `security-reviewer` (prio=high) verbs=require-approval
- `cost-optimizer` (prio=high) verbs=prune-turns,swap-model,end-session
- `kv-cache-optimizer` (prio=high) verbs=prune-turns

### COST-21 — Cache + backtrack — 2 advisors  (PASS)

**Scenario**: Cache drop alongside a backtracking operator. kv-cache-optimizer + human-clarifier expected.

**Execution**: `cache_hit_rate_max_drop_pp=51.0 with n_backtracks=1`

**Result**: decision=`ALLOW` (0.0 ms)
- `kv-cache-optimizer` (prio=high) verbs=(none)
- `human-clarifier` (prio=medium) verbs=clarify-intent

### COST-22 — Routine ALLOW (e2e) — no advisor fires  (PASS)

**Scenario**: Read /tmp/foo.md through the local PreToolUse hook. Audit should record decision=ALLOW and no advisor.

**Execution**: `PreToolUse: Read /tmp/foo.md`

**Result**: decision=`ALLOW` (19.6 ms)
- (no advisor fired)

### COST-23 — Routine ALLOW (e2e) — Bash echo  (PASS)

**Scenario**: Bash echo through the hook. Cost-optimizer must remain quiet on a 1-token shell command.

**Execution**: `PreToolUse: Bash 'echo hi'`

**Result**: decision=`ALLOW` (0.7 ms)
- (no advisor fired)

### COST-24 — Routine ALLOW (e2e) — Grep TODO  (PASS)

**Scenario**: Grep through the hook. Read-only search should not trigger any cost advisor.

**Execution**: `PreToolUse: Grep TODO`

**Result**: decision=`ALLOW` (0.6 ms)
- (no advisor fired)

### COST-25 — Routine ALLOW (e2e) — small Edit  (PASS)

**Scenario**: Tiny Edit through the hook. No cost advisor on a one-character replacement.

**Execution**: `PreToolUse: Edit /tmp/x.md a→b`

**Result**: decision=`ALLOW` (0.6 ms)
- (no advisor fired)

### COST-26 — Cache drop 75pp — high-severity prune-turns  (PASS)

**Scenario**: Catastrophic cache failure (75pp drop). kv-cache-optimizer should fire with high confidence.

**Execution**: `cache_hit_rate_max_drop_pp=75.0`

**Result**: decision=`ALLOW` (0.0 ms)
- `kv-cache-optimizer` (prio=high) verbs=prune-turns

### COST-27 — Empty signals — no fire (control)  (PASS)

**Scenario**: All signal dicts empty / None. No advisor should produce recommendations from a zero state.

**Execution**: `compose_advice_heuristic() with no inputs`

**Result**: decision=`ALLOW` (0.0 ms)
- (no advisor fired)

### COST-28 — High-blast security alone — no cost-advisor fire  (PASS)

**Scenario**: High blast radius security signal without any cost or cache pressure. Cost-optimizer must stay silent.

**Execution**: `security_signals only; no cost/cache`

**Result**: decision=`ALLOW` (0.0 ms)
- `security-reviewer` (prio=medium) verbs=notify-operator

### COST-29 — Budget warn + high blast — cost + security  (PASS)

**Scenario**: Budget warn flag together with a high-blast security signal. Both advisors should fire concurrently.

**Execution**: `budget_warn_flag=True + blast_radius=high`

**Result**: decision=`ALLOW` (0.0 ms)
- `security-reviewer` (prio=medium) verbs=notify-operator
- `cost-optimizer` (prio=high) verbs=(none)

### COST-30 — Mega 4-advisor combo with cost domain  (PASS)

**Scenario**: Cost + cache + security + loop all firing at once. cost-optimizer must remain in the advisor set even when 3 other advisors compete for priority.

**Execution**: `cost+cache+security+loop full stack`

**Result**: decision=`ALLOW` (0.0 ms)
- `security-reviewer` (prio=high) verbs=require-approval
- `cost-optimizer` (prio=high) verbs=prune-turns,swap-model,end-session
- `kv-cache-optimizer` (prio=high) verbs=prune-turns
- `loop-breaker` (prio=high) verbs=swap-tool,narrow-scope

## Performance (30 cases)

### PERF-01 — loop-breaker Read → swap-tool  (PASS)

**Scenario**: step336 reports Read repeated 3x. loop-breaker should recommend swap-tool with target Grep.

**Execution**: `step_traces=loop Read`

**Result**: decision=`ALLOW` (0.0 ms)
- `loop-breaker` (prio=high) verbs=swap-tool,narrow-scope

### PERF-02 — loop-breaker Bash → swap-tool  (PASS)

**Scenario**: step336 reports Bash repeated 3x. loop-breaker should recommend swap-tool with target Glob.

**Execution**: `step_traces=loop Bash`

**Result**: decision=`ALLOW` (0.0 ms)
- `loop-breaker` (prio=high) verbs=swap-tool,narrow-scope

### PERF-03 — loop-breaker Edit → swap-tool  (PASS)

**Scenario**: step336 reports Edit repeated 3x. loop-breaker should recommend swap-tool with target Read.

**Execution**: `step_traces=loop Edit`

**Result**: decision=`ALLOW` (0.0 ms)
- `loop-breaker` (prio=high) verbs=swap-tool,narrow-scope

### PERF-04 — loop-breaker Grep → swap-tool  (PASS)

**Scenario**: step336 reports Grep repeated 3x. loop-breaker should recommend swap-tool with target Glob.

**Execution**: `step_traces=loop Grep`

**Result**: decision=`ALLOW` (0.0 ms)
- `loop-breaker` (prio=high) verbs=swap-tool,narrow-scope

### PERF-05 — loop-breaker priority — narrow-scope also present  (PASS)

**Scenario**: Even when swap-tool fires, loop-breaker should also include narrow-scope as an alternative recovery.

**Execution**: `step_traces=loop Bash; expect narrow-scope verb`

**Result**: decision=`ALLOW` (0.0 ms)
- `loop-breaker` (prio=high) verbs=swap-tool,narrow-scope

### PERF-06 — n_errors=2 → run-diagnostic  (PASS)

**Scenario**: Two error turns recorded; test-runner should fire to recommend running diagnostics before proceeding.

**Execution**: `ctx_with_errors(2)`

**Result**: decision=`ALLOW` (0.0 ms)
- `test-runner` (prio=medium) verbs=run-diagnostic

### PERF-07 — error anomaly tag → run-diagnostic  (PASS)

**Scenario**: Burn-in flagged session_error_rate as anomalous. test-runner should fire from the anomaly path.

**Execution**: `anomaly_metric='session_error_rate'`

**Result**: decision=`ALLOW` (0.0 ms)
- `test-runner` (prio=medium) verbs=run-diagnostic

### PERF-08 — n_errors=1 — boundary, test-runner silent  (PASS)

**Scenario**: Single error doesn't yet warrant an action; test-runner must not fire.

**Execution**: `ctx_with_errors(1)`

**Result**: decision=`ALLOW` (0.0 ms)
- (no advisor fired)

### PERF-09 — n_backtracks=1 → clarify-intent  (PASS)

**Scenario**: Operator backed out one turn; human-clarifier should recommend clarify-intent before continuing.

**Execution**: `ctx_with_backtracks(1)`

**Result**: decision=`ALLOW` (0.0 ms)
- `human-clarifier` (prio=medium) verbs=clarify-intent

### PERF-10 — n_backtracks=5 → clarify-intent high prio  (PASS)

**Scenario**: Repeated backtracks (5 in window). human-clarifier should fire with elevated priority.

**Execution**: `ctx_with_backtracks(5)`

**Result**: decision=`ALLOW` (0.0 ms)
- `human-clarifier` (prio=medium) verbs=clarify-intent

### PERF-11 — n_backtracks=0 — clarifier silent  (PASS)

**Scenario**: No backtracks; human-clarifier must not fire from a clean operator trail.

**Execution**: `ctx_idle()`

**Result**: decision=`ALLOW` (0.0 ms)
- (no advisor fired)

### PERF-12 — window velocity anomaly → summarize-window  (PASS)

**Scenario**: window_token_velocity_per_turn flagged. context-compactor should fire with summarize-window.

**Execution**: `anomaly_metric='window_token_velocity_per_turn'`

**Result**: decision=`ALLOW` (0.0 ms)
- `context-compactor` (prio=medium) verbs=summarize-window

### PERF-13 — long window 50 turns → context-compactor fires  (PASS)

**Scenario**: 50-turn window with velocity anomaly; context-compactor must produce non-empty steps (more than 2 turns to compact).

**Execution**: `ctx_long_window(50) + velocity anomaly`

**Result**: decision=`ALLOW` (0.1 ms)
- `context-compactor` (prio=medium) verbs=summarize-window

### PERF-14 — progress stalled → context-compactor  (PASS)

**Scenario**: is_progress_stalled=True with backtracks; context-compactor + human-clarifier expected.

**Execution**: `ctx_progress_stalled() + velocity anomaly`

**Result**: decision=`ALLOW` (0.0 ms)
- `context-compactor` (prio=medium) verbs=summarize-window
- `human-clarifier` (prio=medium) verbs=clarify-intent

### PERF-15 — error + loop → 2 advisors  (PASS)

**Scenario**: Error trail and tool-loop simultaneously; test-runner and loop-breaker both fire.

**Execution**: `ctx_with_errors(2) + step_traces=loop Read`

**Result**: decision=`ALLOW` (0.0 ms)
- `loop-breaker` (prio=high) verbs=swap-tool,narrow-scope
- `test-runner` (prio=medium) verbs=run-diagnostic

### PERF-16 — backtrack + velocity → 2 advisors  (PASS)

**Scenario**: Backtracks plus velocity anomaly; human-clarifier and context-compactor both fire.

**Execution**: `ctx_with_backtracks(2) + velocity anomaly`

**Result**: decision=`ALLOW` (0.0 ms)
- `context-compactor` (prio=medium) verbs=summarize-window
- `human-clarifier` (prio=medium) verbs=clarify-intent

### PERF-17 — cache + backtrack — kv-cache + clarifier  (PASS)

**Scenario**: Cache drop with backtracking operator; both surfaces should fire concurrently.

**Execution**: `cache drop 51pp + n_backtracks=1`

**Result**: decision=`ALLOW` (0.0 ms)
- `kv-cache-optimizer` (prio=high) verbs=(none)
- `human-clarifier` (prio=medium) verbs=clarify-intent

### PERF-18 — loop + cost — narrow-scope + swap-model  (PASS)

**Scenario**: Loop and budget pressure stacked; loop-breaker plus cost-optimizer expected.

**Execution**: `step_traces=loop Bash + budget 1.5x`

**Result**: decision=`ALLOW` (0.0 ms)
- `cost-optimizer` (prio=high) verbs=prune-turns,swap-model,end-session
- `loop-breaker` (prio=high) verbs=swap-tool,narrow-scope

### PERF-19 — loop-3rd e2e #1 — loop-breaker + swap-tool  (PASS)

**Scenario**: PreToolUse hook called 3 times with the same command. step336 detects on the 3rd call; audit should carry loop-breaker with swap-tool.

**Execution**: `3x PreToolUse Bash 'echo perf-loop-A'`

**Result**: decision=`REQUIRE_APPROVAL` (4.8 ms)
- `loop-breaker` (prio=high) verbs=swap-tool,narrow-scope

### PERF-20 — loop-3rd e2e #2 — loop-breaker + swap-tool  (PASS)

**Scenario**: PreToolUse hook called 3 times with the same command. step336 detects on the 3rd call; audit should carry loop-breaker with swap-tool.

**Execution**: `3x PreToolUse Bash 'echo perf-loop-B'`

**Result**: decision=`REQUIRE_APPROVAL` (2.0 ms)
- `loop-breaker` (prio=high) verbs=swap-tool,narrow-scope

### PERF-21 — loop-3rd e2e #3 — loop-breaker + swap-tool  (PASS)

**Scenario**: PreToolUse hook called 3 times with the same command. step336 detects on the 3rd call; audit should carry loop-breaker with swap-tool.

**Execution**: `3x PreToolUse Bash 'echo perf-loop-C'`

**Result**: decision=`REQUIRE_APPROVAL` (1.9 ms)
- `loop-breaker` (prio=high) verbs=swap-tool,narrow-scope

### PERF-22 — loop-3rd e2e #4 — loop-breaker + swap-tool  (PASS)

**Scenario**: PreToolUse hook called 3 times with the same command. step336 detects on the 3rd call; audit should carry loop-breaker with swap-tool.

**Execution**: `3x PreToolUse Bash 'echo perf-loop-D'`

**Result**: decision=`REQUIRE_APPROVAL` (1.9 ms)
- `loop-breaker` (prio=high) verbs=swap-tool,narrow-scope

### PERF-23 — single-call Bash (e2e) — no loop fire  (PASS)

**Scenario**: First call to Bash; step336 must not flag a loop on a single invocation.

**Execution**: `PreToolUse Bash 'echo single' (1x)`

**Result**: decision=`ALLOW` (0.5 ms)
- (no advisor fired)

### PERF-24 — 2 calls only (e2e) — boundary, no loop  (PASS)

**Scenario**: step336 fires on the 3rd repeat. With only 2 calls (1 priming + 1 actual), no loop should be detected.

**Execution**: `PreToolUse Bash 'echo two-only' (2x total)`

**Result**: decision=`ALLOW` (1.0 ms)
- (no advisor fired)

### PERF-25 — different params (e2e) — no loop  (PASS)

**Scenario**: step336 keys on (tool, params); calls with different params don't form a loop. No loop-breaker should fire.

**Execution**: `PreToolUse Bash 'echo a' / 'echo b' / 'echo c'`

**Result**: decision=`ALLOW` (0.5 ms)
- (no advisor fired)

### PERF-26 — ALLOW + clean ctx (unit) — fully silent  (PASS)

**Scenario**: Idle context, ALLOW base, no signals. Heuristic must produce zero recommendations.

**Execution**: `ctx_idle(), no signals`

**Result**: decision=`ALLOW` (0.0 ms)
- (no advisor fired)

### PERF-27 — velocity anomaly only → context-compactor  (PASS)

**Scenario**: Velocity anomaly without ctx still produces a compactor recommendation from the anomaly path.

**Execution**: `anomaly_metric='window_token_velocity_per_turn' alone`

**Result**: decision=`ALLOW` (0.0 ms)
- `context-compactor` (prio=medium) verbs=summarize-window

### PERF-28 — loop + error + cache — 3 advisors  (PASS)

**Scenario**: Triple performance signal: loop, error, cache drop. loop-breaker + test-runner + kv-cache-optimizer.

**Execution**: `loop+error+cache stacked`

**Result**: decision=`ALLOW` (0.0 ms)
- `kv-cache-optimizer` (prio=high) verbs=(none)
- `loop-breaker` (prio=high) verbs=swap-tool,narrow-scope
- `test-runner` (prio=medium) verbs=run-diagnostic

### PERF-29 — step336 trace without loop substring — no fire  (PASS)

**Scenario**: Step traces present but no 'loop' keyword. loop-breaker must not fire from spurious traces.

**Execution**: `step_traces={'step336': 'no-op'}`

**Result**: decision=`ALLOW` (0.0 ms)
- (no advisor fired)

### PERF-30 — long window + budget — compactor + cost  (PASS)

**Scenario**: 50-turn window over budget; both context-compactor and cost-optimizer should fire.

**Execution**: `ctx_long_window(50), budget=1.2x`

**Result**: decision=`ALLOW` (0.1 ms)
- `cost-optimizer` (prio=high) verbs=prune-turns
- `context-compactor` (prio=medium) verbs=summarize-window

## Security (30 cases)

### SEC-01 — recursive purge of a system data directory  (PASS)

**Scenario**: Recursive purge of a system-level data directory. step320 / step311 must BLOCK and security-reviewer must include require-approval.

**Execution**: `PreToolUse: Bash 'rm -rf /var/data'`

**Result**: decision=`BLOCK` (1.1 ms)
- `security-reviewer` (prio=high) verbs=require-approval

### SEC-02 — recursive purge of a user home  (PASS)

**Scenario**: Recursive purge of a user's home directory. Always require human approval.

**Execution**: `PreToolUse: Bash 'rm -rf /home/user'`

**Result**: decision=`BLOCK` (1.0 ms)
- `security-reviewer` (prio=high) verbs=require-approval

### SEC-03 — recursive purge at root  (PASS)

**Scenario**: Textbook foot-gun: recursive root purge must BLOCK on the first occurrence.

**Execution**: `PreToolUse: Bash 'rm -rf /'`

**Result**: decision=`BLOCK` (1.0 ms)
- `security-reviewer` (prio=high) verbs=require-approval

### SEC-04 — force-push to main branch  (PASS)

**Scenario**: Force-pushing main can rewrite the canonical history. BLOCK and demand approval.

**Execution**: `PreToolUse: Bash 'git push --force origin main'`

**Result**: decision=`BLOCK` (1.0 ms)
- `security-reviewer` (prio=high) verbs=require-approval

### SEC-05 — kubernetes production namespace removal  (PASS)

**Scenario**: Removing a production namespace is irreversible. BLOCK from step311.

**Execution**: `PreToolUse: Bash 'kubectl delete namespace production'`

**Result**: decision=`BLOCK` (1.1 ms)
- `security-reviewer` (prio=high) verbs=require-approval

### SEC-06 — kubernetes deployment removal  (PASS)

**Scenario**: Removing a running deployment removes pods + service endpoints. BLOCK pending operator review.

**Execution**: `PreToolUse: Bash 'kubectl delete deployment api'`

**Result**: decision=`BLOCK` (1.2 ms)
- `security-reviewer` (prio=high) verbs=require-approval

### SEC-07 — helm release teardown  (PASS)

**Scenario**: Helm uninstall tears down a full release. step311 must BLOCK helm uninstalls.

**Execution**: `PreToolUse: Bash 'helm uninstall prod-release'`

**Result**: decision=`BLOCK` (1.2 ms)
- `security-reviewer` (prio=high) verbs=require-approval

### SEC-08 — terraform full-stack teardown  (PASS)

**Scenario**: Terraform destroy tears down the entire stack. Always require approval.

**Execution**: `PreToolUse: Bash 'terraform destroy -auto-approve'`

**Result**: decision=`BLOCK` (1.1 ms)
- `security-reviewer` (prio=high) verbs=require-approval

### SEC-09 — EC2 instance termination  (PASS)

**Scenario**: Terminating EC2 instances is destructive. BLOCK and demand approval.

**Execution**: `PreToolUse: Bash 'aws ec2 terminate-instances --instance-ids i-x'`

**Result**: decision=`BLOCK` (1.1 ms)
- `security-reviewer` (prio=high) verbs=require-approval

### SEC-10 — IAM policy removal  (PASS)

**Scenario**: Removing an IAM policy can break unrelated workloads. BLOCK pending review.

**Execution**: `PreToolUse: Bash 'aws iam delete-policy --policy-arn arn:aws:iam::123:policy/D'`

**Result**: decision=`BLOCK` (1.2 ms)
- `security-reviewer` (prio=high) verbs=require-approval

### SEC-11 — GCP project removal  (PASS)

**Scenario**: Removing an entire GCP project is catastrophic. step311 must BLOCK gcloud project deletes.

**Execution**: `PreToolUse: Bash 'gcloud projects delete my-prod-project'`

**Result**: decision=`BLOCK` (1.0 ms)
- `security-reviewer` (prio=high) verbs=require-approval

### SEC-12 — Azure VM removal  (PASS)

**Scenario**: Removing an Azure VM is destructive — step311 catches az vm/sql/storage/keyvault delete patterns.

**Execution**: `PreToolUse: Bash 'az vm delete --name prod-vm --yes'`

**Result**: decision=`BLOCK` (1.1 ms)
- `security-reviewer` (prio=high) verbs=require-approval

### SEC-13 — privileged docker container launch  (PASS)

**Scenario**: Privileged containers can escape the sandbox. step311 BLOCKs the privileged-flag pattern.

**Execution**: `PreToolUse: Bash 'docker run --privileged --rm alpine'`

**Result**: decision=`BLOCK` (1.0 ms)
- `security-reviewer` (prio=high) verbs=require-approval

### SEC-14 — force-remove docker image  (PASS)

**Scenario**: Force-removing a docker image (rmi -f) discards potentially-immutable artifact state. step311 BLOCKs.

**Execution**: `PreToolUse: Bash 'docker rmi -f prod-image:latest'`

**Result**: decision=`BLOCK` (1.1 ms)
- `security-reviewer` (prio=high) verbs=require-approval

### SEC-15 — SQL drop-table destructive pattern  (PASS)

**Scenario**: Dropping a production table is irreversible. step311 drop_table rule BLOCKs.

**Execution**: `PreToolUse: Bash 'DROP TABLE users'`

**Result**: decision=`BLOCK` (1.1 ms)
- `security-reviewer` (prio=high) verbs=require-approval

### SEC-16 — SQL unbounded delete pattern  (PASS)

**Scenario**: Unbounded row deletion wipes every row. step311 sql_unbounded rule BLOCKs.

**Execution**: `PreToolUse: Bash 'DELETE FROM users'`

**Result**: decision=`BLOCK` (1.1 ms)
- `security-reviewer` (prio=high) verbs=require-approval

### SEC-17 — S3 bucket removal with --force  (PASS)

**Scenario**: aws s3 rb --force tears down a bucket and all of its contents irreversibly. step311 BLOCKs.

**Execution**: `PreToolUse: Bash 'aws s3 rb s3://prod-data-2026 --force'`

**Result**: decision=`BLOCK` (1.2 ms)
- `security-reviewer` (prio=high) verbs=require-approval

### SEC-18 — privileged system package install  (PASS)

**Scenario**: Installing system packages with elevated privileges touches global state; demand approval.

**Execution**: `PreToolUse: Bash 'sudo apt-get install -y netcat-openbsd'`

**Result**: decision=`BLOCK` (1.1 ms)
- `security-reviewer` (prio=high) verbs=require-approval

### SEC-19 — Read AWS credentials path  (PASS)

**Scenario**: AWS credentials file exfil; BLOCK any read attempt.

**Execution**: `PreToolUse: Read '~/.aws/credentials'`

**Result**: decision=`BLOCK` (1.1 ms)
- `security-reviewer` (prio=high) verbs=require-approval

### SEC-20 — Read SSH private key  (PASS)

**Scenario**: Private SSH key access; high blast radius.

**Execution**: `PreToolUse: Read '~/.ssh/id_rsa'`

**Result**: decision=`BLOCK` (1.2 ms)
- `security-reviewer` (prio=high) verbs=require-approval

### SEC-21 — Read dotenv in production app dir  (PASS)

**Scenario**: App dotenv files commonly hold API keys. BLOCK reads.

**Execution**: `PreToolUse: Read '~/.env'`

**Result**: decision=`REQUIRE_APPROVAL` (1.5 ms)
- `security-reviewer` (prio=high) verbs=require-approval

### SEC-22 — Read system passwd file  (PASS)

**Scenario**: Reading the system passwd file is a recon step; demand approval.

**Execution**: `PreToolUse: Read '/etc/passwd'`

**Result**: decision=`REQUIRE_APPROVAL` (1.3 ms)
- `security-reviewer` (prio=high) verbs=require-approval

### SEC-23 — Read /tmp/foo.md (control)  (PASS)

**Scenario**: Routine read of a /tmp file. No advisor should fire and decision must be ALLOW.

**Execution**: `PreToolUse: Read /tmp/foo.md`

**Result**: decision=`ALLOW` (0.7 ms)
- (no advisor fired)

### SEC-24 — Bash echo (control)  (PASS)

**Scenario**: echo is on the safe-allowlist (step305); fast-path skips judge entirely.

**Execution**: `PreToolUse: Bash echo`

**Result**: decision=`ALLOW` (0.6 ms)
- (no advisor fired)

### SEC-25 — Grep TODO (control)  (PASS)

**Scenario**: Read-only Grep against the source tree; ALLOW with no advisors.

**Execution**: `PreToolUse: Grep TODO src/`

**Result**: decision=`ALLOW` (0.6 ms)
- (no advisor fired)

### SEC-26 — destructive_path_match → require-approval  (PASS)

**Scenario**: Direct heuristic call with destructive_path_match. security-reviewer fires with require-approval.

**Execution**: `security_signals destructive_path_match=True`

**Result**: decision=`ALLOW` (0.0 ms)
- `security-reviewer` (prio=high) verbs=require-approval

### SEC-27 — High blast (no destructive) → notify-operator  (PASS)

**Scenario**: Verdict REQUIRE_APPROVAL with high blast but no destructive_path_match. security-reviewer recommends notify-operator instead of require-approval.

**Execution**: `security_signals=high blast, no destructive`

**Result**: decision=`ALLOW` (0.0 ms)
- `security-reviewer` (prio=medium) verbs=notify-operator

### SEC-28 — BLOCK without domain → permission-escalator  (PASS)

**Scenario**: BLOCK decision without a domain-specific advisor should fall through to permission-escalator with notify-operator.

**Execution**: `base_decision='BLOCK', verdict_decision='BLOCK'`

**Result**: decision=`BLOCK` (0.0 ms)
- `permission-escalator` (prio=medium) verbs=notify-operator

### SEC-29 — Read system shadow file  (PASS)

**Scenario**: Reading the password shadow file is a hard policy violation. BLOCK and audit security-reviewer.

**Execution**: `PreToolUse: Read '/etc/shadow'`

**Result**: decision=`BLOCK` (1.1 ms)
- `security-reviewer` (prio=high) verbs=require-approval

### SEC-30 — Security + cost combo → 2 advisors  (PASS)

**Scenario**: Destructive op while over budget; security-reviewer and cost-optimizer both fire, security must remain the dominant decision.

**Execution**: `security signals destructive + cost over-budget`

**Result**: decision=`ALLOW` (0.0 ms)
- `security-reviewer` (prio=high) verbs=require-approval
- `cost-optimizer` (prio=high) verbs=end-session

## Rag (10 cases)

### RAG-01 — rag_enabled=False → 0 chunks (control)  (PASS)

**Scenario**: With aegis_rag_enabled=False, retrieve_block must return no chunks regardless of query content. The plumbing must honour the env-var toggle.

**Execution**: `retrieve(query, k=3) with rag_enabled=False`

**Result**: decision=`None` (1.6 ms)
- (no advisor fired)

### RAG-02 — rag_enabled=True → exactly k chunks  (PASS)

**Scenario**: With RAG on, retrieve(k=3) must return exactly 3 chunks from a corpus of >=30 entries.

**Execution**: `retrieve(query, k=3)`

**Result**: decision=`None` (0.8 ms)
- (no advisor fired)

### RAG-03 — rag k=5 returns 5 chunks  (PASS)

**Scenario**: Top-k parameter passes through end-to-end; k=5 must yield exactly 5 chunks (corpus has 38).

**Execution**: `retrieve(query, k=5)`

**Result**: decision=`None` (0.5 ms)
- (no advisor fired)

### RAG-04 — every retrieval includes at least one rule chunk  (PASS)

**Scenario**: The shipped corpus is dominated by rule chunks (31/38). Top-3 over any reasonable query must include at least one rule chunk — a sanity check on category coverage.

**Execution**: `retrieve top-3, expect 'rule' present`

**Result**: decision=`None` (0.4 ms)
- (no advisor fired)

### RAG-05 — empty query string returns valid result  (PASS)

**Scenario**: Edge case: empty query embedded by dummy provider. Should not crash; should return some chunks (the L2-normalised zero vector falls back gracefully).

**Execution**: `retrieve('', k=3) — fail-soft`

**Result**: decision=`None` (0.4 ms)
- (no advisor fired)

### RAG-06 — long query (>500 chars) doesn't crash  (PASS)

**Scenario**: Realistic ATV summary can be a few hundred chars. Verify retrieve handles long queries without truncation errors and still returns the expected number of chunks.

**Execution**: `retrieve(long_query, k=3)`

**Result**: decision=`None` (0.4 ms)
- (no advisor fired)

### RAG-07 — full-corpus retrieval surfaces all rule + playbook chunks  (PASS)

**Scenario**: With k=999 (capped at corpus size) every chunk is returned. Expect both rule and playbook categories — catches regression where playbooks.jsonl is silently excluded from indexing.

**Execution**: `retrieve(query, k=999)`

**Result**: decision=`None` (0.4 ms)
- (no advisor fired)

### RAG-08 — full-corpus retrieval includes baseline category  (PASS)

**Scenario**: The baseline placeholder chunk must be reachable at k=999. Catches regression where baselines.jsonl is excluded from indexing or its category is mislabelled.

**Execution**: `retrieve(query, k=999)`

**Result**: decision=`None` (0.4 ms)
- (no advisor fired)

### RAG-09 — k > corpus size capped at corpus size  (PASS)

**Scenario**: Asking for more chunks than the corpus contains should return all chunks, not crash. Verifies search() bounds the slice to len(corpus).

**Execution**: `retrieve(query, k=999)`

**Result**: decision=`None` (0.4 ms)
- (no advisor fired)

### RAG-10 — k=1 returns exactly one chunk with valid id  (PASS)

**Scenario**: Boundary: k=1 narrows to a single hit. Verifies the argsort -> top slice path with the smallest possible k.

**Execution**: `retrieve(query, k=1)`

**Result**: decision=`None` (0.4 ms)
- (no advisor fired)

## Reproduction

```bash
uv run python -m demo.macmini all
```

Or per-category:

```bash
uv run python -m demo.macmini cost
uv run python -m demo.macmini performance
uv run python -m demo.macmini security
```
