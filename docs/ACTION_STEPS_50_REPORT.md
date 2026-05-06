# 50-case ActionStep Validation Report

Driver: `demo/action_steps_50_validation.py`
Audit:  `/tmp/action-steps-50-audit.jsonl`

Validates the v2.8 ActionStep surface end-to-end across three groups: e2e through hooks (Group A), unit calls to the heuristic composer (Group B — covers all 11 verbs), and cross-domain combinations (Group C).

## Headline

- **Total:** 50
- **Pass:** 50 (100%)
- **Fail:** 0

## By group

| Group | Description | Cases | Pass | Pass% |
|-------|-------------|-------|------|-------|
| A | e2e (hooks) | 15 | 15 | 100% |
| B | unit (heuristic verb sweep) | 25 | 25 | 100% |
| C | unit (cross-domain combos) | 10 | 10 | 100% |

## Advisor frequency

| Advisor | Count |
|---------|-------|
| `security-reviewer` | 14 |
| `cost-optimizer` | 12 |
| `loop-breaker` | 9 |
| `kv-cache-optimizer` | 6 |
| `human-clarifier` | 4 |
| `test-runner` | 3 |
| `context-compactor` | 3 |
| `permission-escalator` | 1 |

## Verb frequency (across all action_steps)

| Verb | Count |
|------|-------|
| `require-approval` | 12 |
| `prune-turns` | 10 |
| `swap-tool` | 9 |
| `narrow-scope` | 9 |
| `notify-operator` | 8 |
| `swap-model` | 5 |
| `end-session` | 5 |
| `summarize-window` | 4 |
| `clarify-intent` | 4 |
| `run-diagnostic` | 3 |

## Group A — e2e (hooks)

### ✅ Case 1: Read /tmp/foo.md

- **Expected**: no advisor fires
- **Result**: —
- decision: `ALLOW`

### ✅ Case 2: Bash ls

- **Expected**: no advisor fires
- **Result**: —
- decision: `ALLOW`

### ✅ Case 3: Bash echo

- **Expected**: no advisor fires
- **Result**: —
- decision: `ALLOW`

### ✅ Case 4: Edit small file

- **Expected**: no advisor fires
- **Result**: —
- decision: `ALLOW`

### ✅ Case 5: Grep TODO

- **Expected**: no advisor fires
- **Result**: —
- decision: `ALLOW`

### ✅ Case 6: git force-push → BLOCK

- **Expected**: `security-reviewer` with verb `require-approval`
- **Result**: security-reviewer@high[require-approval]
- decision: `BLOCK`
  - top step: `require-approval` (conf=0.95)
    → blocks tool execution until human ACK

### ✅ Case 7: recursive purge /var/data

- **Expected**: `security-reviewer` with verb `require-approval`
- **Result**: security-reviewer@high[require-approval]
- decision: `BLOCK`
  - top step: `require-approval` (conf=0.95)
    → blocks tool execution until human ACK

### ✅ Case 8: recursive purge /home/user

- **Expected**: `security-reviewer` with verb `require-approval`
- **Result**: security-reviewer@high[require-approval]
- decision: `BLOCK`
  - top step: `require-approval` (conf=0.95)
    → blocks tool execution until human ACK

### ✅ Case 9: destructive table drop

- **Expected**: `security-reviewer` with verb `require-approval`
- **Result**: security-reviewer@high[require-approval]
- decision: `BLOCK`
  - top step: `require-approval` (conf=0.95)
    → blocks tool execution until human ACK

### ✅ Case 10: kubectl delete prod

- **Expected**: `security-reviewer` with verb `require-approval`
- **Result**: security-reviewer@high[require-approval]
- decision: `BLOCK`
  - top step: `require-approval` (conf=0.95)
    → blocks tool execution until human ACK

### ✅ Case 11: aws ec2 terminate

- **Expected**: `security-reviewer` with verb `require-approval`
- **Result**: security-reviewer@high[require-approval]
- decision: `BLOCK`
  - top step: `require-approval` (conf=0.95)
    → blocks tool execution until human ACK

### ✅ Case 12: terraform destroy

- **Expected**: `security-reviewer` with verb `require-approval`
- **Result**: security-reviewer@high[require-approval]
- decision: `BLOCK`
  - top step: `require-approval` (conf=0.95)
    → blocks tool execution until human ACK

### ✅ Case 13: privileged docker

- **Expected**: `security-reviewer` with verb `require-approval`
- **Result**: security-reviewer@high[require-approval]
- decision: `BLOCK`
  - top step: `require-approval` (conf=0.95)
    → blocks tool execution until human ACK

### ✅ Case 14: loop-3rd e2e set #1

- **Expected**: `loop-breaker` with verb `swap-tool`
- **Result**: loop-breaker@high[swap-tool,narrow-scope]
- decision: `REQUIRE_APPROVAL`
  - top step: `swap-tool` (conf=0.70)
    → break the loop by switching to Glob

### ✅ Case 15: loop-3rd e2e set #2

