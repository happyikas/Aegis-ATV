# Changelog

All notable changes to Aegis ATV. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
this project adheres to [Semantic Versioning](https://semver.org/).

## [0.5.17] — 2026-05-16  ·  Production advisor path plumbed to wiki (aid)

**v0.5.16 wired the wrong module.** That PR added wiki support to
`aegis.judge.action_advice_sllm` (a structured-output composer
with a `_build_prompt` of its own). But the **production firewall
hook** uses a different module: `aegis.judge.advisor`, with the
`Advisor` protocol (`DummyAdvisor` / `HaikuAdvisor`) and its own
`_build_user_message`. So while v0.5.16 was real Python code, it
was dormant on the production execution path.

v0.5.17 fixes that by plumbing `aid` + `knowledge_context`
through the **actual** advisor pipeline. This is the v0.5.13
lesson applied again: substrate without wiring is dormant.

### Plumbed sites

* **`src/aegis/judge/advisor.py`** — production module:
  - `Advisor` protocol: added `knowledge_context: str | None = None`
    kwarg.
  - `DummyAdvisor.advise()`: accepts the kwarg, silently ignores
    it (the heuristic is signal-driven, no wiki use).
  - `HaikuAdvisor.advise()`: accepts and forwards to
    `_build_user_message`.
  - `_build_user_message`: when `knowledge_context` is supplied,
    prepends a `=== KNOWLEDGE CONTEXT (agent background) ===`
    block as the FIRST section of the prompt — before
    temporal context, signals, and base verdict.
  - `compose_advice_sllm` dispatcher: added `aid` + `use_knowledge`
    kwargs. Selection rules same as v0.5.16:
      1. Explicit `use_knowledge=True` wins.
      2. Otherwise `AEGIS_ADVISOR_USE_KNOWLEDGE=1` opts in.
      3. Default off.
    When opted in AND `aid` is provided, fetches the wiki context
    via `aegis.knowledge.knowledge_context_for_advisor` and
    forwards to the advisor.

* **`tools/aegis_local_hook.py`** — `_compute_action_advice` now
  passes `aid=getattr(inp.header, "aid", None)` to
  `compose_advice_sllm`. This is the actual hot-path activation —
  the line that makes v0.5.16's design real.

* **`tools/aegis_cli.py`** — `aegis assess` gains a new `--aid`
  flag that scopes the assessment to one agent and plumbs that aid
  through to `assess_triple_axis`. Without `--aid` the command
  runs on the full window (preserving v0.5.16 behaviour).

### Tests

8 new tests in `tests/unit/test_advisor_aid_plumbing.py`:

* **Advisor protocol** (1) — `DummyAdvisor` accepts the new kwarg
  without raising.
* **`_build_user_message`** (2) — no block without context;
  block-at-top with context (positioned before PROPOSED CALL).
* **`compose_advice_sllm` dispatcher** (5) — default off, env
  opt-in, explicit kwarg overrides env, opt-in without aid falls
  back, opt-in with unknown aid falls back.

Full suite: **3422 passed**, 13 skipped (8 new). Ruff + mypy clean.

### Migration

Operators who set `AEGIS_ACTION_ADVICE_PROVIDER=haiku` (or
similar sLLM provider) and want their advisor to ground its
reasoning in the wiki should:

```bash
$ aegis knowledge build                # weekly: rebuild wiki
$ export AEGIS_ADVISOR_USE_KNOWLEDGE=1 # opt-in (single switch)
```

The hook's `_compute_action_advice` automatically plumbs aid
through every tool call from now on. No other code changes
required.

### What this completes

The closed loop established in v0.5.15 + v0.5.16 is now
**actually firing on the production hot path**:

```
Raw ContextMemory (audit chain)
      ↓ aegis knowledge build
Knowledge wiki (~/.aegis/knowledge/*.json)
      ↓ knowledge_context_for_advisor(aid)  [cached, hot-path safe]
Markdown block embedded in Haiku/sLLM prompt  [v0.5.17 plumbing]
      ↓ compose_advice_sllm
Wiki-grounded ActionAdvice  ←  this is what makes "is this agent
                                 unusual or normal" answerable
```

## [0.5.16] — 2026-05-16  ·  sLLM advisors consume the knowledge wiki

v0.5.15 shipped the ContextMemory knowledge layer — wiki-shaped
articles for agents / tools / patterns derived from raw events.
v0.5.16 wires the sLLM advisors (`TripleAxisAdvisor` and
`ActionAdvice`) to actually **consume** those entries. The
infrastructure shipped previously; v0.5.16 is the deliberate
consumption hookup.

### How it works

When the operator opts in (env or kwarg), the umbrella composer
fetches the agent's wiki block via
`knowledge_context_for_advisor(aid)` and embeds it in the sLLM
prompt before the firewall signals + heuristic baseline:

```
Instructions
=== Knowledge context (agent background) ===
# Agent foo
**Summary**: 1,247 calls, 1.8% BLOCK rate, $2.87 total cost.
**Tags**: `high-volume`
## Quick facts ...
## Activity profile ...
## Related ...
---
# Tool Bash
**Summary**: 562 invocations, 92% ALLOW ...
...
=== End knowledge context ===

Signals: (this window)
  decisions: 12 ALLOW / 0 BLOCK / 3 REQUIRE_APPROVAL
  ...

Heuristic baseline (refine the prose, keep scores):
  ...
```

The sLLM now has two layers of context: long-term agent profile
(from the wiki) **and** the current window (from the signals).
This lets the advisor distinguish "this agent is behaving
unusually" from "business as usual for this agent" — the central
shortcoming of context-less assessment.

### Opt-in (default off)

Selection rules (each composer):

1. Explicit `use_knowledge=True` kwarg wins.
2. Otherwise, `AEGIS_ADVISOR_USE_KNOWLEDGE=1` env opts in.
3. Default off — preserves v0.5.15 byte-identical behaviour.

Even when opted in, the wiki lookup short-circuits silently if
no wiki exists for the agent, so this is safe to enable
globally. The advisor falls back to the no-context prompt.

### New module

**`src/aegis/knowledge/advisor.py`** — the bridge:

* `advisor_knowledge_enabled()` — reads the env flag.
* `knowledge_context_for_advisor(aid, *, root=None, max_related=6, use_cache=True)`
  — returns prompt-ready markdown or `None`. mtime-keyed in-memory
  cache: subsequent calls with the same wiki return the same object
  identity, amortising the JSON parse cost across the firewall hot
  path. Never raises — broad exception suppression because the
  advisor sits on the firewall hot path.
* `clear_advisor_cache()` — for tests.

### Modified

* **`src/aegis/judge/triple_axis_advisor.py`**:
  - `_build_sllm_prompt(s, baseline, *, knowledge_context=None)` —
    splices the knowledge block between instructions and signals.
  - `assess_via_sllm(s, *, llm_call=None, knowledge_context=None)`.
  - `assess_triple_axis(records, *, aid=None, use_knowledge=None, ...)`
    — new opt-in kwargs.

* **`src/aegis/judge/action_advice_sllm.py`**:
  - `_build_prompt(baseline, *, current_tool="", knowledge_context=None)`
    — splices the block between constraints and the baseline.
  - `compose_advice_sllm(*, knowledge_context=None, **kwargs)`.
  - `compose_advice(*, aid=None, use_knowledge=None, **kwargs)` —
    new opt-in kwargs.

### Tests

21 new tests in `tests/unit/test_advisor_knowledge_wiring.py`:

* **Helper** (6) — empty aid, missing wiki, present wiki,
  unknown aid, env-flag parsing, cache identity preserved.
* **Prompt builders** (5) — both advisors: block absent without
  context, present with context, positioned correctly relative
  to signals / baseline.
* **Umbrella composers** (7) — both advisors: default-off path,
  env opt-in, kwarg overrides env, missing aid falls back,
  heuristic path is unaffected.
* **Hot-path safety** (3) — corrupted index → `None`, missing
  dir → `None`, helper never raises.

### Validation

- `uv run ruff check .` → All checks passed
- `uv run mypy src` → Success: no issues found in 229 source files
- `uv run pytest -q` → **3414 passed**, 13 skipped (21 new)

### Migration

Operators with `AEGIS_ACTION_ADVICE_PROVIDER=sllm` or
`AEGIS_TRIPLE_AXIS_PROVIDER=sllm` who want their sLLM to ground
its assessment in wiki context should:

1. Run `aegis knowledge build` after every burn-in window.
2. Set `AEGIS_ADVISOR_USE_KNOWLEDGE=1`.
3. Plumb `aid` through any explicit `compose_advice` /
   `assess_triple_axis` call sites (existing call sites without
   `aid` continue to work — they just don't get the wiki block).

Other operators see no behaviour change.

## [0.5.15] — 2026-05-16  ·  ContextMemory knowledge layer (LLM-wiki)

Until v0.5.14 ContextMemory was an analytics-shaped row-store
(one JSONL line per ATV decision) optimised for audit + replay
but **not** for downstream LLM consumption. An sLLM advisor that
wanted to give workflow advice had to scan thousands of raw rows
and reconstruct patterns itself. v0.5.15 introduces a derived
**LLM-wiki-shaped knowledge layer** on top of the raw store —
each entity (agent / tool / pattern) becomes a self-contained
wiki article the sLLM advisor can consume directly.

### Architecture — additive layer, not a rewrite

```
~/.aegis/context_memory.jsonl     ← raw events (unchanged, audit responsibility)
~/.aegis/knowledge/               ← derived wiki (NEW, sLLM-ready)
  index.json                      ← catalog of entries
  agent_foo.json                  ← per-entity articles
  tool_Bash.json
  pattern_loop_Bash.json
```

The raw store keeps its audit role; the wiki is a **derived
semantic view** rebuilt on demand via `aegis knowledge build`.
Same explicit-rebuild discipline as `aegis autonomy learn` —
no streaming projection, no implicit auto-update.

### LLM-wiki design choices (informed by Wikipedia / DBpedia /
linked-notes patterns)

| Wiki convention | ContextMemory mapping |
|---|---|
| Article entry | One entry per `agent` / `tool` / `pattern` |
| Lead summary | `summary: str` — 1-2 sentences, always shown first |
| Infobox | `InfoBox` dataclass — structured key-value table (LLMs parse these most reliably) |
| Sections | `Section[]` — ordered markdown headers + bodies |
| Cross-references | `related: tuple[str, ...]` — canonical entry_id URIs |
| Categories / tags | `tags: tuple[str, ...]` — `high-cost`, `unstable`, `frequent`, … |
| Stable URIs | `agent/foo`, `tool/Bash`, `pattern/loop:Bash` |
| Confidence + provenance | `n_observations`, `confidence`, `ts_first_ns`, `ts_last_ns` |

### Modules

* **`src/aegis/knowledge/schema.py`** — `KnowledgeEntry`,
  `InfoBox`, `Section`, `EntryKind` (StrEnum: agent/tool/pattern).
  Round-trip JSON serialisation with defensive `from_dict` —
  malformed payloads fall back to safe defaults, never raise.
* **`src/aegis/knowledge/builder.py`** — pure function
  `build_knowledge(records)` that aggregates per-entity stats
  and synthesises wiki entries. Confidence is
  `min(1.0, n / 50)` — same empirical threshold as the autonomy
  learner. Per-kind builders: AGENT (activity / stability / cost
  profile), TOOL (usage / patterns / top users), PATTERN
  (firing conditions / outcomes / autonomy bypass rate).
* **`src/aegis/knowledge/store.py`** — one JSON file per entry
  plus a catalog `index.json`. Atomic writes via tempfile +
  rename. Honours `AEGIS_KNOWLEDGE_DIR`. Per-entry files keep
  selective loads O(entries) rather than O(corpus).
* **`src/aegis/knowledge/render.py`** —
  `render_entry_markdown(entry)` for one entry,
  `render_advisor_context(entries, intro=...)` for the multi-
  entry prompt composition the sLLM advisor will consume. Pure
  CommonMark markdown — no HTML; LLMs parse markdown tables +
  sections more reliably than prose or JSON.
* **`src/aegis/knowledge/retrieve.py`** — three retrieval modes:
  `get_entry(id)` for by-ID lookup, `get_entries_for_agent(aid)`
  for the agent + cross-refs fanout (the default pre-pop for
  advisor prompts), `search_by_kind_or_tag` for catalog
  enumeration.

### CLI

* `aegis knowledge build [--since 30d]` — derive entries from
  ContextMemory window into `~/.aegis/knowledge/`.
* `aegis knowledge list [--kind agent|tool|pattern] [--tag X] [--limit N]`
  — enumerate the catalog with filter support.
* `aegis knowledge show <entry_id>` — render one entry as
  markdown to stdout (or `--out` file).
* `aegis knowledge advisor-context <aid> [--max-related N]` —
  compose the multi-entry prompt for an sLLM advisor scoped
  to one agent. **This is the integration hook** the next-PR
  TripleAxisAdvisor will call.

### Tests

30 new tests in `tests/unit/test_knowledge.py`, organised by
module: schema serialisation + defensive parsing (4), store
round-trip + atomic write (5), builder per-kind derivation (5),
renderer markdown structure (7), retrieval (7), full integration
build → save → load → render (1). Full suite: **3393 passed**,
13 skipped. Ruff + mypy clean (228 source files).

### Smoke (real local ContextMemory, 7,344 records over 30 days)

```
$ aegis knowledge build --since 30d --out /tmp/aegis-wiki-smoke
✓ knowledge wiki built at: /tmp/aegis-wiki-smoke
  records scanned:    7,344
  window:             30d
  agents:             19
  tools:              12
  patterns:           15
  total entries:      46
```

Sample `tool/Bash` entry rendered:

```markdown
# Tool Bash

**Summary**: 3,859 invocations in window: 47.27% ALLOW, 34.80%
REQUIRE_APPROVAL, 17.93% BLOCK. Average latency 114.4ms.

**Tags**: `high-volume`, `unstable`

## Quick facts
| Field | Value |
|-------|-------|
| n_calls | 3,859 |
| n_allow | 1,824 |
| n_require_approval | 1,343 |
...
```

### What this PR does NOT do

* No sLLM advisor changes yet — `TripleAxisAdvisor` /
  `ActionAdvice` still read raw records. The next PR (v0.5.16)
  wires them to call `get_entries_for_agent` +
  `render_advisor_context` instead.
* No SESSION / INCIDENT / WORKFLOW entry kinds — reserved for
  v0.6.
* No embedding-based semantic search — structural retrieval
  (by entry_id, by tag, by cross-ref) only in v0.5.15. Vector
  search is a v0.6 candidate.

## [0.5.14] — 2026-05-16  ·  Explicit-deny CLI + doctor postmortem

v0.5.12's Bayesian backbone wired the strongest negative-signal
pathway — `EXPLICIT_DENY` worth β += 10 — but never gave the
operator a way to *produce* the signal. v0.5.14 closes that loop
with two complementary additions.

### Item 1 — `aegis autonomy deny <trace_id>`

A new CLI subcommand that lets the operator mark a past auto-
approval as a mistake. One deny ≈ ten clean follow-ups on the
pattern's posterior.

* **`src/aegis/autonomy/denials.py`** — append-only JSONL log at
  `~/.aegis/autonomy/denials.jsonl` (override via
  `AEGIS_AUTONOMY_DENIALS`). `append_denial(trace_id, note=...)`
  for the writer, `load_denial_trace_ids()` for the learner.
  Defensive loader: missing file → empty set, malformed lines
  skipped silently, never raises. A corrupted deny log can never
  block training.
* **`classify_record`** gains a `denied_trace_ids` kwarg.
  A record whose trace_id is in the set is classified as
  `EXPLICIT_DENY` regardless of the timeline followup — this is
  the strongest negative signal we have.
* **`learn_with_diagnostics`** accepts `denied_trace_ids` and
  defaults to reading the on-disk log.
* **`aegis autonomy deny <trace_id> [--note "reason"]`** — CLI
  subcommand. Writes one line, echoes the persisted record,
  prompts the operator to re-run `aegis autonomy learn`.

Why a separate file rather than mutating ContextMemory? Because
ContextMemory is append-only by audit contract — rewriting past
records would invalidate the SHA3 hash chain that
`aegis verify-audit` relies on. The deny log is operator metadata
about past decisions and belongs in its own file.

### Item 2 — `aegis doctor` autonomy section

The standalone `aegis autonomy outliers` already existed for
postmortem walks; v0.5.14 surfaces the same signal inside the
periodic `aegis doctor` report so it lands in the same eyeballs
that scan cost / performance / security.

* **`src/aegis/context_memory/report.py`** — new `AutonomyStats`
  dataclass + `autonomy_stats()` pure function counting bypass /
  explore stamps and surfacing outliers. New `_autonomy_section`
  renders a markdown subsection between Security and Next Actions.
* When the window contains no autonomy activity, the section
  collapses to a one-line "_(autonomy disabled or no bypass
  events)_" so the report stays compact for default deployments.
* When outliers are present, the section renders a markdown
  table (trace_id, tool, bypass signature, follow-up BLOCK
  reason) plus an action prompt:
  ```
  의심스러운 trace_id 에 대해 `aegis autonomy deny <trace_id>`
  실행 → `aegis autonomy learn` 재실행으로 trust table 갱신.
  ```

### Tests

17 new tests in `tests/unit/test_autonomy_denials.py`:

* **Denial file** (6 tests) — create parent dirs, JSONL format,
  empty trace_id rejected, missing file → empty set, malformed
  lines skipped, round-trip via dataclass.
* **classify_record** (3 tests) — denied returns EXPLICIT_DENY,
  not denied returns CLEAN, denied overrides BLOCK_FOLLOWUP.
* **learn_with_diagnostics** (2 tests) — explicit deny drops
  pattern LCB, denials loaded from env-configured path.
* **Doctor autonomy section** (6 tests) — empty window, bypass
  + explore counts, outlier detection, report integration,
  silent rendering, outlier table rendering.

### Validation

- `uv run ruff check .` → All checks passed
- `uv run mypy src` → Success: no issues found in 222 source files
- `uv run pytest -q` → **3363 passed**, 13 skipped (17 new)
- Real local smoke: `aegis doctor --since 7d` renders the new
  autonomy section; `aegis autonomy deny` writes a clean JSONL line.

## [0.5.13] — 2026-05-16  ·  Autonomy runtime wiring (substrate activated)

v0.5.11 + v0.5.12 shipped the entire autonomy substrate — the trust
learner, runtime bypass shim, Bayesian backbone — but the
production paths were never wired to call it. Setting
`AEGIS_AUTONOMY_ENABLED=1` did nothing because no production code
path invoked `apply_autonomy_bypass`. v0.5.13 closes that gap with
two one-line wiring patches:

