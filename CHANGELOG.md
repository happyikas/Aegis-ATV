# Changelog

All notable changes to AegisData MVP. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
this project adheres to [Semantic Versioning](https://semver.org/).

## [2.0.0] — 2026-04-26  ·  aegis-mvp plugin merged into T2 sidecar

This release merges the `aegis-mvp v1.0.0` Claude Code plugin (142
files, 62 tests) into the existing AegisData T2 sidecar (M1–M17, 455
tests). The result is a **single codebase, two deployment modes**,
sharing one ATV / ATMU / Burn-in core:

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