- **Expected**: `loop-breaker` with verb `swap-tool`
- **Result**: loop-breaker@high[swap-tool,narrow-scope]
- decision: `REQUIRE_APPROVAL`
  - top step: `swap-tool` (conf=0.70)
    → break the loop by switching to Glob

## Group B — unit (heuristic verb sweep)

### ✅ Case 16: cost-optimizer at budget 1.6× → prune+swap+end

- **Expected**: `cost-optimizer` with verb `prune-turns`
- **Result**: cost-optimizer@high[prune-turns,swap-model]
  - top step: `prune-turns` (conf=0.70)
    → ~15000 tokens trimmed (~$0.045 saved)

### ✅ Case 17: cost-optimizer at budget 1.6× → swap-model present

- **Expected**: `cost-optimizer` with verb `swap-model`
- **Result**: cost-optimizer@high[prune-turns,swap-model]
  - top step: `prune-turns` (conf=0.70)
    → ~15000 tokens trimmed (~$0.045 saved)

### ✅ Case 18: cost-optimizer at budget 1.6× → end-session present

- **Expected**: `cost-optimizer` with verb `end-session`
- **Result**: cost-optimizer@high[prune-turns,swap-model]
  - top step: `prune-turns` (conf=0.70)
    → ~15000 tokens trimmed (~$0.045 saved)

### ✅ Case 19: M12 ratio 2.0 → notify-operator

- **Expected**: `cost-optimizer` with verb `notify-operator`
- **Result**: cost-optimizer@high[notify-operator]
  - top step: `notify-operator` (conf=0.85)
    → cost / security team aware of HW-vs-SW mismatch

### ✅ Case 20: M12 ratio 3.15 → notify-operator

- **Expected**: `cost-optimizer` with verb `notify-operator`
- **Result**: cost-optimizer@high[notify-operator]
  - top step: `notify-operator` (conf=0.85)
    → cost / security team aware of HW-vs-SW mismatch

### ✅ Case 21: M12 ratio 5.0 → notify-operator

- **Expected**: `cost-optimizer` with verb `notify-operator`
- **Result**: cost-optimizer@high[notify-operator]
  - top step: `notify-operator` (conf=0.85)
    → cost / security team aware of HW-vs-SW mismatch

### ✅ Case 22: M12 ratio 1.99 → no fire (boundary)

- **Expected**: no advisor fires
- **Result**: —

### ✅ Case 23: budget warn flag only → cost-optimizer

- **Expected**: `cost-optimizer` 
- **Result**: cost-optimizer@medium[(no steps)]

### ✅ Case 24: cache drop 51pp → prune-turns

- **Expected**: `kv-cache-optimizer` with verb `prune-turns`
- **Result**: kv-cache-optimizer@high[prune-turns]
  - top step: `prune-turns` (conf=0.75)
    → prune 1 cache-breaking turns; prefix re-stabilises

### ✅ Case 25: prefix unstable → summarize-window

- **Expected**: `kv-cache-optimizer` with verb `summarize-window`
- **Result**: kv-cache-optimizer@low[summarize-window]
  - top step: `summarize-window` (conf=0.50)
    → collapse unstable prefix into a summary

### ✅ Case 26: cache drop 25pp → no kv-cache fire

- **Expected**: `kv-cache-optimizer` does NOT fire
- **Result**: —

### ✅ Case 27: destructive path match → require-approval

- **Expected**: `security-reviewer` with verb `require-approval`
- **Result**: security-reviewer@high[require-approval]
  - top step: `require-approval` (conf=0.95)
    → blocks tool execution until human ACK

### ✅ Case 28: high blast (no destructive) → notify-operator

- **Expected**: `security-reviewer` with verb `notify-operator`
- **Result**: security-reviewer@medium[notify-operator]
  - top step: `notify-operator` (conf=0.70)
    → security operator informed

### ✅ Case 29: loop-breaker Read → swap-tool

- **Expected**: `loop-breaker` with verb `swap-tool`
- **Result**: loop-breaker@high[swap-tool,narrow-scope]
  - top step: `swap-tool` (conf=0.70)
    → break the loop by switching to Grep

### ✅ Case 30: loop-breaker Bash → swap-tool

- **Expected**: `loop-breaker` with verb `swap-tool`
- **Result**: loop-breaker@high[swap-tool,narrow-scope]
  - top step: `swap-tool` (conf=0.70)
    → break the loop by switching to Glob

### ✅ Case 31: loop-breaker Edit → swap-tool

- **Expected**: `loop-breaker` with verb `swap-tool`
- **Result**: loop-breaker@high[swap-tool,narrow-scope]
  - top step: `swap-tool` (conf=0.70)
    → break the loop by switching to Read

### ✅ Case 32: loop-breaker Grep → swap-tool

- **Expected**: `loop-breaker` with verb `swap-tool`
- **Result**: loop-breaker@high[swap-tool,narrow-scope]
  - top step: `swap-tool` (conf=0.70)
    → break the loop by switching to Glob

### ✅ Case 33: n_backtracks=1 → clarify-intent

- **Expected**: `human-clarifier` with verb `clarify-intent`
- **Result**: human-clarifier@medium[clarify-intent]
  - top step: `clarify-intent` (conf=0.65)
    → user re-states intent → agent stops oscillating