* **`src/aegis/api/evaluate.py`** — sidecar `_evaluate_impl` now
  calls `apply_autonomy_bypass(verdict, tool_name=..., reason=...)`
  immediately after `run_firewall`. The bypass-stamped verdict
  flows through step350 approval dispatch and step360 audit
  signing so the bypass is reproducible from the audit log.
* **`tools/aegis_local_hook.py`** — local hook calls the same shim
  AFTER the M12 cost-divergence escalation (so the bypass
  evaluates the *final* REQUIRE_APPROVAL signal, including
  M12 upgrades) and BEFORE `_atmu_finalize_intent` (so ATMU
  records the bypassed verdict). Wrapped in try/except — autonomy
  must never block a tool call on error.

Both wiring sites are byte-identical with v0.5.12 when the env
flag is off: the shim short-circuits at the top and returns the
verdict unchanged.

### Tests

`tests/integration/test_autonomy_wiring.py` — 5 new tests that
hit the real FastAPI `POST /evaluate` route through the
`aegis_app` fixture:

1. **Default off** — without `AEGIS_AUTONOMY_ENABLED`, no step331
   stamp ever appears in step_traces.
2. **Bypass engages** — with a populated trust table + the loop
   detector tripped on three identical Bash calls, the verdict
   downgrades to ALLOW and the step331 stamp lands.
3. **Never-trust filter holds** — a `dangerous_pattern` trust
   entry is refused at runtime even if a stale trust table lists
   it.
4. **ε-greedy exploration** — with `AEGIS_AUTONOMY_EPSILON=0.5`,
   at least one of 8 sessions stays REQUIRE_APPROVAL with the
   explore stamp set.
5. **Drift refusal** — `drifted=True` patterns are refused
   regardless of trust score.

Full suite: **3346 passed**, 13 skipped (5 new integration tests).
Ruff + mypy clean (221 source files).

### Migration

Operators upgrading from v0.5.11 / v0.5.12 who built a trust
table on disk see no behaviour change until they set the env
flag. Wiring is **strictly additive** — no existing code path
is altered when `AEGIS_AUTONOMY_ENABLED` is unset.

The "moment" the autonomy module starts mattering is when an
operator runs:

```
$ AEGIS_AUTONOMY_ENABLED=1 \
  AEGIS_AUTONOMY_TRUST_TABLE=~/.aegis/autonomy/trust_table.json \
  uv run aegis install --mode local   # or sidecar
```

…after which routine REQUIRE_APPROVAL patterns in the trust
table get auto-bypassed (5% are still forced to the human for
drift coverage via ε-greedy). Outliers surface through
`aegis autonomy outliers` and (next PR) `aegis doctor`.

## [0.5.12] — 2026-05-16  ·  Autonomy Bayesian backbone (anti-overfitting)

v0.5.11 shipped the human-in-the-loop minimiser as a point-estimate
classifier — `clean_rate * sample_weight`. That formulation exhibited
six classical ML-training side-effects which v0.5.12 closes
systematically. Each side-effect now has a named, tested defence.

### Side-effect → defence

| Side-effect | v0.5.11 exposure | v0.5.12 defence |
|---|---|---|
| **Overfitting** at small n | trust ≈ 0.70 for 5-of-5 clean | Beta(α, β) posterior + LCB(95%); 5-of-5 → LCB 0.30, well below 0.85 |
| **Self-confirming loop** (bypass kills feedback) | not addressed | ε-greedy forced exploration (default 5%, deterministic by BLAKE2b(atv_id)) |
| **Spurious correlation** | each pattern fitted independently | Empirical-Bayes hierarchical prior per tool (shrinkage toward tool baseline) |
| **Catastrophic staleness** | 90-day patterns still 100% | Exponential decay (30-day half-life); 90-day obs weighs ⅛ |
| **Distribution drift** | invisible | Jensen-Shannon between baseline (older 2/3) and recent (newer 1/3); drifted patterns dropped |
| **Reward sparsity** | binary clean / not-clean | Ternary shaping: CLEAN(+1) / BLOCK_FOLLOWUP(+3 to β) / EXPLICIT_DENY(+10 to β) |
| **Calibration miscarry** | not measured | 80/20 train/val split + ECE; learner refuses to persist when ECE > 0.10 |
| **Multiple comparisons** | flat min_samples=5 | Bonferroni-style ln(N) adjustment of min_samples |

### New modules

* **`src/aegis/autonomy/bayesian.py`** — `BetaPosterior` dataclass +
  pure-Python inverse-CDF (verified vs scipy to 1e-15) + LCB metric.
  `ToolBaseline` + `empirical_bayes_prior` for hierarchical
  regularisation. `adjusted_min_samples` for Bonferroni-style
  scaling. `make_posterior` is the single constructor.
* **`src/aegis/autonomy/reward.py`** — `RewardEvent` enum +
  `classify_record` event extractor + `RewardCounts` accumulator.
  Weights are constants (CLEAN=1, BLOCK=3, DENY=10) — not runtime
  tunable to prevent accidentally zeroing out the negative-signal
  pathway.
* **`src/aegis/autonomy/decay.py`** — `decay_weight()` + `should_drop`.
  Pure-math; uses `math.exp(-ln(2) Δd / τ)`. Half-life via
  `AEGIS_AUTONOMY_HALF_LIFE_DAYS` env (defensive parse).
* **`src/aegis/autonomy/drift.py`** — `kl_divergence_beta` (closed
  form via digamma) + `jensen_shannon_beta` + `is_drifted`.
  No scipy dependency.
* **`src/aegis/autonomy/calibration.py`** — `trace_split` (BLAKE2b
  mod 5; bucket 4 = val) + `compute_calibration` returning
  `CalibrationReport` with per-bucket ECE.

### Refactored

* **`src/aegis/autonomy/learner.py`** — new entry point
  `learn_with_diagnostics` returning a `LearnResult` (trust table
  + calibration + drop counts by gate + chosen thresholds).
  Backward-compatible `learn_trusted_patterns` is a thin wrapper
  with v0.5.11 admission policy preserved exactly.
* **`src/aegis/autonomy/runtime.py`** — ε-greedy in
  `apply_autonomy_bypass`. Deterministic via BLAKE2b(atv_id)
  mod 1000, threshold = ε × 1000. New env
  `AEGIS_AUTONOMY_EPSILON` (default 0.05, clamped to [0.0, 0.5]).
  Forced-exploration calls leave the verdict unchanged but stamp
  `STEP_TRACE_EXPLORE_KEY` so the operator sees exploration
  happening. Drift-flagged patterns are also refused at runtime
  (never auto-approve a `drifted=True` entry regardless of trust
  score).
* **`TrustedPattern`** gains optional posterior fields (`alpha`,
  `beta`, `posterior_mean`, `posterior_std`, `n_effective`,
  `n_explicit_deny`, `drift_score`, `drifted`, `credibility`,
  `prior_alpha`, `prior_beta`) — all default-safe so v0.5.11
  trust tables on disk continue to load and decide identically.

### CLI

* `aegis autonomy learn` accepts new flags:
  `--min-trust` (LCB threshold, default 0.85),
  `--credibility` (default 0.95),
  `--half-life-days` (default 30),
  `--ece-threshold` (default 0.10),
  `--force` (persist despite calibration failure, audit hazard).
  The output prints sample counts, drop counts per gate, the
  Bonferroni-adjusted `min_samples`, and the calibration ECE.
  A failed calibration check **refuses** to persist the new
  table; the previous one stays authoritative.
* `aegis autonomy show -v` prints the Bayesian posterior
  (α, β, LCB, drift) per pattern. Also surfaces the current
  ε-greedy rate from the env.

### Tests

52 new tests in `tests/unit/test_autonomy_bayesian.py`, organised
by side-effect (`TestOverfittingResistance`, `TestExplorationBreaksLoop`,
`TestDecayReducesStaleWeight`, `TestDriftDetectionFlagsShift`,
`TestRewardShapingMagnitude`, `TestCalibrationRejectsMiscalibrated`,
`TestBonferroniRaisesThreshold`, `TestEmpiricalBayesPrior`,
`TestLearnWithDiagnostics`, `TestApplyAutonomyBypassExploration`).

All 31 v0.5.11 autonomy tests continue to pass — backward
compatibility is preserved.

Full suite: 3341 passed, 13 skipped.

### Smoke (real local ContextMemory, 6,536 records over 30 days)

```
records scanned:    6,536
patterns considered:4
patterns learned:   2
  dropped (never-trust):    0
  dropped (low samples):    0
  dropped (low trust LCB):  2
  dropped (drifted):        0
min_samples (after Bonferroni): 7
min_trust (LCB threshold):      0.85
credibility:                    0.95
half-life:                      30.0 day(s)
calibration ECE:                0.0051 (pass)

✓ autonomy trust table updated

tool         signature                  n      α      β   LCB  drift
----------------------------------------------------------------------
Bash         loop:Bash                170  171.1    0.2  0.99  0.003
Bash         cost-divergence           68   70.7    0.2  0.99  0.003
```

