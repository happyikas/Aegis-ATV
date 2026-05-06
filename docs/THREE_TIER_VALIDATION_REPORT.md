# 3-Tier Validation Report - 100 cases

Driver: `demo/three_tier_validation.py`
Audit:  `/tmp/three-tier-validation-audit.jsonl`
Records emitted: 200

## Headline

- **Total:** 100
- **Pass:** 100 (100%)
- **Fail:** 0

## By tier

| Tier | Cases | Pass | Pass% |
|------|-------|------|-------|
| 1 | 93 | 93 | 100% |
| 3 | 7 | 7 | 100% |

## By category

| Category | Tier | Cases | Pass | Pass% |
|----------|------|-------|------|-------|
| `destructive_block` | 1 | 13 | 13 | 100% |
| `destructive_lookalike` | 1 | 2 | 2 | 100% |
| `loop_3rd` | 1 | 4 | 4 | 100% |
| `loop_pre` | 1 | 8 | 8 | 100% |
| `multi_domain_combo` | 3 | 1 | 1 | 100% |
| `multi_domain_security` | 3 | 2 | 2 | 100% |
| `read_redundant_2nd` | 1 | 2 | 2 | 100% |
| `read_redundant_3rd` | 1 | 2 | 2 | 100% |
| `read_redundant_first` | 1 | 2 | 2 | 100% |
| `retro_accurate` | 3 | 3 | 3 | 100% |
| `retro_missed_signal` | 3 | 1 | 1 | 100% |
| `retro_not_applicable` | 1 | 1 | 1 | 100% |
| `routine_bash` | 1 | 15 | 15 | 100% |
| `routine_edit` | 1 | 8 | 8 | 100% |
| `routine_misc` | 1 | 9 | 9 | 100% |
| `routine_read` | 1 | 18 | 18 | 100% |
| `routine_search` | 1 | 7 | 7 | 100% |
| `sensitive_path_read` | 1 | 2 | 2 | 100% |

## Decision distribution

| Decision | Count |
|----------|-------|
| `ALLOW` | 74 |
| `BLOCK` | 18 |
| `REQUIRE_APPROVAL` | 8 |

## Gate

- invoked: **30**
- skipped: **70**

## Advisor recommendation frequency

| Advisor | Count |
|---------|-------|
| `security-reviewer` | 20 |
| `loop-breaker` | 8 |

## Retrospective accuracy distribution

| Accuracy | Count |
|----------|-------|
| `not_applicable` | 70 |
| `accurate` | 21 |
| `false_alarm` | 8 |
| `missed_signal` | 1 |

## How to reproduce

```bash
uv run python demo/three_tier_validation.py
```