### ✅ Case 34: n_backtracks=5 → clarify-intent

- **Expected**: `human-clarifier` with verb `clarify-intent`
- **Result**: human-clarifier@medium[clarify-intent]
  - top step: `clarify-intent` (conf=0.65)
    → user re-states intent → agent stops oscillating

### ✅ Case 35: n_errors=2 → run-diagnostic

- **Expected**: `test-runner` with verb `run-diagnostic`
- **Result**: test-runner@medium[run-diagnostic]
  - top step: `run-diagnostic` (conf=0.50)
    → isolates whether the recent error is reproducible

### ✅ Case 36: error anomaly tag → run-diagnostic

- **Expected**: `test-runner` with verb `run-diagnostic`
- **Result**: test-runner@medium[run-diagnostic]
  - top step: `run-diagnostic` (conf=0.50)
    → isolates whether the recent error is reproducible

### ✅ Case 37: n_errors=1 → no test-runner fire

- **Expected**: `test-runner` does NOT fire
- **Result**: —

### ✅ Case 38: velocity anomaly → summarize-window

- **Expected**: `context-compactor` with verb `summarize-window`
- **Result**: context-compactor@medium[summarize-window]
  - top step: `summarize-window` (conf=0.50)
    → compact the early window; recent 2 turns kept verbatim

### ✅ Case 39: BLOCK without domain → permission-escalator

- **Expected**: `permission-escalator` with verb `notify-operator`
- **Result**: permission-escalator@medium[notify-operator]
  - top step: `notify-operator` (conf=0.60)
    → operator notified; awaits manual decision

### ✅ Case 40: ALLOW + no signals → no fire (clean)

- **Expected**: no advisor fires
- **Result**: —

## Group C — unit (cross-domain combos)

### ✅ Case 41: cost+cache+security → 3 advisors fire

- **Expected multi**: `cost-optimizer`, `kv-cache-optimizer`, `security-reviewer`
- **Result**: security-reviewer@high[require-approval] | cost-optimizer@high[prune-turns,swap-model] | kv-cache-optimizer@high[prune-turns]
  - top step: `require-approval` (conf=0.95)
    → blocks tool execution until human ACK

### ✅ Case 42: cost+cache (no security) → 2 advisors

- **Expected multi**: `cost-optimizer`, `kv-cache-optimizer`
- **Result**: cost-optimizer@high[prune-turns] | kv-cache-optimizer@high[prune-turns]
  - top step: `prune-turns` (conf=0.70)
    → ~15000 tokens trimmed (~$0.045 saved)

### ✅ Case 43: security + loop → 2 advisors

- **Expected multi**: `security-reviewer`, `loop-breaker`
- **Result**: security-reviewer@high[require-approval] | loop-breaker@high[swap-tool,narrow-scope]
  - top step: `require-approval` (conf=0.95)
    → blocks tool execution until human ACK

### ✅ Case 44: backtrack + velocity → 2 advisors

- **Expected multi**: `human-clarifier`, `context-compactor`
- **Result**: context-compactor@medium[summarize-window] | human-clarifier@medium[clarify-intent]
  - top step: `summarize-window` (conf=0.50)
    → compact the early window; recent 2 turns kept verbatim

### ✅ Case 45: error + loop → 2 advisors

- **Expected multi**: `test-runner`, `loop-breaker`
- **Result**: loop-breaker@high[swap-tool,narrow-scope] | test-runner@medium[run-diagnostic]
  - top step: `swap-tool` (conf=0.70)
    → break the loop by switching to Grep

### ✅ Case 46: M12 + velocity → cost + compactor

- **Expected multi**: `cost-optimizer`, `context-compactor`
- **Result**: cost-optimizer@high[notify-operator] | context-compactor@medium[summarize-window]
  - top step: `notify-operator` (conf=0.85)
    → cost / security team aware of HW-vs-SW mismatch

### ✅ Case 47: cache + backtrack → 2 advisors

- **Expected multi**: `kv-cache-optimizer`, `human-clarifier`
- **Result**: kv-cache-optimizer@high[(no steps)] | human-clarifier@medium[clarify-intent]

### ✅ Case 48: high blast + budget → security + cost

- **Expected multi**: `security-reviewer`, `cost-optimizer`
- **Result**: security-reviewer@medium[notify-operator] | cost-optimizer@high[(no steps)]
  - top step: `notify-operator` (conf=0.70)
    → security operator informed

### ✅ Case 49: 4-advisor combo (cost+cache+sec+loop)

- **Expected multi**: `cost-optimizer`, `kv-cache-optimizer`, `security-reviewer`, `loop-breaker`
- **Result**: security-reviewer@high[require-approval] | cost-optimizer@high[prune-turns,swap-model] | kv-cache-optimizer@high[prune-turns] | +1 more
  - top step: `require-approval` (conf=0.95)
    → blocks tool execution until human ACK

### ✅ Case 50: no signals → no recommendations

- **Expected**: no advisor fires
- **Result**: —

## Reproduction

```bash
uv run python demo/action_steps_50_validation.py
```