Note: two patterns from v0.5.11's table (low-sample variants) now
drop under the LCB threshold — the v0.5.12 contract specifically
filters those out. The remaining two are well above the threshold
with tight posteriors (β ≈ 0.2 reflects empirical-Bayes shrinkage
toward the Bash tool's clean baseline).

## [0.5.11] — 2026-05-16  ·  Autonomy — human-in-the-loop minimiser

Closes the autonomous-agent UX gap surfaced in the user audit:
every REQUIRE_APPROVAL today interrupts the agent and asks the
operator. For routine patterns the operator has seen + cleared
many times, this becomes pure friction. v0.5.11 ships a burn-in
trust learner + runtime bypass + outlier postmortem that lets
the operator stay in the loop **only for non-routine events**,
while every bypassed event is still permanently traced for audit.

### Added

* **`src/aegis/autonomy/`** package — learner + outlier detector
  + runtime shim.

  * `learner.py` — `learn_trusted_patterns()` mines the burn-in
    window of ContextMemory and produces a trust table
    `{(tool_name, reason_signature) → TrustedPattern}`. Three
    safety gates:
      1. `n_seen ≥ min_samples` (default 5).
      2. `clean_rate ≥ min_clean_rate` (default 0.95) — pattern
         is dropped if subsequent BLOCKs from the same aid
         followed too often.
      3. Never-trust filter — patterns whose reason contains
         `dangerous pattern`, `rule:git_destructive`,
         `rule:cloud_destructive`, `sensitive path`, or
         `cumulative_dollars` are NEVER trusted even with high
         sample count.
    `evaluate_autonomy_request()` is the runtime query — returns
    `AutonomyVerdict(auto_approve, matched_pattern, confidence,
    reason, outlier_signals)`.

  * `outliers.py` — `detect_outliers()` walks ContextMemory for
    records carrying the `step331: auto-approved` step_traces
    stamp and surfaces any that were followed by a BLOCK within
    a `block_lookahead` (default 10) window from the same aid.
    This is the postmortem signal — false-positive trust
    patterns surface within a single session.

  * `runtime.py` — `apply_autonomy_bypass(verdict, *,
    tool_name, reason, trust_table=None)` is the Verdict shim.
    When `AEGIS_AUTONOMY_ENABLED=1` and the verdict is
    REQUIRE_APPROVAL and the trust table contains a high-trust
    match (default ≥ 0.85), the verdict is downgraded to ALLOW
    with a permanent stamp in `step_traces[aegis.autonomy
    .step331.run]`. Otherwise the verdict is returned unchanged.
    Trust table persists at `~/.aegis/autonomy/trust_table.json`.

* **CLI** — `aegis autonomy {learn, show, outliers}`:
  * `learn --since DURATION` — re-mine + save trust table.
  * `show` — render current trust table + enabled status.
  * `outliers --since DURATION` — postmortem walk.

### Safety properties

* **Off by default.** `AEGIS_AUTONOMY_ENABLED` unset →
  `apply_autonomy_bypass` is a no-op; existing deployments see
  byte-identical behavior. Operators opt in explicitly.
* **Never-trust filter enforced twice.** Once at learning time
  (the pattern never enters the table) and once at runtime
  (even if a malicious / stale table contains it, the bypass is
  refused).
* **Every bypass is traceable.** The `step331` step_trace stamp
  carries `tool=X signature=Y trust=0.92 (was REQUIRE_APPROVAL:
  '...')` so audit replay re-derives the bypass decision.
* **Outlier feedback loop closes within a session.** `aegis
  autonomy outliers` (and the next-PR `aegis doctor` integration)
  walks the recent window and surfaces any auto-approval that
  was followed by a BLOCK. Trust patterns that turn out poorly
  show up immediately, not weeks later.

### Smoke (real local data — 5,960 ContextMemory records, 30d)

```
$ aegis autonomy learn --since 30d
✓ autonomy trust table updated: ~/.aegis/autonomy/trust_table.json
  records scanned:    5,960
  patterns learned:   2
  min_samples:        5
  min_clean_rate:     0.95

$ aegis autonomy show
Autonomy bypass — 🟡 disabled
  tool           signature                 seen  clean  trust
  Bash           loop:Bash                  155   100%   1.00
  Bash           cost-divergence             62   100%   1.00
```

Engaging the bypass would auto-approve 217 of the operator's
REQUIRE_APPROVAL events in a 30-day window — a meaningful
reduction in friction without losing audit visibility.

### Tests

* `tests/unit/test_autonomy.py` — 31 cases covering reason
  signature canonicalisation, learner thresholds, never-trust
  filter at both learning and runtime, trust table round-trip,
  Verdict bypass (off / verdict-class filter / downgrade /
  step_traces preservation), outlier detection (empty, clean,
  flagged, lookahead bounds). Full sweep: 3258 → 3289.

### Wiring (next PR)

This PR ships the autonomy module + CLI but **does not** wire
the bypass into `tools/aegis_local_hook.py` or `src/aegis/api/
evaluate.py` yet. The hook-side wiring is a small follow-up
that needs broader integration testing across all hook
surfaces. The autonomy module is fully usable from Python today;
the next PR will activate the runtime bypass via the env flag.

## [0.5.10] — 2026-05-16  ·  TripleAxisAdvisor — sLLM scene interpretation across 3 axes

Closes the architectural gap the user audit surfaced: sLLM was
producing a 3-class verdict (ALLOW/BLOCK/REQUIRE_APPROVAL), not
**interpreting the run-time context** into the three axes the
patent's advisor pipeline targets. v0.5.9 added sLLM prose
refinement; v0.5.10 adds a structured per-axis assessment that
matches the patent's "sLLM understands the scene" intent.

### Added

* **`src/aegis/judge/triple_axis_advisor.py`** — new module
  exposing `assess_triple_axis()` (ContextMemory window → 3-axis
  advice) + dataclasses + render.
* **`aegis assess`** CLI — triple-axis scene interpretation
  command. Flags: `--since DURATION`, `--sllm`, `--json`,
  `--context-memory PATH`.

### The three axes

| Axis | Signals | Score formula (heuristic) |
|---|---|---|
| 💰 **token_efficiency** | repeat_call_ratio, top_cost_tool_share, avg_tokens_per_call | 1.0 − 0.5×repeat − 0.3×top_tool_share − 0.2×token_size |
| 🧊 **cache_performance** | est_cache_hit_rate (inferred from repeat patterns), prefix instability, redundant Reads | est_hit_rate − 0.5×prefix_unstable |
| 🛡️ **stability** | block_rate, approval_rate, loop_detected, dangerous_pattern, sensitive_path | 1.0 − block_rate − 0.3×appr_rate − loop − danger |

Each axis carries: `score ∈ [0,1]`, `severity ∈ {ok, warn, alert}`,
one-sentence `interpretation`, and (when applicable) `next_action`.

### sLLM brain (`--sllm` flag or `AEGIS_TRIPLE_AXIS_PROVIDER=sllm`)

The sLLM is asked to **refine the per-axis prose** (interpretation +
next_action) — it cannot change scores or severity. Same defensive-
fallback contract as the v0.5.9 ActionAdvice sLLM brain:

* No LLM available → heuristic baseline
* LLM raises / returns None / unparseable → heuristic baseline
* LLM tries to inject `decision`/`score` keys → **ignored**
  (prompt-injection defense)

### Smoke against local ContextMemory (4,658 records, 24h window)

```
Triple-axis assessment (heuristic)
  records: 4,658    window: 1d
  overall priority: 🧊 Cache performance

  💰 Token efficiency   score 0.50  🟡 warn
    100% of calls in 3+ repeat pattern
  🧊 Cache performance  score 0.00  🔴 alert
    est cache hit rate ~0%; 1307 redundant Read calls;
    230 prompt-prefix instability events
  🛡️  Stability         score 0.69  🟡 warn
    10.0% BLOCK rate; 230 step336 loop events;
    208 dangerous-pattern hits
```

### Tests

* `tests/unit/test_triple_axis_advisor.py` — 28 cases covering
  signal extraction (counts, repeat-ratio, redundant Reads, top-
  cost tool), heuristic per-axis assessment (empty window, high-
  repeat penalty, loop drop, BLOCK drop), overall priority +
  summary, sLLM refinement (success, fallback paths, score-
  preservation, prompt-injection defense), umbrella selection
  (default heuristic, env opt-in, kwarg override), rendering, and
  data-shape invariants.
* Total sweep: 3230 → 3258 (+28).

### Production-gap status — all four closed

| Gap | Status |
|---|---|
| ContextMemory rotation | ✅ v0.5.7 |
| ATMU auto WAL replay | ✅ v0.5.8 |
| ActionAdvice sLLM brain (prose) | ✅ v0.5.9 |
| **sLLM scene interpretation (3-axis)** | ✅ this release |

## [0.5.9] — 2026-05-16  ·  ActionAdvice sLLM brain (PR-ζ-head, production gap #3)

Closes the third and final production gap from the v0.5.6 self-
audit. The `ActionAdvice` dataclass has carried the full sLLM-
ready schema since v2.5; up to v0.5.8 the *brain* generating
those advices was always `compose_advice_heuristic` — a small
template-based composer the module docstring explicitly marked
as a placeholder ("PR-ζ-head will swap the body for an actual
sLLM call"). This release ships that swap.

### Added

* **`src/aegis/judge/action_advice_sllm.py`** — sLLM composer +
  umbrella `compose_advice()`.
* **`compose_advice_sllm(...)`** — runs the heuristic composer
  to get a baseline, then asks a configured sLLM to refine the
  prose fields (`reason`, `next_action_hint`,
  `alternative_tool`). Verdict-class fields stay heuristic.
* **`compose_advice(prefer_sllm=None, ...)`** — umbrella entry
  point. Picks sLLM vs heuristic based on `prefer_sllm` kwarg
  → `AEGIS_ACTION_ADVICE_PROVIDER=sllm` env var → default
  (heuristic, preserves v0.5.8 byte-for-byte).
* **Default LLM-call adapter** — dispatches on
  `AEGIS_JUDGE_PROVIDER`:
    * `haiku` → Anthropic Haiku via the existing client
    * `local-phi` / `phi` → llama-cpp `Llama` via
      `_load_real_phi` (lazy GGUF load, cached)
    * `hybrid` → Haiku when API key set, else Phi
    * `dummy` / unset → `None` (fall back to heuristic)

### Design decisions

* **Heuristic-first, sLLM-enhanced.** The decision class
  (ALLOW/BLOCK/REQUIRE_APPROVAL/DEFER), confidence,
  cited_anomalies, cited_turns_rel, and recommended_advisors all
  stay heuristic. Those fields have hard contracts + extensive CI
  coverage; we don't want the LLM rewriting them. The sLLM only
  touches *prose* fields (`reason`, `next_action_hint`,
  `alternative_tool`). This also defends against
  prompt-injection: an attacker who somehow biases the LLM
  toward "ALLOW" can't actually flip the verdict — the parser
  ignores any `decision` key in the response.

* **No new Judge interface.** The sLLM call piggybacks on the
  LLM client surface that `LocalPhiJudge` / `HaikuJudge` already
  expose, bypassing the verdict-shaped `Judge.evaluate()`
  contract. Keeps the public API surface stable.

* **Opt-in.** `AEGIS_ACTION_ADVICE_PROVIDER` defaults to
  heuristic so v0.5.8 audit replay byte-matches. Operators flip
  the env var when they're ready.

* **Defensive fallback at every layer:**
    * No LLM available → heuristic
    * LLM raises → heuristic (advisor sits on firewall hot path)
    * Response unparseable → heuristic
    * Response is non-object JSON → heuristic
    * Response identical to baseline → heuristic
    * String fields capped at 400 chars (defense against runaway
      output)

### Response parsing — robust to LLM quirks

* Plain JSON: `{"reason": "..."}`
* Markdown fence: ` ```json\\n{...}\\n``` `
* Markdown fence (no lang tag): ` ```\\n{...}\\n``` `
* Leading prose + JSON
* Nested braces (balanced-brace walker)
* All accepted by `_extract_json_blob()`.

### Tests

* `tests/unit/test_action_advice_sllm.py` — 27 cases:
  prompt builder shape, JSON extraction (plain, fenced, prose-
  wrapped, nested, empty), response parsing (partial fields,
  null-string handling, malformed JSON, non-object, None,
  no-op detection, runaway-string truncation), end-to-end
  composer (LLM success, fallback on None/raise, decision-class
  immutable against prompt injection), default LLM-call adapter
  with dummy provider, umbrella (default heuristic, env opt-in,
  kwarg overrides env, unknown env), audit-field preservation,
  produced_at_ns timestamping.
* Full sweep: 3203 → 3230 (+27).

### Production-gap status — all closed

| Gap (from v0.5.6 audit) | Status |
|---|---|
| ContextMemory rotation | ✅ v0.5.7 |
| ATMU auto WAL replay on startup | ✅ v0.5.8 |
| **ActionAdvice sLLM brain (PR-ζ-head)** | ✅ this release |

## [0.5.8] — 2026-05-16  ·  ATMU auto WAL replay on startup (production gap #2)

Closes the second of three production gaps from the v0.5.6
self-audit. Before this release, an Aegis process that crashed
mid-flight left rows stuck in non-terminal ATMU states
(TENTATIVE / PREPARED) with no automatic remediation — operators
had to discover and clean them up manually via `aegis rollback`
or direct SQLite surgery.

### Added

* **`src/aegis/atmu/recovery.py`** — `find_orphans()` +
  `recover_orphans()` + `render_sweep_summary()`. Sweeps
  non-terminal rows older than `max_age_hours` (default 24 h),
  transitioning them to ABORTED with a structured reason
  ("orphaned at startup — auto-recovered (ATMU §5A)"). Idempotent,
  per-row failure isolation, supports `dry_run=True` for preview.

* **`aegis atmu recover`** CLI — on-demand sweep with the same
  semantics. Flags: `--dry-run`, `--max-age-hours N`,
  `--db PATH`.

* **Automatic recovery on startup** —
    * Sidecar (`src/aegis/main.py`): runs once during FastAPI
      lifespan after `IntentLog` is opened. Prints a one-line
      stderr summary when orphans are swept; silent on no-op.
    * Local-mode hook (`tools/aegis_local_hook.py`): runs on
      first lazy `_get_intent_log()` init per process. Same
      silent-on-no-op contract; verbose mode prints the summary.

### Policy

* **Age threshold** — rows younger than `max_age_hours` are
  presumed live (a slow tool call still in flight). Setting it
  too low could ABORT a real in-flight transaction; setting it
  too high lets crashed rows linger. 24 h is the chosen default;
  operators override via `--max-age-hours` on the CLI.
* **From-state policy** — TENTATIVE *and* PREPARED rows are
  swept. Terminal states (COMMITTED, ABORTED, ROLLED_BACK,
  COMPENSATED, QUARANTINED) are never touched by definition.
* **Target state** — always ABORTED. We never auto-promote
  orphans to COMMITTED because we have no evidence the side
  effect happened. The compensation plan (if any) stays attached
  so `aegis rollback <trace>` still works against the orphan.

### Safety properties

* **Idempotent** — second sweep produces no spurious work
* **Read-only when `dry_run=True`** — fits the operator-preview
  pattern used by `memory rotate --dry-run` and `memory claude-md`
* **Per-row failure isolation** — a SQLite error or
  `InvalidTransition` on one row doesn't abort the sweep; the
  failing row lands in `result.failed` and the rest of the sweep
  continues
* **Startup never blocks** — every `Exception` is swallowed in
  the sidecar / local-hook auto-recovery wrappers; if the sweep
  itself raises, the process still comes up

### New public API in `aegis.atmu`

* `OrphanRecord`, `OrphanSweepResult` dataclasses
* `find_orphans(intent_log, *, max_age_hours, now_ns=None) -> (eligible, too_young)` — pure (no mutation)
* `recover_orphans(intent_log, *, max_age_hours, dry_run=False, ...) -> OrphanSweepResult`
* `render_sweep_summary(result) -> str`
* `NON_TERMINAL_STATES`, `DEFAULT_MAX_AGE_HOURS` constants

### Tests

* `tests/unit/test_atmu_recovery.py` — 18 cases: NON_TERMINAL
  sanity, find_orphans correctness (empty, split-by-age, zero
  threshold, terminal-state filter, no-mutation), recover_orphans
  (dry-run, transitions to ABORTED, idempotent, age threshold,
  PREPARED state, custom reason, per-row failure isolation),
  render_sweep_summary (dry-run wording, long-list truncation,
  empty summary), result shape.
* `tests/unit/test_cli_restructure_v05.py` — 3 CLI cases:
  dry-run reports without mutating, execute actually transitions,
  `--max-age-hours 0` sweeps all.
* Total: 3182 → 3203 (+21).

### Production-gap status

| Gap (from v0.5.6 audit) | Status |
|---|---|
| ContextMemory rotation | ✅ v0.5.7 |
| **ATMU auto WAL replay on startup** | ✅ shipped (this release) |
| ActionAdvice sLLM brain (PR-ζ-head) | open (v0.5.9 target) |

## [0.5.7] — 2026-05-16  ·  ContextMemory rotation (production gap #1)

Closes one of the three production gaps identified in the v0.5.6
self-audit: ContextMemory was unbounded — `~/.aegis/context_memory
.jsonl` grew forever, eventually filling the disk and slowing
every `read_window()` linearly.

### Added

* **`src/aegis/context_memory/rotation.py`** — size-triggered
  rotation with gzip archives and retention prune. Modeled after
  `src/aegis/audit/rotation.py` but simpler: ContextMemory is
  analytics-only, so no cross-file SHA3 chain continuity to
  preserve — the oldest archive can be dropped without ceremony.
* **`aegis memory rotate`** CLI — force-rotate on demand. `--dry-run`
  reports what would happen without changing files;
  `--context-memory PATH` overrides the default location.
* **`include_rotated=True`** parameter on `iter_records()`,
  `read_all()`, `read_window()` — walk archived rotations in
  chronological order before the active file, so historical
  windows reconstruct across rotation boundaries.

### Storage layout

```
~/.aegis/
├── context_memory.jsonl           (active, plain text)
├── context_memory.jsonl.1.gz      (most recent archive)
├── context_memory.jsonl.2.gz
└── context_memory.jsonl.{K}.gz    (oldest retained)
```

### Trigger

The writer calls `rotate_if_needed()` after every successful
`append()`. When the active file crosses
`AEGIS_CONTEXT_MEMORY_MAX_BYTES` (default 50 MB), the next append
triggers a rotation: gzip the active to `.1.gz`, shift older
archives up one slot, drop slot `K` (oldest beyond retention).
Rotation latency for a 50 MB file is ~0.5–1 s; concurrent appends
during rotation may lose a few records but never crash.

### Configuration

| env var | default | meaning |
|---|---|---|
| `AEGIS_CONTEXT_MEMORY_MAX_BYTES` | `52428800` (50 MB) | size trigger; `0` = off |
| `AEGIS_CONTEXT_MEMORY_MAX_ROTATIONS` | `5` | keep K archives; `0` = no rotation at all |
| `AEGIS_CONTEXT_MEMORY_ROTATION_DISABLED` | (unset) | `1` = unconditionally suppress |

### Defensive contract

Mirror of `writer.append()`'s "analytics never blocks verdict
path" rule. Every step in `rotate()` suppresses `OSError` and
returns gracefully on failure — a permission error or disk-full
condition during rotation leaves the active file intact and the
next append proceeds normally. Operators notice via `aegis memory
show` reporting a stagnant size, not via a Claude Code session
breaking.

### Tests

* `tests/unit/test_context_memory_rotation.py` — 19 new cases:
  trigger gating (size, max-rotations=0, disable env), engine
  correctness (slot-1 creation, slot shift, oldest drop,
  no-op-when-missing, no-op-when-disabled), writer integration
  (opportunistic rotation), reader integration (`include_rotated`
  walks chronologically + handles malformed lines in archives),
  helper functions (slot_path, open_rotation_text).
* `tests/unit/test_cli_restructure_v05.py` — 3 new CLI cases:
  dry-run reports without mutating, execute creates slot 1 archive,
  disabled-via-env returns 1.
* Total: 3160 → 3182 (+22).

### Production gap status

| Gap (from v0.5.6 audit) | Status |
|---|---|
| **ContextMemory rotation** | ✅ shipped (this release) |
| ATMU auto WAL replay on startup | open |
| ActionAdvice sLLM brain (PR-ζ-head) | open |

## [0.5.6] — 2026-05-16  ·  `aegis memory diff` — applied-proposal history

Closes the proposal lifecycle loop. v0.5.2 *generated* proposals,
v0.5.4 *applied* them, v0.5.6 *audits* what's been applied so far.

### Added

* **`aegis memory diff`** — walks the project CLAUDE.md, finds
  every ``<!-- aegis-managed-proposal: ... -->`` marker stamped by
  ``--apply``, and prints a git-log-like list with kind, pattern,
  confidence, section, line number, and body.
* **`--json`** — emit a structured JSON payload (one object per
  applied proposal) for piping into `jq` or CI artifacts.
* **`--claude-md PATH`** — file path override; same precedence as
  ``aegis memory claude-md``.

### New public API in `aegis.context_memory.claude_md_proposals`

* `AppliedProposal(kind, pattern, confidence, section, body,
  line_number)` — dataclass for one recovered splice.
* `extract_applied_proposals(md_text) -> list[AppliedProposal]` —
  parses CLAUDE.md text and returns markers in file order. Tracks
  the surrounding heading; falls back to `"(top-level)"` for
  markers before any heading. Body capture stops at the first
  blank line (matching the spacing `--apply` emits), so the
  reverse-lookup doesn't pick up pre-existing prose that happens
  to sit below.
* `render_diff_text(applied, *, md_path=None) -> str` — plain-text
  rendering for the CLI default output.

### End-to-end roundtrip

`apply_proposal()` writes a marker → `extract_applied_proposals()`
reads it back. Kind, pattern, confidence, section, and body all
round-trip intact. Test
`test_extract_applied_round_trip_with_apply_proposal` locks this
invariant.

### Tests

* `tests/unit/test_claude_md_proposals.py` — 8 new module tests:
  metadata recovery, multi-marker / multi-section, no-marker, marker
  before any heading (top-level fallback), quote-style flexibility
  (single + double), apply→extract round-trip, empty render hint,
  populated render contract.
* `tests/unit/test_cli_restructure_v05.py` — 4 new CLI tests:
  basic happy path, `--json` emits parseable payload, missing
  CLAUDE.md returns 1, `--claude-md PATH` override.
* Total: 3148 → 3160 (+12).

## [0.5.5] — 2026-05-16  ·  Two more `memory claude-md` miners

Grows the miner count from 4 → 6. New surfaces cover *cost* and
*advisor-pipeline* signals that the v0.5.2 miners didn't touch.

### Added

* **high-cost-tool miner** — groups ALLOW records by `tool_name`,
  sums their `cost_usd`, and surfaces tools whose cumulative cost
  in the window exceeds the threshold (default $0.01, tunable via
  `--min-tool-cost-usd`). Proposes a CLAUDE.md "before calling
  this tool, check if a recent result is reusable" note under
  `## Cost Discipline`. Confidence scales with call count:
  ≥10 calls = high, fewer = medium.

* **advisor-recommendation miner** — rolls up the
  `recommended_advisors` tuple across the window. Each advisor name
  that appears `>= min_count` times produces a proposal. Section is
  keyword-routed: `cost-*` → Cost Discipline, `security-*` →
  Security Notes, `cache-*` → Cost Discipline, `performance-*` →
  Workflow Discipline, anything else → Project Guardrails.

* **`--min-tool-cost-usd USD`** CLI flag — sets the $-threshold for
  the high-cost-tool miner. Default `0.01`.

### Changed

* `propose_edits()` signature — adds `min_tool_cost_usd` keyword
  argument (default 0.01). Backward-compatible default; the older
  4-miner signature still works.

### Verified

* Real-world smoke on local 7d window (2,508 records) — the
  advisor-recommendation miner immediately surfaced
  `security-reviewer` (289×), `loop-breaker` (255×), and
  `permission-escalator` (17×) as actionable proposals. The
  high-cost-tool miner correctly skipped on the local store (all
  cost_usd=0 in dummy mode).

### Tests

* `tests/unit/test_claude_md_proposals.py` — 11 new cases covering
  both miners: threshold gating (count, $), decision-type filter,
  confidence scaling, multi-advisor records, keyword section
  routing, empty-string defensiveness, custom threshold override.
* Total: 23 → 34 module tests (3137 → 3148 full sweep).

## [0.5.4] — 2026-05-15  ·  `memory claude-md --apply N` — auto-splice proposals

v0.5.2 generated proposals; v0.5.4 closes the loop by **applying
them in one command**. The natural follow-up flow becomes:

```bash
aegis memory claude-md              # see proposals
aegis memory claude-md --apply 1    # splice proposal #1 (writes .bak)
git diff CLAUDE.md                  # review
git commit -m "docs: aegis-managed proposal #1"
```

### Added

* **`--apply N`** — splice the Nth (1-indexed) proposal from the
  current window into the project CLAUDE.md.
  * If the proposal's `suggested_section` heading exists anywhere in
    the file (case-insensitive substring match, **bidirectional** —
    "Security" matches "Security Notes" and vice versa), the new
    text lands immediately after that heading.
  * Otherwise, a fresh `## <section>` block is appended at EOF.
  * Stamped with an `<!-- aegis-managed-proposal: ... -->` HTML
    marker carrying `kind`, `pattern`, `confidence` for downstream
    traceability (markdown renderers ignore HTML comments — invisible
    in rendered docs).
* **`--no-bak`** — skip the `<CLAUDE.md>.bak` backup. By default,
  the splicer writes the backup before mutating the file.

### New public API

* `apply_proposal(proposal, md_path, *, write_backup=True) -> ApplyResult`
  in `aegis.context_memory.claude_md_proposals`.
* `ApplyResult` dataclass: `md_path`, `bak_path`, `inserted_under`
  (heading text or `"(appended new section)"`), `new_lines_added`.

### Changed

* The `aegis memory claude-md` output footer now reads
  "_Auto-apply: `aegis memory claude-md --apply N` splices …_"
  (previously: "_Apply manually for now…_").

### Edge cases handled

* Out-of-range N (e.g. `--apply 99` when there are 3 proposals)
  prints a clear error + exits 1.
* No proposals in window (--since too small, --min-count too high)
  + `--apply` → exits 1 with a hint to widen the window.
* Trailing newline of the original file is preserved.
* `.bak` is written **before** the modification so an interrupted
  apply leaves both copies recoverable.

### Tests

* `tests/unit/test_claude_md_proposals.py` — 6 new cases for
  `apply_proposal`: insert under matching heading, append new
  section when no match, case-insensitive bidirectional match,
  `--no-bak`, trailing-newline preservation, marker metadata.
* `tests/unit/test_cli_restructure_v05.py` — 4 new CLI cases for
  `--apply`: happy path with .bak, --no-bak path, out-of-range,
  empty-window.

### Total

* +10 new tests (3127 → 3137 passed, 13 skipped)
* +1 source file modified (`tools/aegis_cli.py`)
* `src/aegis/context_memory/claude_md_proposals.py` — +`ApplyResult`,
  +`apply_proposal`, +`_find_section_insertion_point`,
  +`_format_apply_marker`

## [0.5.3] — 2026-05-15  ·  Chore — Node 24 workflow opt-in + docs vocab refresh

No code changes. Two infrastructure clean-ups so the v0.5 surface
stays correct as the world around it moves.

### Changed

* **CI / release workflows** — every workflow gets the
  `FORCE_JAVASCRIPT_ACTIONS_TO_NODE24: "true"` env flag. GitHub
  deprecated Node 20 for JavaScript actions effective 2026-06-02;
  this opt-in keeps Aegis builds green past that deadline without
  having to chase individual action-version bumps. Files touched:
    * `.github/workflows/ci.yml`
    * `.github/workflows/release-pypi.yml`
    * `.github/workflows/release-docker.yml`
    * `.github/workflows/quickstart-smoke.yml`
    * `.github/workflows/openclaw-plugin.yml`
  Once Node 24 becomes the runner default the flag is a no-op and
  can be deleted.

* **README + user-facing docs** — the named-features table and the
  `docs/USER_GUIDE.ko.md` Quick Reference section now use the v0.5+
  canonical vocab (`live` / `coach` / `guard` / `memory` / `doctor`).
  Older command names continue to work as aliases and the deeper
  technical guides (M13, MACMINI, STEP340, MANUAL_v2.5_advisor)
  carry a one-line note pointing at the canonical form. Files
  touched:
    * `README.md` — three-features table → five-features table
      including ATV Memory + ATV Guard; appended a one-line vocab
      note
    * `docs/USER_GUIDE.ko.md` — coach + live sections, troubleshoot
      hint
    * `docs/M13_TRAINING.md` — vocab note in the front-matter
    * `docs/MANUAL_MACMINI.md` — H1 → "Aegis ATV" (was MVP); vocab note
    * `docs/MANUAL_v2.5_advisor.md` — vocab note in the front-matter
    * `docs/STEP340_RAG.md` — vocab note in the front-matter

### Unchanged

* All command behavior, parsers, miners, tests. v0.5.2 functionality
  is identical — the version bump is for the documentation contract
  alone so users running `aegis --version` see the latest doc set.

## [0.5.2] — 2026-05-15  ·  `memory claude-md` — real CLAUDE.md proposal generator

Closes the last gap in the v0.5 "one-stop solution" vision: an
agent's BLOCK + REQUIRE_APPROVAL events become **concrete CLAUDE.md
edit proposals**, not just statistics.

### What's new

`aegis memory claude-md` now runs four miners over the recent
ContextMemory window and prints a markdown report:

* **dangerous-pattern miner** — groups BLOCK events with reason
  ``dangerous pattern: <regex>``. Maps the regex to a human-
  readable rule via an internal lookup table (`rm -rf`, raw drop-
  table, `kubectl delete`, `terraform destroy`, force-push on main).
  Unknown patterns get a generic medium-confidence proposal.
* **loop-detector miner** — groups REQUIRE_APPROVAL events with
  reason ``same X call repeated N times``. Suggests reflective-
  stop language for the looping tool.
* **sensitive-path miner** — groups ``sensitive path requires
  approval: <p>`` reasons by path. Surfaces "request pre-approval
  before touching <p>".
* **rule-violation miner** — groups ``rule:<name>`` reasons by
  rule name. Surfaces frequency so operators know which custom
  guardrails CLAUDE.md doesn't yet explain.

Each proposal includes: kind, pattern, count, confidence, suggested
section heading, suggested text (copy-paste ready), rationale, and
3 sample trace IDs for cross-reference.

### Flags

```
aegis memory claude-md
  --since DURATION    window (default 7d). 24h, 30d, 1h, …
  --min-count N       threshold for surfacing a pattern (default 3)
  --out FILE          write the markdown report to FILE (default stdout)
  --context-memory P  ContextMemory path override
```

### Dedup against existing CLAUDE.md

If your CLAUDE.md already mentions the trigger (pattern / tool name
/ path), that proposal is filtered out. Anchoring on the actual
pattern string keeps false-positives low; operators won't see "add
rm -rf rule" if they've already documented `rm -rf`.

### Fallback behavior

When ContextMemory is empty (fresh install), falls back to the
v0.5.1 locator-only output: print CLAUDE.md path, size, line count,
step309 baseline status. Operators get something useful before any
agent traffic has accumulated.

### Added

* `src/aegis/context_memory/claude_md_proposals.py` — the four
  miners + `Proposal` dataclass + markdown renderer. Self-defended
  via `_KW_*` constants so the source passes its own firewall.

### Changed

* `tools/aegis_cli.py::cmd_memory_claude_md` — expanded from
  locator to proposal generator. New flags: `--since`, `--min-count`,
  `--out`, `--context-memory`.

### Tests

* `tests/unit/test_claude_md_proposals.py` — 17 cases covering
  each miner (threshold, decision-type filter, grouping), dedup
  behavior, priority sorting, markdown rendering edge cases.
* `tests/unit/test_cli_restructure_v05.py` — 2 new CLI integration
  cases: locator-fallback when CM missing, full proposal path
  writing to `--out`.

### Roadmap (next release)

* `--apply N` to auto-splice the Nth proposal into the named
  section, with a `.bak` of the previous CLAUDE.md
* miners 5+: high-cost-tool, provider-drift, advisor-recommendation
  rollup

## [0.5.1] — 2026-05-15  ·  Top-level CLI restructure — operator vocabulary

**Additive only** — no breaking changes. v0.5.1 introduces seven
"intent" commands at the top of `aegis --help`, modeled on the
operator's mental model rather than the underlying module layout.
Every v0.5.0 command keeps working.

### New top-level commands

```
Core commands:
  doctor:        Diagnose cost, performance, security, and mistakes
  report:        Generate a session or timeline report
  live:          Monitor agent execution in real time          (alias for `dashboard`)
  advise:        Show prioritized recommendations
  memory:        Improve: CLAUDE.md and ContextMemory          (new group)
  guard:         Create and test guardrails                    (alias for `rule`)
  coach:         Train sLLM and RAG with saved logs            (new group)

Audit & forensics:
  verify-audit, replay, forensic
```

### Added

* `aegis live` — argparse alias of `aegis dashboard`. Same flags
  (`--refresh`, `--since-hours`, `--demo`, `--context-memory`).
* `aegis guard` — argparse alias of `aegis rule`. All subcommands
  (`list / add / remove / enable / disable / test / import`) work
  identically.
* `aegis coach` — composite group routing to existing training
  commands via a thin `_coach_delegate` router:
    * `aegis coach burnin <action>` → `aegis burnin <action>`
    * `aegis coach calibrate <action>` → `aegis advisor-calibration <action>`
    * `aegis coach case-memory <action>` → `aegis case-memory <action>`
  Every legacy flag passes through unchanged (`REMAINDER`-based
  delegation).
* `aegis memory` — composite group with two own handlers + one
  delegate:
    * `aegis memory show` — print ContextMemory store status (path,
      size, record count, ts range). Walks the JSONL once; resilient
      to malformed lines (crash-recovery scenarios).
    * `aegis memory claude-md` — locate + summarize project
      `CLAUDE.md` / `AGENTS.md`. Surfaces step309 instruction-
      baseline drift status if `AEGIS_INSTRUCTION_BASELINE_PATH` is
      set. The "given recent BLOCK / advise events, propose CLAUDE.md
      edits" feedback loop is roadmapped for 0.5.x.
    * `aegis memory case <action>` — alias for `aegis case-memory
      <action>` (RAG operators reaching for "memory" find it here).

### Changed

* Top-level `aegis --help` now uses a `RawDescriptionHelpFormatter`
  banner that groups commands by **intent** (Core / Audit &
  forensics / System) instead of the argparse default
  alphabetic-by-everything list. The auto-generated subparser
  enumeration is collapsed via `metavar="<command>"`.

### Unchanged (backward compat)

* All v0.5.0 commands keep their canonical names — `dashboard`,
  `rule`, `burnin`, `advisor-calibration`, `case-memory`,
  `fleet-monitor`, etc. — and continue dispatching to the same
  handlers. v0.4.x / v0.5.0 scripts work as-is.
* License-JWT audience handling: still accepts both `aegis-atv` and
  `aegis-mvp` (carried forward from 0.5.0).

### Tests

* `tests/unit/test_cli_restructure_v05.py` — 21 new test cases
  covering: alias resolution (live, guard), coach delegate routing,
  memory handlers (show on empty store, malformed JSONL tolerance,
  claude-md locator in / out of project), top-level help banner
  contract.

### Files changed

* `tools/aegis_cli.py` — `_AEGIS_TOP_HELP` constant, `_coach_delegate`
  router, `cmd_memory_show`, `cmd_memory_claude_md`, new `coach` /
  `memory` parser registrations, `aliases=` on `dashboard` / `rule`,
  top-level parser `description=` + `metavar="<command>"`.
* `pyproject.toml`, `src/aegis/__init__.py` — version → `0.5.1`.
* `CHANGELOG.md` — this entry.
* `tests/unit/test_cli_restructure_v05.py` — new.

## [0.5.0] — 2026-05-15  ·  Naming alignment — Aegis ATV — Agent Telemetry Vector

**Breaking change**: PyPI package renamed `aegis-mvp` → `aegis-atv` to
match the canonical product naming. The Python module name (`aegis`)
and the CLI command (`aegis`) are unchanged. Source-clone users see
no impact; PyPI users must switch install commands.

### Naming canon

| Field | Value |
|---|---|
| Product (short) | **Aegis ATV** |
| Full name       | **Aegis ATV — Agent Telemetry Vector** |
| PyPI package    | `aegis-atv` |
| CLI command     | `aegis` |
| GitHub repo     | `Aegis-ATV` |
| Docker image    | `aegis-atv` |

### Migration

* Install via the new name:
  ```bash
  uv tool install --reinstall aegis-atv
  ```
* Existing JWT licenses (with `aud: aegis-mvp`) keep working — the
  verifier accepts both `aegis-mvp` and `aegis-atv` audience values
  during the 0.5.x transition window.
* The Claude plugin manifest (`.claude-plugin/plugin.json`) renames
  its `name` field from `aegis-mvp` to `aegis-atv`. Existing
  installs will be re-detected on next `aegis install` run.

### Changed

* `pyproject.toml::name` = `aegis-atv`
* `pyproject.toml::description` updated to lead with "Agent Telemetry
  Vector"
* `src/aegis/__init__.py` docstring + version
* `docker-compose.yml` — image / container name → `aegis-atv`
* `README.md` H1 → `Aegis ATV — Agent Telemetry Vector`
* `.claude-plugin/plugin.json` + bundled copy at
  `src/aegis/_data/plugin.json` — name + description updated
* `src/aegis/license/verify.py` — `EXPECTED_AUDIENCE` now `"aegis-atv"`
  with backward-compat set `EXPECTED_AUDIENCES` accepting both old
  and new audience values
* License-related tests + plugin-manifest tests updated to mint with
  the new `aud` value

### Unchanged (preserves history)

* Past CHANGELOG entries (0.4.x and earlier) keep their `aegis-mvp`
  references — they're accurate records of what shipped under that
  name
* `tools/aegis_*.py` docstring headers ("Donor: aegis-mvp v1.0.0")
  are historical attributions and stay as-is
* `openclaw-plugin/` (separate npm package `@happyikas/openclaw-plugin-aegis`)
  is unrelated to this rename

## [0.4.0] — 2026-05-15  ·  One-Stop Console — Dashboard · Tour · Serena integration

User-facing minor release that turns the patchwork of 8+ CLI commands
into **one console**. Three new surfaces — all rich-based, all
Korean-friendly — make Aegis usable by non-engineers without
remembering subcommands:

* **`aegis dashboard`** — one-screen TUI. Auto-refreshing 3-pane
  layout (💰 Cost / ⚡ Performance / 🛡️ Security) + top advisor
  recommendations + recent BLOCKs. Reads ContextMemory; demo mode
  for fresh installs.
* **`aegis tour`** — 60-second interactive onboarding. 7 panel
  walkthrough: 비유 → chokepoint → 시연 → 설치 → 기능 → 다음
  단계. Plain language first.
* **`docs/integrations/serena.md`** — 3-layer stack guide for the
  Aegis × Serena composition (token-efficient code retrieval +
  cryptographic tool-call audit). Design-partner / Show HN
  narrative asset.

Plus minor: the hotfix in 0.3.3 (bundled plugin manifest) is
included.

### Added

* **`aegis dashboard`** (`src/aegis/dashboard/`) — rich-based TUI
  console with auto-refresh (default 2s) reading ContextMemory.
  3-pane stats, advisor recommendations (top 5, sorted high → low),
  recent BLOCKs, demo mode for empty stores, provider compression
  (`openrouter:anthropic-claude-sonnet-4` → `OR:claude-sonnet-4` in
  cost panel). PR #178.
* **`aegis tour`** (`src/aegis/tour/`) — interactive 7-panel
  onboarding walkthrough. Pure read+display, safe to re-run.
  Korean primary, plain-language. `--auto` for demo / test
  pipelines. PR #180.
* **`docs/integrations/serena.md`** — Aegis × Serena 통합 가이드.
  10 sections covering 3-layer stack, setup recipe, ManoMano
  benchmark (~70% token savings on 36K LOC Java), regulated-
  industry recipe (zdr + local-mode), Show HN one-liner. PR #179.
* **`rich>=13.7`** dependency added for TUI rendering.

### Changed

* Top-level `README.md`'s **User manuals** entry now points at both
  the new integrated guide and the deeper per-feature manuals —
  matches the One-Stop Console framing.

### Tests
* 21 new in `test_dashboard.py` (collect_stats / build_layout /
  run_dashboard / CLI wiring / demo invariants / provider
  compression).
* 18 new in `test_tour.py` (step data invariants / rendering /
  runner exit paths / CLI wiring).
* Net: 3044 → 3044 passed, 13 skipped (no regressions).

### Roadmap context

This release ships **Phase 1 + Phase 6 + Phase 5 (docs)** of the
One-Stop Console plan (`docs/USER_GUIDE.ko.md` framing). Remaining
phases (rule authoring, cost timeline, CLAUDE.md auto-reattest,
browser dashboard) ship in `0.5.0` and later.

## [0.3.3] — 2026-05-15  ·  Hotfix — bundle plugin manifest in wheel

### Fixed

* **`aegis install` from `uv tool install` / `pip install` failed** —
  `aegis install --mode local` looked for `.claude-plugin/plugin.json`
  at `PROJECT_ROOT/.claude-plugin/` which is the repo-root hidden
  directory. That works in dev mode (source clone) but the wheel
  package layout (`packages = ["src/aegis", "tools"]`) doesn't carry
  the hidden directory, so non-clone installs hit
  `plugin manifest not found: .../site-packages/.claude-plugin/plugin.json`
  and exited 1.

  Fix is two-fold:
  1. **Bundle the manifest in the wheel** —
     `src/aegis/_data/plugin.json` is now a tracked copy of
     `.claude-plugin/plugin.json` and `pyproject.toml` 's
     `[tool.hatch.build.targets.wheel.force-include]` ships it.
  2. **Resolver fallback** — new `_resolve_plugin_manifest()` tries
     the dev path first (source clone), then falls back to the
     bundled copy via `aegis.__file__.parent/_data/plugin.json`.
     Resolution is deterministic and module-level — no behaviour
     change for source-clone users.

  4 new unit tests in `tests/unit/test_aegis_cli.py` cover dev-path
  priority, bundled fallback, the "both missing" fallthrough, and
  the bundled-file JSON-validity invariant (catches stale copies).

  Verified end-to-end: built the wheel, installed into a fresh
  `uv venv` outside the repo, ran `aegis install --mode local` and
  `_resolve_plugin_manifest()` — both pick up the bundled copy
  cleanly.

## [0.3.2] — 2026-05-15  ·  ContextMemory + aegis doctor + OpenRouter integration

A feature + tooling release. Three big themes — all shippable from the
PitchDeck's "HARDWARE NEXT" and "Multi-LLM provider" surfaces:

* **ContextMemory** — software emulation of the planned CXL SSD /
  Computational SSD near-storage compute layer. Every ATV gets a row
  in a separate analytics store (`~/.aegis/context_memory.jsonl`)
  alongside the audit chain. A second tier (1 KB fixed-size packed
  binary, opt-in via env) mirrors the silicon layout exactly — same
  schema = silicon spec.
* **`aegis doctor`** — CLI that reads ContextMemory and produces a
  6-section markdown report (요약 · Cost · Performance · Security ·
  다음 액션 · footer) with heuristic optimization advice (provider
  dominance, p95 vs < 50ms PitchDeck target, BLOCK-rate drift, etc.).
* **OpenRouter integration** — `aegis.integrations.openrouter`
  helper stamps the canonical Aegis provider field from an OpenRouter
  response so `--by-provider` cross-grouping works on multi-LLM
  routes. Makes the provider-drift advisor (already shipped in 0.3.0)
  immediately useful for OpenRouter users.

Plus `aegis label` (human adjudication CLI — patent ¶[0083] source 1
of 4), the Hermes integration doc, the user-facing manual translated
into Word + PowerPoint, and a cleanup of the 0.3.0 ghost release in
the documentation tree.

### Added

* **ContextMemory binary tier** (`src/aegis/context_memory/binary_emulation.py`)
  — second emulation layer with **1 KB fixed-size packed records**,
  little-endian, NAND-page-aligned. Mirrors the planned CXL SSD /
  Computational SSD silicon layout (Same ATV schema = silicon spec).
  Optional secondary write: set `AEGIS_CONTEXT_MEMORY_BINARY=1` to
  mirror every JSONL write to `~/.aegis/context_memory.bin`. New
  helpers: `pack` / `unpack` / `append_binary` / `iter_binary` /
  `read_all_binary` / `equivalence_check` (cross-tier sanity).
  58 new unit tests covering layout invariants, pack/unpack round-trip
  with Korean / emoji / NaN m13 score, truncation of long fields,
  enum sentinels, writer/reader integration, env override, and
  cross-tier equivalence. Layout: header 48B + body 944B + reserved
  32B = 1024B per record.
* **ContextMemory** (`src/aegis/context_memory/`) — append-only ATV
  analytics store, software emulation of the planned CXL SSD /
  Computational SSD near-storage compute layer (PitchDeck's
  "HARDWARE NEXT" surface). Every audit-record write now also writes
  an analytics-shaped projection to `~/.aegis/context_memory.jsonl`
  (env override `AEGIS_CONTEXT_MEMORY_PATH`). Separate from the
  audit chain by design — different concerns (tamper-evidence vs
  analytics). Hooks land in both local mode (`tools/aegis_local_hook.py`)
  and sidecar mode (`src/aegis/api/evaluate.py`); fully defensive,
  never blocks the verdict path.
* **`aegis doctor`** — Cost · Performance · Security 통합 markdown
  리포트. Reads ContextMemory, runs heuristic advisor on top of
  per-window stats (provider dominance, p95 vs PitchDeck < 50ms
  target, BLOCK rate vs baseline, provider drift 3× threshold,
  dominant-step pattern, etc.). Options: `--since DURATION`,
  `--out FILE`, `--context-memory PATH`. 46 new unit tests covering
  record schema, writer (defensive), reader (malformed-tolerant),
  analytics (window/cost/perf/security), advisor heuristics, and
  the markdown renderer. 7 new CLI integration tests.
* **`aegis.integrations.openrouter`** — Python helper that extracts
  the canonical Aegis `provider` string from an OpenRouter response,
  including fallback-chain resolution. `aegis report --by-provider`
  now cross-groups by *actual* served provider when the caller stamps
  `provider="openrouter:<vendor>-<model>"` into the ATV header, so
  the provider-drift advisor works correctly with OpenRouter routes.
  Pure-Python, no network, 45 unit tests. (PR-this)
* **`docs/integrations/openrouter.md`** — 3-layer stack guide (agent
  runtime → OpenRouter LLM gateway → Aegis tool firewall), code
  examples for both fresh use and OpenClaw composition, honest scope
  table. README + SHOW_HN.md gain a paragraph positioning the
  compose narrative.
* **`aegis label`** (PR #169) — human adjudication CLI; first code
  surface for the patent's "human analyst" label source.
* **`docs/integrations/hermes.md`** (PR #169) — Aegis-as-external-
  observer mapped against Hermes's self-improving-agent positioning,
  with a code-grounded 5-pattern matrix.

## [0.3.1] — 2026-05-11  ·  Audit docs + license gate activation + plugin GA

A documentation- and gate-flip release on top of 0.3.0. No new
public-API surface; the runtime behavior changes are limited to
license-gated refusals (which only fire on Pro+ install paths that
weren't enforceable before). Three landings:

* **OpenClaw plugin GA** — `@happyikas/openclaw-plugin-aegis` lifts
  the `-preview` suffix and publishes as `0.3.0`. The plugin diff
  against `0.2.0-preview.2` is metadata-only; the E2E CI soak window
  (PR #143 → main on 2026-05-09) cleared with zero flake before the
  publish call. The plugin's CHANGELOG / README / install snippets
  + the top-level `README.md` release-tracks matrix + the Korean
  release notes + `SHOW_HN.md` all flip from "preview" to "GA" in
  lockstep. (PR #164, PR #165 — closes [#148](https://github.com/happyikas/Aegis-ATV/issues/148))
* **License-key gate wired to three call sites** —
  `aegis install --profile pro|cloud` refuses without `advisor.full`,
  `aegis install --mode sidecar` refuses without `sidecar.multi-tenant`,
  and the runtime advisor pipeline (`_compute_action_advice` in
  `tools/aegis_local_hook.py`) silently returns `None` when
  `advisor.full` is not granted. Activates `LICENSE_KEY.md` §9
  steps 5–7 on top of PR #157's no-op plumbing. Solo Free / Pro
  installs without sidecar profile are unchanged. The runtime gate
  uses a boot-once sentinel to keep disk I/O off the per-tool-call
  hot path. (PR #163 — closes [#149](https://github.com/happyikas/Aegis-ATV/issues/149))
* **`docs/THREAT_MODEL.md`** — STRIDE walk + auditor checklist for
  the 3rd-party audit. Names the trust boundaries (firewall ↔ sidecar
  ↔ audit chain ↔ license module), per-asset threat tables, and the
  mitigations already in place vs. open. (PR #162)

### Roadmap state

ROADMAP.md refreshed: no items remain "in flight". The three
remaining MVP items (#147 Gap D, #150 ClawHub, #151 Show HN) are
all external-event-gated — design-partner availability or upstream
platform readiness. Code work is closed.

## [0.3.0] — 2026-05-10  ·  Multi-agent + multi-LLM + production hardening

> **PyPI / GHCR note**: `0.3.0` never reached PyPI as a published
> artifact — the tag was missed and the `release-pypi` workflow
> didn't fire. The body of work described below was instead shipped
> in `0.3.1` (2026-05-11), which is what `pip install aegis-mvp`
> resolves to. The GHCR multi-arch image likewise jumps from 0.2.0
> → 0.3.1. The `[0.3.0]` heading is preserved here for historical
> traceability — it reflects the commit set that was originally
> intended for that version. See the `[0.3.1]` entry above.

The first release driven entirely by post-v0.2 feedback rather than
the patent backlog. Two big themes:

1. **Multi-agent + multi-LLM observability** — operators running
   OpenClaw deployments where each agent uses a different LLM
   (some local OSS, some cloud) needed cross-grouped views of the
   audit chain × inference telemetry × baseline learning. Three
   PRs (Gaps A/B/C) close the loop: report-side, infra-side,
   baseline-side.
2. **Sidecar production hardening** — the audit log no longer grows
   unbounded (gzip-compressed rotation), the sidecar has a graceful
   shutdown + /readyz + rate limit + size cap + structured error
   envelope, and there's a load-test harness for the 24h sign-off.

Plus the **commercial offering boundary** is committed in writing
(`PRICING.md`, `LICENSE_KEY.md`) with the no-op runtime gate already
shipped, and the **release pipeline** (PyPI + GHCR multi-arch) is
in place — this very release is going through it.

No breaking changes to the public CLI or HTTP surface. One *intentional*
change to the HTTP error response shape (FastAPI's default
`{"detail": "..."}` → structured `{"error": {"code": ..., "message": ...}}`),
documented under "Changed".

### Added

#### Multi-agent + multi-LLM cross-grouping (Gaps A/B/C)

* **`aegis report --by-aid-and-provider`** — agent-anchored cross-
  table: per aid, sub-rows per provider it has used, per-pair BLOCK
  / approval counts. Per-agent provider-divergence advisor fires
  when an aid's BLOCK rate diverges by ≥3× across providers.
  Combined `--by-aid --by-provider` activates the same view.
  (PR #142, Gap A)
* **`~/.aegis/inference.toml` registry + `aegis metrics --all`** —
  multi-vLLM-server scrape. Each agent maps to its own inference
  backend (vLLM / cloud / disabled); concurrent scrape with graceful
  degradation on unreachable endpoints. New `aegis metrics --aid <name>`
  for single-endpoint by registry label, `aegis report
  --by-aid-and-provider --with-live` cross-references live metrics
  in the report. (PR #154, Gap B)
* **Coach burn-in 3-tuple keying** — L5 baselines now key by
  `(tenant, role, aid, provider)`. The live provider-divergence
  advisor fires *during* evaluation (not just at report time) and
  surfaces in `verdict.step_traces["aegis.coach.provider_drift"]`.
  Backwards compat: legacy 2-tuple records continue to load and
  accumulate cleanly. (PR #155, Gap C)

#### OpenClaw plugin

* **End-to-end test** against a real Aegis sidecar — boots the
  sidecar in a subprocess, exercises ALLOW / REQUIRE_APPROVAL /
  BLOCK paths over real HTTP. New `e2e (plugin ↔ real Aegis sidecar)`
  CI job. Closes the gap that earlier CHANGELOG entries flagged as
  "no end-to-end test against a running OpenClaw runtime + Aegis
  sidecar has been performed". (PR #143)
* **npm scope renamed** to `@happyikas/openclaw-plugin-aegis` (the
  `@openclaw` org wasn't registered). Published live as
  `0.2.0-preview.2`. (PRs #138/#139/#140/#141)

#### Audit log

* **gzip compression of rotated slots** — `.1.gz`..`.K.gz`. With
  default 50 MB × 10 retention, ceiling drops from ~500 MB to
  ~50 MB after gzip. Active file stays plain text. Every reader
  (verify-audit, last-hash, list_rotation_chain) handles both
  formats transparently. (PR #158)
* **Time-based rotation trigger** — opt-in `AEGIS_AUDIT_ROTATE_DAILY=1`
  rotates at the first record's UTC-day boundary, useful for
  per-day SIEM ingestion. (PR #158)
* **`aegis audit status` + `aegis audit prune`** — operator-facing
  surfaces for "what's my audit log doing right now" and "free
  disk space without lowering retention permanently". `--json`
  output for fleet-monitor ingestion. (PR #158)

#### Sidecar production hardening

* **Five middlewares, all opt-out-able** — request size limit
  (1 MiB cap, 413), token-bucket rate limit per (X-Tenant-ID || IP)
  (600/min + 100 burst, 429 with Retry-After), X-Request-ID stamp,
  X-Frame-Options + X-Content-Type-Options + Referrer-Policy
  security headers. Pure-ASGI design (not BaseHTTPMiddleware) so
  500 responses generated by the global exception handler still
  carry the headers. (PR #159)
* **Graceful shutdown** — `LifecycleState.ready=False` flips on
  SIGTERM (→ /readyz returns 503 → load balancers drain), 5s grace
  window, then registered flush_callbacks. Tolerates individual
  callback failures. (PR #159)
* **`/readyz` distinct from `/healthz`** — runs probes against the
  audit DB + audit log path; 200 when all pass + state.ready, 503
  otherwise. Probes that raise are treated as failed. (PR #159)
* **Structured error envelope** — `{"error": {"code": ..., "message":
  ...}}` for every error response. Catch-all 500 emits "internal_
  error" (no traceback in body, only in structlog). Validation 422
  exposes the field-specific error list. HTTPException maps to
  `http_<status>` codes. (PR #159)

#### Commercial offering boundary

* **`PRICING.md`** — Solo Free $0 (forever, Apache-2.0) / Solo Pro
  $19/mo / Team $39/seat/mo / Enterprise custom. Explicit boundary,
  what's never gated, FAQ. (PR #144)
* **`docs/LICENSE_KEY.md`** — Ed25519 JWS validation design,
  offline-first, opt-in CRL refresh, optional burn-in bind. (PR #144)
* **License-key runtime gate** (`src/aegis/license/`) — no-op gate
  plumbing. JWS verify, tier feature manifest, `has_feature()` /
  `require_feature()`, `aegis license activate / status / deactivate
  / verify / refresh` CLI. 75 tests, 94% coverage. Solo Free
  behavior unchanged — the gate doesn't gate any feature yet
  (steps 5-7 of `LICENSE_KEY.md §9` light up per-feature). (PR #157)

#### Release + distribution

* **PyPI publish workflow** (`.github/workflows/release-pypi.yml`)
  — tag-triggered, trusted-publisher (no API tokens), pre-publish
  version match check, dry-run path. (PR #156)
* **GHCR multi-arch publish** (`.github/workflows/release-docker.yml`)
  — linux/amd64 + linux/arm64, semver tags + `:latest` (only on
  stable, not prereleases). (PR #156)
* **Slim sdist** — `[tool.hatch.build.targets.sdist]` allow-list
  drops PDFs / GIFs / audio / model files. **17 MB → 628 KB**, 27×
  smaller. Wheel unchanged at 680 KB. (PR #156)
* **`docs/RELEASE_PIPELINE.md`** — one-time setup runbook + 3-line
  per-release procedure + rollback strategy. (PR #156)

#### Developer experience

* **`aegis soak` + `aegis bench`** — load test harness. soak is the
  24h sign-off (default rate 10/s); bench is the same harness with
  CI-friendly defaults (5 min / 50 RPS). Pass/fail on error rate +
  p99 latency + audit chain integrity. Reservoir sampling for
  latency keeps memory flat over 24h. `docs/SOAK_TEST.md` runbook.
  (PR #160)
* **`ROADMAP.md`** — public roadmap mirrored from open issues +
  inline `gh project create` recipe. (PR #152)
* **`SHOW_HN.md` refresh** for current state — 16-step pipeline,
  npm-published OpenClaw plugin, multi-agent cross-grouping,
  Apache-2.0. The April-2026 draft is preserved at the bottom as
  archive. (PR #153)

### Changed

* **HTTP error response shape** — every error now follows the
  `{"error": {"code": ..., "message": ...}}` envelope (PR #159).
  Pre-v0.3 callers depending on FastAPI's `{"detail": "..."}`
  shape need to switch to `["error"]["message"]`. The internal
  test suite was updated alongside; three integration tests in
  `tests/integration/test_*.py` track the new shape.

### Internal

* **~2,759 tests passing** (1,761 → 1,940 → 1,966 across the
  release cycle, plus integration tests). mypy strict clean over
  188 source files. ruff clean across the whole repo.
* **Pure-ASGI middleware design note** — see `src/aegis/api/middleware.py`
  module docstring + the inline rationale in PR #159's commit body.
  Worth reading before adding any new middleware.

### Migration

No code changes required for existing users. If you parse the
sidecar's HTTP error responses programmatically:

```diff
- error_msg = response.json()["detail"]
+ error_msg = response.json()["error"]["message"]
```

The audit log layout changes are backwards-compatible: legacy
uncompressed `audit.jsonl.N` files keep working; new rotations
produce `audit.jsonl.N.gz`; `aegis verify-audit` walks both.

## [0.2.0] — 2026-05-09  ·  Coach / Live / Doctor + three release tracks

First substantive iteration on the Personal MVP since the v0.1.0
public release. No breaking changes: every existing command behaves
exactly as before. Net addition: ~3,500 lines of feature work + docs
across PRs #119–#129.

### Added

* **🏋️ ATV Coach / 📊 ATV Live / 🔧 ATV Doctor product feature buckets**
  — same firewall + audit chain core, three named user-facing features.
  All CLI subcommands tagged with bucket emoji in `aegis --help`.
  Korean canonical user manuals: [`docs/manuals/`](docs/manuals/).
  (PR #126)
* **Three release tracks** — `aegis install --target {claude-code,
  openclaw-local,openclaw-cloud}`. claude-code is GA; the two OpenClaw
  tracks are Preview with friendly install stubs pointing at
  [`docs/releases/`](docs/releases/). (PR #127)
* **`@openclaw/plugin-aegis` TypeScript plugin skeleton** — under
  `openclaw-plugin/`. Maps OpenClaw `before_tool_call` to Aegis
  ALLOW/BLOCK/REQUIRE_APPROVAL/PARAM-REWRITE verdicts. Configurable
  fail-open vs fail-closed on sidecar errors. 19 vitest tests; new
  GitHub Actions workflow `openclaw-plugin.yml`. (PR #128)
* **vLLM `/metrics` Prometheus scraper** — new `aegis metrics --vllm-url`
  CLI under `📊 ATV Live`, plus `src/aegis/inference/` module with
  `InferenceMetrics` dataclass + `scrape_vllm_metrics()`. KV cache
  utilization, queue depth, TTFT/TPOT histograms, speculative-decoding
  efficiency. 28 pytest tests with real vLLM 0.6.x fixture. Cloud LLM
  tracks return a friendly "this surface is not exposed" hint. (PR #129)
* **5 Claude Code custom slash commands** — `/aegis-report`,
  `/aegis-verify`, `/aegis-advise`, `/aegis-forensic`, `/aegis-help`.
  Auto-installed by `aegis install` into `~/.claude/commands/`.
  (PR #121)
* **SessionStart welcome hint** — first-session onboarding via the
  Claude Code SessionStart hook. Idempotent via `~/.aegis/.welcomed`
  marker; opt-out via `AEGIS_WELCOME_DISABLE=1`. (PR #125)
* **Three release-track docs** — `docs/releases/CLAUDE_CODE.ko.md`,
  `OPENCLAW_LOCAL.ko.md`, `OPENCLAW_CLOUD.ko.md` + 1-page decision
  matrix in `docs/releases/README.md`. (PR #127)
* **Three feature-bucket manuals (Korean canonical)** — `docs/manuals/
  COACH_MANUAL.ko.md`, `LIVE_MANUAL.ko.md`, `DOCTOR_MANUAL.ko.md` +
  index. (PR #126)
* **Integration analyses** — `docs/integrations/openclaw.md` (430
  lines, "best fit" verdict drove PRs #128/#129) +
  `docs/integrations/paperclip.md` (361 lines, "doesn't fit" verdict).
  (PRs #117, #118)
* **5-min screencast script** — `docs/launch/screencast-v0.1.0.md`.
  (PR #123)
* **README rebrand** — cryptographic-audit lead + side-by-side comparison
  table vs Claude Code's built-in flags + "Three release tracks" hero
  section. (PRs #119, #124, #126, #127)

### Changed

* Subcommand `--help` strings now prefix with bucket emoji (🏋️ Coach
  / 📊 Live / 🔧 Doctor) so the product structure is visible in the
  default `aegis --help` output. No CLI behaviour change.
* `aegis install` default target preserved (`claude-code`) — existing
  users see no change. New `--target` flag is opt-in.

### Deferred (Preview / Roadmap)

* `@openclaw/plugin-aegis` end-to-end test against running OpenClaw
  runtime (handler is mock-tested only).
* `@openclaw/plugin-aegis` npm publish (version `0.1.0-preview.1`).
* vLLM scraper `--watch` continuous mode (single-shot only).
* vLLM `InferenceMetrics` → ATV vector integration (CLI-only today).
* Ollama / TGI inference adapters.
* Model-weight hash baseline (step309 OpenClaw variant).
* Logit-level forensic.

### Notes

* PR #127 introduces three release tracks but the OpenClaw plugin
  package (`openclaw-plugin/`) is independently versioned at
  `0.1.0-preview.1` — the Aegis Python core's v0.2.0 is unrelated to
  the plugin's npm version.

## [0.1.0] — 2026-04-30  ·  Personal MVP public release

Initial public release of the Personal MVP under Apache-2.0. Renamed
the project from "AegisData T2 MVP" (versioning track 4.x, v2.x) to
"Aegis ATV" (versioning track 0.x, target 1.0). The 4.x history below
covers the pre-public T2 sidecar work that this MVP builds on.

## [4.4.0] — 2026-04-29  ·  TEE-rooted attestation deployment (Claim 58)

Promotes the audit chain's trust root from host OS to **TEE silicon**
(Intel TDX, AMD SEV-SNP). v4.1 had mock TEE collectors; v4.4 ships
real ioctl bindings, a pluggable quote verifier, and a sealed-key
abstraction. **Auto-detects** the TEE provider — same binary works
on T2 dev hosts (mock fallback) and T3 production silicon (real
attestation).

### Added

* `src/aegis/attest/tee_ioctl.py` — Real `ctypes`-based ioctl bindings:
  - `fetch_tdx_report()` — `TDX_CMD_GET_REPORT0` (1024-byte TDREPORT)
  - `fetch_sev_snp_report()` — `SNP_GET_REPORT` (4000-byte attestation)
  - Parses MRTD / measurement / report_data from the raw ABI struct.
* `src/aegis/attest/tee_quote.py` — `_tdx_quote()` / `_sev_snp_quote()`
  upgraded from placeholders to real ioctl path. Auto-fallback to
  mock when device missing.
* `src/aegis/attest/tee_verifier.py` — `TEEQuoteVerifier` with
  pluggable backends:
  - Default: schema-only verification (mock + TDX + SEV-SNP)
  - Production: `register_provider()` with Intel DCAP / AMD KDS / etc.
* `src/aegis/sign/sealed_key.py` — `SealedKeyProvider` Protocol +
  `LocalSealedKey` (fallback) + `SEVSNPDerivedKey` / `TDXSealedKey`
  stubs (real ioctl path = v4.5 milestone). `detect_sealed_key_provider()`
  auto-selects strongest available.
* `src/aegis/hw_telemetry/collectors/mock_tee_quote.py` —
  `MockTEEQuoteCollector` upgraded from static SHA3 to auto-detecting
  collector. Class name kept for v4.1 backward compat.
* `src/aegis/api/attestation.py` — `GET /attestation/tee` now runs
  the verifier alongside fetch. New `POST /attestation/tee/verify`
  for cross-org / peer-host verification.
* `docs/T3_DEPLOYMENT_GUIDE.md` — Azure Confidential VM (TDX), AWS
  r7iz (SEV-SNP), NVIDIA H100 CC, troubleshooting, production verifier
  swap-in (Intel DCAP / AMD KDS).

### Patent

* `docs/PATENT_SUPPLEMENT_v3.md` adds **Claim 58** — TEE-rooted
  attestation deployment with auto-detect + mock fallback + pluggable
  verifier.

### Numbers

* **1177 tests PASS** (1138 → 1177, +39), 1 skipped (llama-cpp).
* **mypy 125 source files clean.**
* **ruff clean.**

### Config

```bash
AEGIS_TEE_PROVIDER=tdx          # auto-detect / mock / tdx / sev-snp
AEGIS_HW_PROVIDER=real          # v4.1 collector aggregator
AEGIS_TEE_SEAL_KEYS=auto        # local / auto (v4.5 stubs available)
```

### Breaking change (minor)

`MockTEEQuoteCollector.collect()` metadata schema changed:
- Old keys: `quote_sha3`, `trust_level=unverified`
- New keys: `tee_provider`, `trust_level ∈ {mock, tdx-attested, sev-snp-attested}`,
  `enclave_measurement`, `report_data`, `raw_quote_size_bytes`

Existing v4.1 tests in `test_hw_collectors.py` updated to new schema.

---

## [4.3.0] — 2026-04-29  ·  Compliance evidence automation (Claim 57)

Turns the existing audit primitives into structured compliance
evidence packets for **SOC 2 / EU AI Act / HIPAA / ISO 42001**.
**29 of 31 controls** automatically covered; the 2 not_implemented
are honestly flagged (training procedure = model provider's
responsibility; TLS transmission = external mesh layer).

### Added

* `src/aegis/compliance/frameworks.py` — 4 framework definitions:
  - **SOC 2 TSC** — CC6/CC7/CC8 + A1.2 (9 controls)
  - **EU AI Act Annex IV** — Article 12 + Annex IV §2-§6 (9 controls)
  - **HIPAA** — 45 CFR § 164.312(a)-(d) (7 controls)
  - **ISO/IEC 42001 AIMS** — A.5.2/A.6/A.8/A.9/A.10 (6 controls)
* `src/aegis/compliance/evidence.py` — `EvidenceCollector` walks the
  audit stores (audit DB, encrypted journal, ATMU intent log, cost
  ledger, AuditPatrol reports) and produces a `ComplianceReport`
  with one `ControlEvidence` per control.
* **Deterministic sampling** — same (audit, period, framework) →
  bit-identical evidence packet. Seed = SHA3(control_id + period_start).
  Audit replay reproducible.
* Output formats: JSON (machine-readable), Markdown (auditor-friendly).
* `src/aegis/api/compliance.py` — `GET /compliance/frameworks` (list)
  + `POST /compliance/evidence` (generate per-period packet).

### Patent

* `docs/PATENT_SUPPLEMENT_v3.md` adds **Claim 57** — automated mapping
  of cryptographically-signed agent audit primitives to compliance
  control frameworks with deterministic sample selection.

### Numbers

* **1138 tests PASS** (1106 → 1138, +32), 1 skipped (llama-cpp).
* **mypy 122 source files clean.**
* **ruff clean.**
* 4 frameworks × 31 total controls; 29 covered, 2 honestly not_implemented.

### Operator UX

```bash
# List frameworks:
curl /compliance/frameworks

# Generate Q1 SOC 2 evidence packet (Markdown):
curl -X POST /compliance/evidence -d '{
  "framework": "soc2",
  "period_start_ns": 1735689600000000000,
  "period_end_ns": 1743465600000000000,
  "format": "markdown"
}' > soc2-2026-Q1.md
```

---

## [4.2.0] — 2026-04-29  ·  Agent identity & MCP integration (Claim 56)

Multi-agent system identity layer with W3C DID compatibility,
Anthropic Model Context Protocol (MCP) hook pattern, and Ed25519-
signed delegation chains that enforce capability subset along the
chain (no escalation).

### Added

* `src/aegis/identity/agent_id.py` — `AgentIdentity`, `IdentityProof`
  (compact-token serialisation), `DelegationChain` (capability-subset
  enforcement, tenant + parent_aid linkage).
* `src/aegis/identity/did.py` — pluggable W3C DID resolver:
  - `did:aegis:<tenant>:<aid>` — local pubkey lookup (single-org)
  - `did:key:z<base58btc>` — pubkey embedded in DID (cross-org trust)
  - `did:web:<host>:<path>` — stub (production swaps in HTTPS resolver)
* `src/aegis/identity/mcp.py` — `MCPAegisMiddleware` reference adapter:
  issue identity proof, verify inbound proof, build call context,
  dispatch to `/evaluate`. Pure Python, no MCP SDK dependency.
* `src/aegis/firewall/step308_identity.py` — new firewall step that
  reads `ATVInput.agent_identity_proof_token` (new field), verifies
  signature + expiry + tenant/aid/capability fit, attaches verified
  identity to `ctx.extras["verified_identity"]` for later steps.
* Backward compat: when no proof is present and `AEGIS_IDENTITY_REQUIRE=false`
  (default), step308 is a no-op. Production sets the env var to enforce.

### Patent

* `docs/PATENT_SUPPLEMENT_v3.md` adds **Claim 56** — agent identity
  with capability-subset delegation chain, MCP server-side hook pattern,
  W3C DID-Agent compatibility.

### Numbers

* **1106 tests PASS** (1075 → 1106, +31), 1 skipped (llama-cpp).
* **mypy 118 source files clean.**
* **ruff clean.**
* Identity proof sign+verify: <0.5 ms per call.
* Three-level delegation chain validation: <2 ms.

### Config

```bash
AEGIS_IDENTITY_REQUIRE=true     # enforce identity proof on every call
                                # (default false for backward compat)
```

---

## [4.1.0] — 2026-04-29  ·  HW telemetry collectors — multi-source aggregator (Claim 55)

Real hardware data finally flows into the ATV HW band. Up through
v4.0, the 200-D HW band was either zero-filled (T2 default) or fed
by the v2.3 SHA3 simulator. v4.1 introduces an 8-source collector
framework that reads from standard Linux interfaces (`/proc`, `/sys`),
NVML, ethtool, Redfish — and graceful-degrades to the simulator
baseline for any slot that doesn't have a real source on this host.

### Added

* `src/aegis/hw_telemetry/collectors/` — new package:
  - `base.py` — `HWCollector` Protocol + `CollectorResult` dataclass.
  - `pmu.py` — CPU PMU via `/proc/stat` + `/proc/loadavg`.
  - `edac.py` — DRAM ECC via Linux EDAC subsystem.
  - `iommu.py` — DMA fanout via `/sys/kernel/iommu_groups/`.
  - `ethtool.py` — NIC counters via `/proc/net/dev`.
  - `nvml.py` — NVIDIA GPU via optional `pynvml` dependency.
  - `bmc_redfish.py` — out-of-band BMC via Redfish HTTP.
  - `mock_tee_quote.py` — Intel TDX / AMD SEV-SNP / ARM CCA placeholder.
  - `mock_aegis_fpga.py` — M21+ custom silicon placeholder.
  - `aggregator.py` — `CollectorAggregator` with frozen merge priority,
    `availability_report()` for ops, and `aggregate_from_env()`
    factory.
* `src/aegis/hw_telemetry/simulator.py` — `simulate_from_env()` now
  routes `AEGIS_HW_PROVIDER=real` to the aggregator (the v2.3 `sim`
  path stays unchanged).
* `src/aegis/config.py` — `aegis_hw_provider` literal extended with
  `"real"`.

### Patent

* `docs/PATENT_SUPPLEMENT_v3.md` adds **Claim 55** — multi-source HW
  telemetry aggregator with frozen collector priority order.

### Numbers

* **1075 tests PASS** (1045 → 1075, +30), 1 skipped (llama-cpp).
* **mypy 113 source files clean.**
* **ruff clean.**
* All new functionality opt-in via `AEGIS_HW_PROVIDER=real`.

### T3 swap-in path

When real silicon arrives (M19+):
- `MockTEEQuoteCollector` → real TDX/SEV-SNP/CCA quote provider
- `MockAegisFPGACollector` → real PCIe/MMIO read of FPGA counters

Aggregator + firewall + audit chain stay unchanged.

---

## [4.0.0] — 2026-04-29  ·  AuditPatrol — periodic background integrity check (Claim 54)

Closes the open question from the v3.9 whitepaper: "what catches
silent corruption / bit-rot / missing records *between* reads?"
v3.x verifies integrity **on demand** (Ed25519 sigs at write, AES-GCM
auth tags at decrypt, `aegis verify-audit` CLI). v4.0 adds a
**continuous background patrol** that walks the stores on its own
cadence and surfaces findings before the next reader trips over them.

### Added

* `src/aegis/audit/patrol.py` — `AuditPatrol` daemon with five patrol
  scopes:
  - **sequence** (5 min default) — ATMU (Agent Telemetry Management
    Unit) `intent_log.seq` gap detection
  - **sample** (1 h) — random 1 % subset; signature + SHA3 recompute
  - **consistency** (1 h) — cross-check SQLite ↔ JSONL ↔ encrypted
    journal (record presence + AEAD tag)
  - **full** (6 h) — every aid's chain in audit DB + cost ledger
    (Merkle + Ed25519)
  - **cold** (24 h) — sample N segments from the v3.9 cold tier and
    re-decrypt
  Each scope returns a `PatrolReport` with structured `PatrolFinding`s
  classified by category (signature, hash_mismatch, chain_break, aead,
  consistency, sequence_gap) and severity (warning, critical).
  Rolling 50-report history kept in memory for ops dashboards.
* `src/aegis/api/audit_patrol.py` — `GET /audit/patrol/status` and
  `POST /audit/patrol/run` endpoints.
* `src/aegis/main.py` — auto-wires the patrol when
  `AEGIS_AUDIT_PATROL_ENABLED=true`.

### Patent

* `docs/PATENT_SUPPLEMENT_v3.md` adds **Claim 54** — periodic 6-check
  integrity attestation. T3 hardware (M19+) extension: patrol report
  itself signed under the cost-attestation key (Claim 34) so the
  patrol can't lie either.

### Config (all default off)

```bash
AEGIS_AUDIT_PATROL_ENABLED=true
AEGIS_AUDIT_PATROL_FULL_INTERVAL_SEC=21600          # 6h
AEGIS_AUDIT_PATROL_SAMPLE_INTERVAL_SEC=3600         # 1h
AEGIS_AUDIT_PATROL_SEQUENCE_INTERVAL_SEC=300        # 5min
AEGIS_AUDIT_PATROL_CONSISTENCY_INTERVAL_SEC=3600    # 1h
AEGIS_AUDIT_PATROL_COLD_INTERVAL_SEC=86400          # 24h
AEGIS_AUDIT_PATROL_SAMPLE_FRACTION=0.01             # 1 %
AEGIS_AUDIT_PATROL_COLD_SEGMENTS_PER_RUN=3
AEGIS_AUDIT_PATROL_POLL_SECONDS=30
```

### Tests

* `tests/unit/test_audit_patrol.py` (26 tests) — every scope covered
  with both clean-chain and corrupted-chain inputs (signature tamper,
  hash mismatch, sequence gap, AEAD tamper, JSONL drift, cold-tier
  decrypt). Lifecycle (start/stop/double-start). Endpoint integration.

### Numbers

* **1045 tests PASS** (1019 → 1045, +26), 1 skipped (llama-cpp).
* **mypy 102 source files clean.**
* **ruff clean.**
* All new functionality opt-in; existing test surface unaffected.

---

## [3.9.0] — 2026-04-28  ·  Production durability — group-commit + tiered archive

Bridges the gap between T2 demo (memory + per-call sync) and the four
production durability patterns documented in
`docs/WHITEPAPER_PERFORMANCE_KR.md` §2 (group commit / tiered / replicated
WAL / Raft). v3.8 ships pattern A; v3.9 ships pattern B.

### v3.8 — Group commit + persistent perf EWMA

* `src/aegis/audit/group_commit.py` — `GroupCommitEncryptedJournal`
  drop-in replacement for `EncryptedJournal`. Batches up to N records
  or `interval_ms` ms into a single `open() / write_all / fsync /
  close()` cycle. Each `append()` blocks until its batch is durable,
  preserving caller contract. On-disk format is bit-identical so the
  plain `EncryptedJournal` reads records group-committed earlier.
  `make_journal()` factory + flag-driven via
  `AEGIS_JOURNAL_GROUP_COMMIT`.
* `src/aegis/audit/encrypted_journal.py` — split `encrypt(record)` and
  `serialize(wrapper)` so wrappers can be staged without I/O. Plain
  `append()` now does `os.fsync(fileno())` for true durability
  (was previously `flush()` only).
* `src/aegis/performance/feedback_snapshot.py` —
  `PerfFeedbackSnapshotter` background daemon that periodically writes
  the v3.2 EWMA store to SQLite. Trigger:
  `min(interval_sec, updates_per_snapshot)` (default 30 s, 100).
  `load_into_store()` restores prior EWMA on boot so advisor confidence
  doesn't reset. Wired via `AEGIS_PERF_FEEDBACK_SNAPSHOT_DB`.

### v3.9 — Tiered archive (hot → cold)

* `src/aegis/audit/tiered_archive.py` —
  `TieredArchiveMigrator` background coordinator that:
  - Rotates the live journal file when it exceeds `rotate_bytes` or
    `rotate_seconds`.
  - Pushes closed segments to a pluggable `ArchiveBackend`:
    `FilesystemArchive` (default — `cp` to `cold_dir/`) or
    `S3ArchiveStub` (interface for S3/GCS/Azure Blob; production
    impl plugs in boto3).
  - Prunes hot tier after `hot_retention_segments` archived copies are
    safe.
* Encryption + commitment chain unchanged — replay still works against
  cold-tier files with the same data key.
* Wired via `AEGIS_TIERED_ARCHIVE_COLD_DIR`.

### Config changes

* `aegis_perf_feedback_snapshot_db` (path, default empty)
* `aegis_perf_feedback_snapshot_interval_sec` (default 30.0)
* `aegis_perf_feedback_snapshot_updates_threshold` (default 100)
* `aegis_journal_group_commit` (default False)
* `aegis_journal_group_commit_batch_size` (default 100)
* `aegis_journal_group_commit_interval_ms` (default 1.0)
* `aegis_tiered_archive_cold_dir` (path, default empty)
* `aegis_tiered_archive_rotate_bytes` (default 100 MB)
* `aegis_tiered_archive_rotate_seconds` (default 3600)
* `aegis_tiered_archive_hot_retention_segments` (default 3)
* `aegis_tiered_archive_poll_seconds` (default 10)

### Tests

* `tests/unit/test_feedback_snapshot.py` (11) — round-trip, trigger
  logic, lifecycle, simulated-restart EWMA continuity.
* `tests/unit/test_journal_group_commit.py` (10) — round-trip, durable-
  on-return, factory, validation, concurrent appends, cross-compat
  with plain journal, drain on close.
* `tests/unit/test_tiered_archive.py` (16) — backend, rotation,
  archive idempotency, hot-tier retention, lifecycle, encrypted-
  journal cross-tier replay.

### Numbers

* **1019 tests PASS** (982 → 1019, +37), 1 skipped (llama-cpp).
* **mypy 100 source files clean.**
* **ruff clean.**
* All new modules opt-in (off by default), so existing test surface
  is unaffected. T3 hardware (M19+) will swap the filesystem backend
  for a CSD-backed durable region.

---

## [3.7.0] — 2026-04-28  ·  Context window advisor

ATV-based **token-budget-aware** decision of which historical turns
to keep verbatim, summarise, or drop. Different axis from KV cache:
KV cache works at the runtime memory layer; context advisor works
at the prompt-construction layer. Both consume the same ATV.

### Added

* `src/aegis/performance/context_advisor.py` — pure function
  `(current_atv, history_atvs, history_turn_ids, history_token_costs,
  token_budget) → ContextAdvice` with `keep_verbatim_turn_ids`,
  `summarize_turn_ids`, `drop_turn_ids`, `expected_token_savings`,
  per-turn relevance scores, `advisor_hash`. Frozen weights (0.45
  state cosine, 0.20 progress match, 0.10 novelty proximity, 0.25
  recency with 8-turn half-life). Greedy ROI fit under token_budget.
* `src/aegis/api/advisory.py` — `POST /advisory/context` accepting
  current ATVInput + list of historical (turn_id, atv_input,
  token_cost) + token_budget.
* `demo/context_advisor.py` — 12-turn three-phase conversation,
  three budgets (5000 / 2000 / 800 tokens). Recent same-phase
  turns score 0.85+ → keep; older different-phase turns drop first.
* `tests/unit/test_context_advisor.py` — 14 unit tests covering
  pure-function shape, determinism, budget fit, recency tie-breaks,
  per-turn bucket consistency, latency, endpoint integration.

### Patent

* `docs/PATENT_SUPPLEMENT_v3.md` — Claims 48–50 added:
  * **Claim 48** — context window advisory head over ATV history
    (implemented).
  * **Claim 49** — subfield-selective ATV diff compression (deferred).
  * **Claim 50** — unified head v2 with 5 outputs including context
    (deferred to v3.8).

### Numbers

* **982 tests PASS** (968 → 982, +14), 1 skipped (llama-cpp).
* **mypy 97 source files clean.**
* **ruff clean.**
* Latency: 0.087 ms for 50-turn history (M3 Mac).
* Demo savings: 50 % (budget=5000) / 67 % (2000) / 87 % (800)
  on a 12-turn 6050-token simulated history.

---

## [3.6.0] — 2026-04-28  ·  Performance advisory surface (v3.1 → v3.6)

The same ATV-2080 that powers the trust firewall now drives **out-of-band
performance advisory** for LLM serving runtimes. Six chained releases
land in one milestone.

### v3.1 — KV cache advisor

* `src/aegis/performance/kv_cache_advisor.py` — pure function
  `(atv, inp) → KVCacheAdvice` with `prefetch_segment_ids`,
  `evict_candidates`, `residency_class` (hot/warm/cold), `batch_key`,
  `speculative_decode`, `confidence`, `advisor_hash`.
* `src/aegis/api/advisory.py` — `POST /advisory/kv_cache`.
* Sub-millisecond, deterministic, advisory-only (runtime is the enforcer).

### v3.2 — Closed-loop perf feedback

* `src/aegis/performance/feedback.py` — thread-safe per-(tenant, aid)
  EWMA store (α=0.30). Process-wide singleton.
* `src/aegis/api/tool_outcome.py` — extended with optional
  `cache_hit_rate` / `context_utilization_ratio` / `tokens_per_second` /
  `runtime_latency_ms` / `memory_peak_bytes`. Updates the EWMA on
  receipt; returns the snapshot.
* `src/aegis/api/{advisory,evaluate}.py` — backfill `s-10/s-11` when
  the host hasn't measured. Host-supplied values are NEVER overwritten.

### v3.3 — Runtime adapters

* `integrations/mlx_lm/__init__.py` — `MLXLMAegisAdvisor`: residency →
  sliding_window (hot=16k, warm=4k, cold=2k); speculative → draft model.
* `integrations/llama_cpp/__init__.py` — `LlamaCppAegisAdvisor`:
  residency → kv_cache_dtype (f16/q8_0) + n_gpu_layers delta.
* `demo/runtime_closed_loop.py` — 8-turn simulated runtime, watches
  EWMA + advice confidence climb.

### v3.4 — Scheduling + Placement advisors

* `src/aegis/performance/scheduling_advisor.py` — `(priority_class,
  preempt_safe, max_concurrent_in_cohort, deadline_ms)`.
* `src/aegis/performance/placement_advisor.py` — `(layer_residency_plan,
  kv_quantisation_dtype, prefetch_window_tokens, swap_threshold_bytes)`.
  Demotes middle blocks under high pressure; T3 routes cold layers
  to CSD instead of CPU.
* New endpoints: `/advisory/scheduling`, `/advisory/placement`,
  `/advisory/all` (one-shot fan-out).

### v3.5 — vLLM integration shim + design doc

* `integrations/vllm/__init__.py` — `VLLMAegisAdvisor` posts to
  `/advisory/all` and projects onto `VLLMAdvice`.
* `docs/VLLM_INTEGRATION_DESIGN.md` — three plug points
  (`AegisAwareBlockManager`, `AegisAwareScheduler`, `AegisAwarePrefetcher`).

### v3.6 — M13 unified head

* `src/aegis/judge/unified_head.py` — `UnifiedHead.evaluate_unified()`
  composes the v2.5 AttributionHead with the v3.1 / v3.4 advisors
  in one ATV pass. `unified_hash` = SHA3-256 over the four advisor
  versions — audit replay catches any head change. Trust path is
  bit-identical to standalone AttributionHead.
* `POST /advisory/unified` — runtime gets trust + perf in one call.

### Patent

* `docs/PATENT_SUPPLEMENT_v3.md` (Korean) — provisional supplement
  proposing Claims 41–47 extending the existing `ATV_v7_10` filing
  with the perf-advisory surface, closed-loop attestation, unified
  head, and advisor-as-hint protocol.

### Tests / lint / types

* **968 tests PASS** (905 → 968, +63), 1 skipped (llama-cpp).
* **mypy 96 source files clean.**
* **ruff clean.**

---

## [3.0.0] — 2026-04-28  ·  ATV-native sLLM stack: M13 + Phi + hybrid combiner

The patent's three-tier sLLM vision (Claims 8 / 9) lands as a working
hybrid stack. v2.5 + v2.6 + v3.0 ship together as v3.0.0.

### Added — v2.5 M13 AttributionHead

* **`src/aegis/judge/attribution_head.py`** — frozen 30-feature linear
  classifier that reads the 2080-D ATV vector directly (not a text
  summary). Hand-tuned weights in `models/m13_attribution_head_v1.json`,
  SHA3-256 hashed at load time as `model_hash`.
* `evaluate_full(summary, atv, inp)` returns a `JudgeVerdict` with the
  full 30-key `subfield_attribution` map populated for the first time
  (Dummy / Haiku had returned empty dicts).
* <1ms inference, IEEE-754 deterministic, auditable via the frozen
  weights' SHA3 hash.

### Added — v2.6 LocalPhiJudge

* **`src/aegis/judge/local_phi.py`** — Phi-4-mini-q4 / Llama-family
  local sLLM with three-mode dispatch:
  * **Real** — `AEGIS_JUDGE_MODEL_PATH=/path/to/phi.gguf` + `llama-cpp-
    python` installed: GGUF-loaded greedy-decode (T=0, top_k=1).
    `model_hash` = SHA3-256 of the GGUF file.
  * **Stub** — no path / `AEGIS_JUDGE_LOCAL_PHI_STUB=1`: delegates to
    M13 AttributionHead and re-labels the reason. Deterministic,
    audit-clean, CI-friendly.
  * **Disabled** — env points at missing file or llama-cpp-python
    missing: returns confidence=0.0 ALLOW so the v3.0 HybridJudge
    routes past it.
* Prompt embeds the M13 attribution top-5 contributors so the LM
  has structured signal alongside the summary.
* `_parse_real_decode` accepts both strict JSON output and keyword-
  fallback for robust small-model inference.

### Added — v3.0 HybridJudge

* **`src/aegis/judge/hybrid.py`** — confidence-routing combiner over a
  layered Judge stack. Default tiers, in increasing latency × cost ×
  non-determinism order:

  | Tier | Judge | Latency | Determinism |
  |---|---|---:|---|
  | 1 | `m13_attribution` (AttributionHead) | <1 ms | bit-identical |
  | 2 | `local_phi` (stub or real Phi-4-mini-q4) | <1ms / ~50 ms | bit-identical (stub) / attestable (real) |
  | 3 | `haiku` (Anthropic API, only when `ANTHROPIC_API_KEY` set) | ~150 ms | "approximately stable" |
  | 4 | `dummy` (regex) | <1 ms | bit-identical |

* Routing rule: a tier "commits" on BLOCK / REQUIRE_APPROVAL OR on
  ALLOW with `confidence ≥ allow_threshold`. Low-confidence ALLOW
  escalates to the next tier — the "fail-safe escalation" pattern.
* `JudgeVerdict.layer_traces` records each consulted tier's
  ``"name: decision conf=X.XX (T.Tms)"``. `model_hash` set to the
  *deciding* tier's hash so `aegis verify-audit` can re-run the
  exact path. `latency_ms` is the cumulative wall-clock.

### Changed

* **`src/aegis/judge/base.py`** — `JudgeVerdict` gains optional
  `model_hash`, `latency_ms`, `layer_traces` fields (default values
  preserve all existing tests). `Judge` gains `evaluate_full(summary,
  *, atv, inp)` with default fallback to `evaluate(summary)` —
  backward compatible.
* **`src/aegis/judge/__init__.py`** — `get_judge()` routes
  `attribution_head`, `local-phi`, and `hybrid` providers.
* **`src/aegis/firewall/step340_policy.py`** — calls `judge.evaluate_full(
  summary, atv=atv, inp=inp)` so M13-style judges get the structured
  signal. Backward compatible (legacy judges fall back to `evaluate`).
* **`src/aegis/config.py`** — `aegis_judge_provider` Literal extended
  with `attribution_head`, `hybrid`. New env vars:
  `AEGIS_JUDGE_MODEL_PATH`, `AEGIS_JUDGE_LOCAL_PHI_STUB`.

### Demo

* **`demo/judge_stack.py`** (new) — runs the same 5 canonical tool
  calls through both M13 alone and the v3.0 hybrid stack. Prints
  per-tier decision / confidence / latency + final verdict + reason.
  Live verified: every scenario decides at M13 (Tier 1) with
  cumulative latency <1 ms.

### Tests

* +56 unit tests (849 → 905 total). Coverage:
  * Attribution head (21): weights file SHA3, model_hash determinism,
    text fallback, evaluate_full populates 30-key map, latency, blast
    discrimination, destructive-arg → top contributor, HW anomaly →
    HW subfields in top-3, innocent read → ALLOW < 0.40, score clamping.
  * LocalPhiJudge (19, 1 skipped): mode detection (stub default,
    explicit stub, missing model → disabled), real-file SHA3 hash,
    stub block on destructive args + allow on innocent read, text-only
    fallback, deterministic same-input, _parse_real_decode JSON +
    keyword fallback + unparseable → ALLOW.
  * HybridJudge (16): default-layer construction with/without Anthropic
    key, BLOCK short-circuits, high-confidence ALLOW commits, low-
    confidence ALLOW escalates, REQUIRE_APPROVAL commits, fall-through
    to last tier, layer_traces / model_hash / cumulative latency, real
    default stack catches `rm -rf`, deterministic same-input.

### Verified gates

* `pytest -q`     → **905 passed** + 1 skipped (was 849).
* `mypy src`      → clean, **89 source files** (was 86).
* `ruff check .`  → clean.
* Live demo: 5 / 5 scenarios decided at Tier 1 in <1 ms aggregate.

### Migration from v2.4.x

No breaking changes. `aegis_judge_provider` defaults to `dummy` so the
existing surface is unchanged. To opt in to ATV-native judging:

```bash
export AEGIS_JUDGE_PROVIDER=attribution_head    # M13 only, fastest
# or
export AEGIS_JUDGE_PROVIDER=hybrid               # full stack with fallback
# Optional: real Phi-4-mini-q4
export AEGIS_JUDGE_MODEL_PATH=/path/to/phi-4-mini-q4.gguf
uv pip install llama-cpp-python
```

### What is NOT done

* Real Phi-4-mini-q4 model file is **not bundled** — multi-GB GGUF
  files don't fit in the repo. Stub mode covers the contract; real
  mode activates when the user downloads the model.
* M13 weights are **hand-tuned**, not learned. v3.x will replace with
  weights trained from labelled (ATV, verdict) pairs collected via
  the Burn-in Shadow phase (M11).
* Cross-hardware quantized determinism (Apple Metal vs CUDA vs CPU)
  is "attestable per (model, backend, hw)" — addressed by storing
  backend hash alongside `model_hash` in v3.x.

---

## [2.4.0] — 2026-04-28  ·  step337 HW band anomaly gate

Closes the gap surfaced by v2.3's demo (3 / 6 attacks unblocked).
Adds a new firewall step that reads the ATV HW band's normalized
signals and converts clear-cut anomalies into BLOCK / REQUIRE_APPROVAL
— complementing the M12 cost-divergence escalation (Claim 27) which
only watches the j-14/j-15/j-16 cost axis. Together M12 + step337
catch all 6 simulator attack modes.

### Added — `step337_hw_anomaly`

* **`src/aegis/firewall/step337_hw_anomaly.py`** — new firewall step
  reading the ATV HW band directly:

  Severity 1 (BLOCK):
  * ``aid_tag_transitions[0]`` ≥ 0.20 → ``rule:hw_iommu_violation``
    (IOMMU AID-tag breach, Claim 5 enforcement target).
  * ``atmu_anomaly[2]``        ≥ 0.34 → ``rule:hw_hypervisor_violation``
    (VM-to-host ring violation).
  * ``network_telemetry[0]``   ≥ 0.20 AND tool ∉ ``_EGRESS_ALLOWED``
    → ``rule:hw_network_exfil`` (≥10 MB egress on a non-egress tool).

  Severity 2 (REQUIRE_APPROVAL):
  * ``thermal_ecc_drift[3]`` == 1.0 → ``rule:hw_thermal_spike``
    (sustained ≥90°C p95).
  * ``dma_fanout[0]``        ≥ 0.50 → ``rule:hw_dma_fanout``
    (≥16 distinct DMA peers in one call).
  * ``atmu_anomaly[3]``      ≥ 1.0 → ``rule:hw_ecc_uncorrectable``
    (uncorrectable ECC error — possible rowhammer probe).

  ``_EGRESS_ALLOWED`` covers ``WebFetch``, ``WebSearch``, ``fetch``,
  ``http_request``, ``curl``, ``browse``, ``render_image``,
  ``embed_image``, ``send_email``, ``post_message``,
  ``call_external_api`` so step337 doesn't BLOCK the legitimate
  outbound use cases (step311's ``exfil_url`` rule still inspects
  the URL itself).

  Step short-circuits to no-op when the HW band is zero-filled
  (T2 default + sidecar without ``AEGIS_HW_PROVIDER=sim``), so the
  v2.0 / v2.2 / v2.3 surface is unaffected.

### Changed

* **`src/aegis/firewall/core.py`** — `default_steps()` inserts
  step337 between step336 (loop) and step340 (sLLM judge).
* **`_donor/smoke_hw_double_check.py`** — demo now runs both gates
  (M12 escalation + step337) and prints a 7-column matrix. All 6
  attack modes light up:

  ```
   scenario                      tok→FLOPS  mem    $    M12     s337   rule
   ──────────────────────────── ────────── ───── ──── ──────── ───── ───────────────
   ✓ honest agent (no attack)        0.05  0.08 0.05      ok    ok    -
   ✗ attack: cost_underreport         1.00  0.08 1.00 ESCALATE  ok    token_to_flops
   ✗ attack: hbm_exfil                0.05  1.00 0.05 ESCALATE  ok    memory_cost
   ✗ attack: iommu_violation          0.05  0.08 0.05      ok BLOCK   rule:hw_iommu_violation
   ✗ attack: network_exfil            0.05  0.08 0.05      ok BLOCK   rule:hw_network_exfil
   ✗ attack: thermal_spike            0.05  0.08 0.05      ok   ASK   rule:hw_thermal_spike
   ✗ attack: token_flops_mismatch     1.00  0.08 1.00 ESCALATE  ok    token_to_flops
   ✗ multi: token+network+iommu       1.00  0.08 1.00 ESCALATE BLOCK  token_to_flops
  ```

### Tests

* +20 unit tests (829 → **849** total). Coverage: T2 zero-band
  short-circuit, honest-path no-op, each severity-1 and severity-2
  rule fires individually, egress-allowed tools exempt from
  ``hw_network_exfil`` (parametrised across 6 tools), priority
  ordering (BLOCK before REQUIRE_APPROVAL, IOMMU before thermal),
  threshold edge case (0.19 just below 0.20 doesn't fire),
  ``ctx.extras`` audit hint contract, end-to-end through
  `run_firewall` (step337 BLOCK propagates to Verdict).

### Verified gates

* `pytest -q`     → **849 passed** (was 829).
* `mypy src`      → clean, **86 source files** (was 85).
* `ruff check .`  → clean.
* Live demo: every attack mode catches at one of two gates.

### Migration from v2.3.x

No breaking change. step337 is a no-op when the HW band is
zero-filled, which is the default unless ``AEGIS_HW_PROVIDER=sim``
is set. The user already-on-sim path picks up the new gate
automatically — no env var changes needed.

---

## [2.3.0] — 2026-04-27  ·  T3 hardware-emulation: SW/HW double-check live

Brings the patent's two-axis double-check model (Claims 26 / 27 / 30 / 34)
on-line *without* needing real T3 silicon (M19 RAPL/NVML, M20 FPGA, M21
IOMMU, M22 CSD all stay deferred). Adds a deterministic SW emulator
that populates the 200-D ATV HW band so the M12 cost-divergence
escalation gate becomes a working defence layer instead of a 0÷0
placeholder.

### Added — HW telemetry simulator

* **`src/aegis/hw_telemetry/`** (new package):
  * `simulator.py` — `HWCounters` dataclass + `simulate(inp, attack="")`
    function. Deterministic via SHA3-seeded `_seeded_floats(...)`
    keyed on (tool, args, aid). Honest path = SW-expected baseline
    (`expected_flops` from `cost.model_flops`) jittered ±10% so all
    three j-14 / j-15 / j-16 divergence metrics stay <0.30 escalation
    threshold.
  * `simulate_from_env(inp)` — env-driven on/off + attack injection.
    `AEGIS_HW_PROVIDER=sim` switches on; `AEGIS_HW_INJECT_ATTACK=...`
    rewrites counters to make the gate fire.
  * Six attack modes (the SW emulation of compromised silicon):
    * `token_flops_mismatch` — HW FLOPS = 50× SW expected → catches
      model substitution / hidden background work.
    * `hbm_exfil` — HBM bytes = 30× SW expected → side-channel via
      KV cache / activations.
    * `cost_underreport` — HW FLOPS = 100× while SW dollar stays the
      same → billing-evasion compromise.
    * `thermal_spike` — sustained 95°C p95 + ECC spikes.
    * `network_exfil` — 50 MB egress on a "read_file" call.
    * `iommu_violation` — IOMMU AID-tag breaches (Claim 5
      enforcement target).
* **`src/aegis/atv/hw_encoders.py`** (new) — 11 deterministic
  projections from `HWCounters` to ATV HW band subfields:
  `memory_timing_histograms` (32-D), `aid_tag_transitions` (24-D),
  `atmu_anomaly` (16-D), `dma_fanout` (16-D), `thermal_ecc_drift`
  (16-D), `watchdog_signals` (12-D), `network_telemetry` (24-D),
  `gpu_accelerator_state` (16-D), `hypervisor_signals` (8-D),
  `hw_cost_attestation` (16-D), `linkage_consistency` (20-D).
  Per Claim 26, slots 13/14/15 of `hw_cost_attestation` carry the
  j-14/j-15/j-16 divergence values directly so the cryptographic
  audit record is self-attesting.

### Changed

* **`src/aegis/atv/builder.py`** — `build_atv(inp, *, hw=None)` accepts
  an optional `HWCounters`. When absent (default) the HW band stays
  zero-filled (T2 contract); when present, the encoders fire and the
  HW band carries real signal.
* **`src/aegis/api/evaluate.py`** — calls `simulate_from_env(inp)`
  early, threads the result into both `build_atv(inp, hw=...)` and
  `compute_divergence(..., hw_flops_observed=hw.flops_observed,
  hw_hbm_bytes_observed=hw.hbm_bytes_observed)`. Existing M12
  escalation gate (Claim 27) now lights up under attack injection
  without any code change to the firewall pipeline.
* **`src/aegis/config.py`** — two new settings (default `none` /
  empty string so the v2.0/v2.1/v2.2 surface is unaffected):
  `aegis_hw_provider: Literal["none", "sim"] = "none"` and
  `aegis_hw_inject_attack: str = ""`.

### Tests

* +30 unit tests (792 + v2.2.1's 7 → 829 total). Coverage: simulator
  determinism, aid-isolated jitter, every attack mode produces an
  observable counter change, three "guaranteed escalation" attacks
  trigger the M12 gate, env-var on/off + attack pickup, every
  encoder's shape + clamp + slot semantics, builder integration with
  HW-cost-attestation slot 13 numerically equal to compute_divergence
  output.

### Demo

* `_donor/smoke_hw_double_check.py` — runs the same synthetic SW
  request through honest + each of 6 attack modes + a multi-attack
  combination, prints the `(token_to_flops, memory, dollar, gate,
  metric)` matrix. 3 / 6 attack modes (`token_flops_mismatch`,
  `hbm_exfil`, `cost_underreport`) trip the M12 cost-axis gate; the
  other 3 are visible in the ATV HW band (subfields populated as
  expected) but require their own firewall step for BLOCK conversion
  — clean roadmap for a future step `step337_hw_anomaly`.

### What is still NOT done

This is **SW emulation, not real T3**. The hardware procurement
roadmap is unchanged:

* M18 ML-DSA dual-signing (oqs-python) — pure SW, can land any time.
* M19 RAPL/NVML — needs Linux server + GPU.
* M20 FPGA sLLM — Xilinx Versal AI Edge VEK280.
* M21 HW tag comparator — bare-metal IOMMU.
* M22 CSD — Solidigm D7-PS1010 eval kit.

The simulator's `HWCounters` envelope matches the data shape M19–M22
will deliver, so the wire from `evaluate.py` → `compute_divergence`
→ M12 ledger is *already correct*. Replacing `simulate(...)` with a
real driver per provider is a one-file swap when silicon shows up.

### Verified gates

* `pytest -q`                                       **829 passed**
                                                     (was 799).
* `mypy src` — clean, **85 source files** (was 82).
* `ruff check .` — clean.
* HW band non-zero in audit records when `AEGIS_HW_PROVIDER=sim`.
* M12 escalation flips ALLOW → REQUIRE_APPROVAL on attack injection
  (verified live by `_donor/smoke_hw_double_check.py`).

### Migration from v2.2.x

No breaking change. Sidecar service installs continue to use HW
band = 0 unless `AEGIS_HW_PROVIDER=sim` is set in their environment.
For demos / dogfood:

```bash
docker compose down
echo 'AEGIS_HW_PROVIDER=sim' >> .env
echo 'AEGIS_HW_INJECT_ATTACK=token_flops_mismatch' >> .env  # optional
docker compose up -d
```

After this, every `/evaluate` request gets a populated HW band and
divergence-triggered REQUIRE_APPROVAL on the chosen attack mode.

---

## [2.2.0] — 2026-04-27  ·  must-install: Safe Auto-Run + Poisoned Instruction Detector

This release closes the "must-install" gap from the v2.0 strategy
review. Five v2.1 features (Safe Auto-Run, cloud destructive rules,
Loop Saver, Risk Report, local signed audit) plus the v2.2 Poisoned
Instruction Detector turn the sidecar / plugin into the
**"Aegis Guard makes Claude Code & Codex safe enough to run
unattended"** product.

### Added — v2.1 Safe Auto-Run + Cost saver + visibility

* **v2.1.1 Safe action allowlist** — new `step305_safe_allowlist`
  runs first in the pipeline. Curated `policies/safe_actions.json`
  flags read-only file tools (Read / Grep / Glob, ``any_args``) and
  60 bash subcommand prefixes (file inspection, formatters, test
  runners, read-only git) as ``ctx.extras["safe_fast_path"] = True``.
  step340 honors the flag and skips the sLLM judge round-trip,
  dropping median latency from ~150 ms (Haiku) to <5 ms.
  Disqualifying shell metachars (``|``, ``;``, ``&&``, ``>``, ``$()``,
  backticks) immediately revert the call to the full pipeline so a
  destructive subshell never papers over a safe leading verb.
* **v2.1.2 step311 cloud + sql_unbounded patterns** — kubectl
  delete / drain, terraform destroy / apply -auto-approve / state rm,
  aws s3 rm / iam delete-user / iam create-access-key / ec2
  terminate-instances / rds delete-db-*, gcloud iam roles | service-
  accounts delete + iam service-accounts keys create + compute | sql
  | kms ... delete + projects delete / remove-iam-policy-binding, az
  role assignment create | delete + vm | sql | storage | keyvault
  delete, helm uninstall | delete, docker rmi -f | system prune -a |
  volume rm. Plus DELETE / UPDATE without WHERE on sql-class tools
  (incl. bash-tunneled ``psql -c "DELETE FROM logs"``).
* **v2.1.3 Loop & Redundant Call Saver** — new
  `aegis.monitor.loop_detector` (per-session, lock-protected SHA3
  counter) + `step336_loop`. Loop = same (tool, args_hash) repeated
  ≥ 3 times → REQUIRE_APPROVAL. Redundant = read-only repeat within
  300 s window → ALLOW + ``ctx.extras["redundant"] = True`` so the
  risk report can later count "N redundant calls deduped".
* **v2.1.4 ``aegis report``** — 5-line Agent Risk Report that reads
  the local audit JSONL and bins by decision + reason:

  ```
  ✅  N safe tool calls auto-approved
  ⚠️   K high-risk actions required approval
  ⛔  B destructive commands blocked
  ⛔  P poisoned-instruction sources detected
  💸  D redundant calls deduplicated
  🔁  L potential loops aborted
  🧾  Full signed local audit: <path>
  ```

  ``--since 24h`` filters by ts_ns; ``--verbose`` adds a top-10
  reason × count table.
* **v2.1.5 Local-mode SHA3 audit chain** — every line in
  ``~/.aegis/audit.jsonl`` now carries ``prev_hash`` + ``this_hash``
  so any post-write mutation breaks every subsequent recompute.
  ``aegis verify-audit`` walks the chain end-to-end and reports the
  first broken record. Sidecar mode is unchanged (M5/M9/M15 Ed25519
  + Merkle + AES-GCM remain canonical there).

### Added — v2.2 Poisoned Instruction Detector

* **`src/aegis/instruction_baseline/`** — captures SHA3-256 hashes
  of CLAUDE.md, AGENTS.md, .mcp.json, .claude-plugin/plugin.json,
  .claude/skills/*.md, .claude/commands/*.md, .cursor/rules/*.mdc.
  ``snapshot``, ``diff_baseline``, ``write/load_baseline`` are pure
  stdlib; ``DriftReport(added, removed, modified)`` is the contract.
* **`step309_instruction_drift`** — sits after step305, before
  step310. Re-hashes on every PreToolUse and BLOCKs on any drift
  with reason ``instruction_drift: <summary> (<top-3-files>)``.
  Disabled by default (settings.aegis_instruction_baseline_path = ""
  → no-op) so existing sidecar tests pass unchanged.
* **`aegis baseline {init|status|reattest}`** — repo-local manifest
  management. Default path is ``.aegis/instruction_baseline.json``
  under the repo root. ``init`` refuses to overwrite without
  ``--force``; ``status`` exits 1 on drift with per-file diff;
  ``reattest`` overwrites and drops the firewall's in-process cache.

### Changed

* `src/aegis/firewall/core.py` `default_steps()` is now a 10-step
  pipeline:

  ```
  step305_safe_allowlist  (v2.1.1)
  step309_instruction_drift  (v2.2)
  step310_args
  step311_donor_rules  (D11 + v2.1.2 cloud)
  step312_normalize
  step315_aid_auth
  step320_blast
  step330_human
  step335_cost
  step336_loop  (v2.1.3)
  step340_policy  (skips judge when safe_fast_path is set)
  ```

* `tests/conftest.py` `aegis_app` fixture resets the module-level
  default loop detector before and after each test so cross-test
  bleeds (the existing burnin e2e re-posts the same call 5×) don't
  trigger spurious loop verdicts.

### Tests

* +142 unit tests (Phase 0 baseline 455 → v2.0.0 650 → **v2.2.0 792**).
  Coverage: 23 step305, 38 step311 cloud rules, 22 loop detector +
  step336, 7 ``aegis report``, 17 local audit chain + verify-audit, 16
  instruction baseline, 8 step309, 9 ``aegis baseline``.

### Verified gates

* `pytest -q`                                       **792 passed**.
* `mypy src` — clean, **82 source files**.
* `ruff check .` — clean.

### Migration from v2.0.x

No breaking changes for sidecar mode — step305 / step309 / step336
are no-op when disabled, and the new policies/safe_actions.json is
purely additive. To opt into the new surface in your install:

```bash
# v2.1 features ship enabled (safe allowlist + loop detector run by default).
# v2.2 baseline is opt-in:
uv run aegis baseline init                         # write the manifest
export AEGIS_INSTRUCTION_BASELINE_PATH=$(pwd)/.aegis/instruction_baseline.json
# Restart the service / Claude Code.
```

---

## [2.0.0] — 2026-04-26  ·  aegis-mvp plugin merged into T2 sidecar

This release merges the `aegis-mvp v1.0.0` Claude Code plugin (142
files, 62 tests) into the existing AegisData T2 sidecar (M1–M17, 455
tests). The result is a **single codebase, two deployment modes**,
sharing one ATV / ATMU (Agent Telemetry Management Unit) / Burn-in core:

* **Sidecar mode** (default) — multi-tenant FastAPI; the host hook
  POSTs to ``localhost:8000/evaluate``. Audit signing, cost ledger,
  HAM and Burn-in are the full M1–M17 surface.
* **Plugin (`local`) mode** (new) — single-developer in-process hook;
  no service, no HTTP, no API keys. Solo Free tier.

### Added — plugin surface (D1–D6)

* **D1** — `tools/aegis_payload.py`: Claude Code ↔ ``/evaluate``
  payload adapter. Normalises both Claude Code's ``PreToolUse`` shape
  (``session_id`` / ``tool_name`` / ``tool_input``) and the legacy
  ``{tool, args, agent_id}`` shape; maps internal verdicts
  (``allow`` / ``block`` / ``require_approval``) onto Claude Code's
  ``hookSpecificOutput.permissionDecision`` (``allow`` / ``deny`` /
  ``ask``).
* **D2** — `.claude-plugin/plugin.json` v2.0.0 manifest (PreToolUse +
  PostToolUse + Stop hooks, six sprint-N-kickoff slash commands, the
  ``aegis-mvp`` skill, and the ``tier`` / ``policy_pack`` /
  ``burnin_baseline`` / ``sllm_endpoint`` config schema).
* **D3** — `tools/aegis_cli.py`: ``aegis`` CLI with 14 subcommands
  (``status`` / ``verify-audit`` / ``replay`` / ``policy-replay`` /
  ``cost`` / ``health`` / ``rollback`` / ``snapshots`` / ``burnin`` /
  ``cost-record`` / ``cost-import`` / ``budget`` / ``install``).
  Promoted ``tools/`` to a wheel package and added
  ``[project.scripts] aegis = "tools.aegis_cli:main"`` so
  ``uv run aegis install`` works after a fresh ``uv sync``. Absorbs
  the safety properties of the legacy ``tools/install_hook.py``.
* **D4** — `src/aegis/rollback/` + four strategies (file / shell /
  git / mcp). Pre-tool snapshot captures filesystem + git state so
  ``aegis rollback INVOCATION_ID`` can restore. Bulk restore via
  ``--session SID`` or ``--since ISO``.
* **D5** — `src/aegis/cost/transcript.py`: Claude Code transcript
  ``.jsonl`` parser. ``parse_transcript`` is pure;
  ``import_into_wal`` calls a pluggable ``ledger_writer`` hook
  (defaults to a parse-only no-op so no OPENAI/ANTHROPIC key is
  required — Phase 5 packaging rebinds it to the M12
  CostAttestationLedger).
* **D6** — `tools/hooks/session_end.py`: Claude Code Stop-event hook
  that auto-imports transcript cost data through D5 when a session
  ends.

### Added — ATV-2080 adapter (Phase 3)

* **`src/aegis/atv/adapter.py`** — `from_claude_code_payload(req, *,
  tenant_id, role_id, agent_state_text, plan_text) -> ATVInput`.
  Bridges the plugin payload shape into MVP/'s 30-subfield
  ATV-2080-v1 so the same ``/evaluate`` endpoint serves both modes.
  Trace IDs derived from invocation_id via SHA3-256 so re-evaluating
  the same call yields the same audit anchor.
* `donor_behavior_features(tool, args)` preserves the donor's 32-D
  hand-engineered feature vector verbatim for callers that want
  deterministic donor-style features.

### Added — donor pattern rule pack (D11, partial)

* **`src/aegis/firewall/step311_donor_rules.py`** — new firewall
  stage between step310 and step312, ports seven stdlib pattern
  rules from `_donor/aegis-mvp/atmu/rules/` that close the eight
  Phase 3 e2e gap incidents:
  * `persona_drift`     I-01  REQUIRE_APPROVAL — system-prompt
    extraction patterns ("repeat your system prompt").
  * `exfil_url`         I-04 / I-07  BLOCK — base64 / hex / long-query
    URL blobs and suspicious TLDs (`.tk` `.ml` `.ga` `.cf` `.gq`
    `.pw` `.top`) on egress tools (fetch / render_image / send_email).
  * `sandbox_escape`    I-06  BLOCK — `docker.sock`,
    `docker run --privileged`, `--cap-add=SYS_ADMIN`, `nsenter`,
    `mount --bind /`.
  * `prompt_injection`  I-08  REQUIRE_APPROVAL — "ignore previous
    instructions" patterns on input-bearing tools (fetch / read_file
    / search / rag_query / browse / read_page).
  * `mcp_injection`     I-09  BLOCK — instruction patterns inside
    newly-registered MCP tool descriptions.
  * `git_destructive`   I-10  BLOCK — `git push --force` to
    main / master / prod, `git branch -D main`, `git rebase main`.
  * `payment_overflow`  I-11  BLOCK — per-tool USD ceilings:
    stripe_charge ≥$1k, wire_transfer ≥$10k, ach_payment ≥$5k,
    crypto_send ≥$500, payout ≥$5k.
* `cost_overflow` and `malfunction_pattern` rules deferred to v2.1
  (depend on D7 ``monitor.malfunction`` and D10 ``cost.budget``,
  not yet ported).

### Added — plugin packaging (Phase 5)

* **`aegis install --mode {sidecar,local}`**:
  * `--mode sidecar` (default) — registers ``tools/aegis_hook.py`` so
    the hook POSTs to ``localhost:8000/evaluate``. Requires
    ``docker compose up -d``.
  * `--mode local` — registers ``tools/aegis_local_hook.py`` so the
    firewall pipeline runs in-process. Auto-prepends
    ``AEGIS_EMBEDDING_PROVIDER=dummy``,
    ``AEGIS_JUDGE_PROVIDER=dummy``, ``AEGIS_POLICY_DIR=…`` and
    ``PYTHONPATH=…`` so the spawned subprocess works without any
    OpenAI / Anthropic key (Solo Free contract per CLAUDE.md
    "Dummy/Mock Mode").
* **Plugin manifest validation** before install — refuses if
  ``.claude-plugin/plugin.json`` is missing, malformed, or lacks
  ``name`` / ``version``.
* **Stop hook auto-registration** alongside PreToolUse, idempotently
  across modes; sidecar + local entries can coexist (different
  markers).
* **Legacy migration banner** — when an ``install_hook.py`` entry is
  detected in the user's settings, prints a yellow note pointing at
  the new CLI but leaves the legacy line in place (preserves v1.x
  compatibility).

### Added — tests

* **+195 tests** (Phase 0 baseline 455 → 650).
  * Plugin / CLI: payload adapter (9), ``aegis`` CLI argparse +
    install (51), Stop hook (6), local hook smoke (11).
  * Rollback: 4 strategies + snapshot orchestrator (30).
  * Cost transcript parser (10).
  * ATV adapter + donor encoder features (27).
  * Donor rule pack (37).
  * 12-incident e2e through real ``/evaluate`` (14, 12 strict pass).

### Changed

* `src/aegis/firewall/core.py` — `default_steps()` now inserts
  `step311_donor_rules.run` between step310 and step312.
* `src/aegis/cost/__init__.py` — re-exports `parse_transcript` and
  `import_into_wal`.
* `pyproject.toml`:
  * `tools/` promoted to a hatch wheel package.
  * `[project.scripts] aegis = "tools.aegis_cli:main"` entry point.
* `INTEGRATION_PLAN.md` committed at the start of the merge as the
  living plan.

### Migration from v1.x

Existing `tools/install_hook.py` users can keep using it; the new
``aegis install`` CLI lands its own PreToolUse entry alongside the
legacy one and prints a yellow banner. To switch:

```bash
# 1. Pull v2.0
git pull && uv sync

# 2. Re-install with the new CLI
uv run aegis install --mode sidecar    # multi-tenant default
# or
uv run aegis install --mode local      # Solo Free, no service

# 3. (Optional) Remove the legacy install_hook.py entry from
#    ~/.claude/settings.json by hand.

# 4. Restart Claude Code.
```

### Verified end-to-end

* `pytest -q`                                         **650 passed**.
* `mypy src` — clean, **74 source files**.
* `ruff check .` — clean.
* `bash demo/scenarios/run_all.sh` — **7/7 PASS** in 68s.
* `/evaluate` against the 12-incident donor KPI panel —
  **12/12 strict** (4 via existing MVP rules, 8 via step311 D11).

### Deferred to v2.1

* D7 `src/aegis/monitor/malfunction.py` — runtime malfunction
  classifier (per-session error_rate / atv_loop / schema_drift).
* D8 `src/aegis/burnin/retrain.py` — sanity-check + revert wrapper
  around the M11 5-layer Burn-in baseline.
* D9 `src/aegis/api/replay.py` extension — policy-replay engine
  on top of the existing ``/forensic/replay`` endpoint.
* D10 `src/aegis/cost/budget.py` — hot-reloadable budget thresholds.
* `cost_overflow` and `malfunction_pattern` rules in step311 (depend
  on D10 / D7 above).
* `aegis status` / `aegis health` / `aegis policy-replay` /
  `aegis budget` / `aegis cost` — depend on D7–D10 backings; the
  CLI subcommands ship as lazy-imported stubs.

---

## [1.x] — pre-v2.0

The full pre-v2.0 milestone history (M1 FastAPI through M17 TEE
attestation, plus DOGFOOD Phase A/B and the 49-page WHITEPAPER) lives
in `git log` and `SESSION_HANDOFF.md` §4. This file covers v2.0
forward.
