"""``aegis`` CLI v2 — operator utility (D3).

Donor: aegis-mvp v1.0.0 ``claude_hooks/cli.py``.

Subcommands::

    aegis status            Plugin status, KPIs, latest anchor
    aegis verify-audit      Verify Merkle chain + signatures in WAL
    aegis replay [N]        Show last N intents (audit mode)
    aegis policy-replay     Re-evaluate past intents under new/current policy
    aegis cost [--days N]   Cost rollup (day/agent breakdown)
    aegis health            Malfunction signal report
    aegis rollback ID       Restore filesystem (and optionally git) snapshot
    aegis snapshots         List or prune recent snapshots
    aegis burnin retrain    Retrain Burn-in baseline (sanity-check + revert)
    aegis cost-record       Manually record token usage for an invocation
    aegis cost-import       Backfill cost from transcript or Admin API
    aegis budget            Show or set budget limits
    aegis install           Install hooks into ~/.claude/settings.json

The ``install`` subcommand absorbs ``tools/install_hook.py``: it backs up
any existing ``settings.json``, is idempotent (re-runs are safe), and
points PreToolUse at this repo's ``tools/aegis_hook.py``. The remaining
subcommands import their backing modules lazily — D4/D5/D7/D8/D10 wire
those in with subsequent commits, so e.g. ``aegis status`` raises
``ImportError`` until those modules land. ``aegis install`` is fully
operational from D3.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import signal
import sqlite3
import sys
import time
from datetime import UTC
from pathlib import Path
from typing import Any

DB = Path(".aegis/wal.db")
HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent
MODELS_DIR = PROJECT_ROOT / "models"
HOOK_SCRIPT = HERE / "aegis_hook.py"               # sidecar mode (POST /evaluate)
LOCAL_HOOK_SCRIPT = HERE / "aegis_local_hook.py"   # local mode (in-process)
POST_HOOK_SCRIPT = HERE / "hooks" / "post_tool.py"
STOP_HOOK_SCRIPT = HERE / "hooks" / "session_end.py"
PRECOMPACT_HOOK_SCRIPT = HERE / "hooks" / "pre_compact.py"
USER_PROMPT_HOOK_SCRIPT = HERE / "hooks" / "user_prompt_submit.py"
SESSION_START_HOOK_SCRIPT = HERE / "hooks" / "session_start.py"
PLUGIN_MANIFEST = PROJECT_ROOT / ".claude-plugin" / "plugin.json"
POLICIES_DIR = PROJECT_ROOT / "policies"
SRC_DIR = PROJECT_ROOT / "src"
SETTINGS_PATH = Path.home() / ".claude" / "settings.json"

# Sprint 1 PR3 — Claude Code custom slash commands.
# Source: tools/claude_commands/*.md (templates with {AEGIS_CMD} placeholder).
# Dest:   ~/.claude/commands/aegis-*.md (rendered at install time).
SLASH_COMMANDS_SRC = HERE / "claude_commands"
SLASH_COMMANDS_DST = Path.home() / ".claude" / "commands"
# Marker so uninstall removes only what we installed (preserves user-owned
# files in the same directory if any).
SLASH_COMMANDS_MARKER = "<!-- aegis-managed-slash-command -->"


def _conn() -> sqlite3.Connection:
    if not DB.exists():
        print(f"[aegis] no WAL found at {DB}. Run something first.")
        sys.exit(2)
    return sqlite3.connect(DB)


def _green(s: str) -> str:
    return f"\033[32m{s}\033[0m"


def _yellow(s: str) -> str:
    return f"\033[33m{s}\033[0m"


def _red(s: str) -> str:
    return f"\033[31m{s}\033[0m"


def cmd_status(args: argparse.Namespace) -> int:
    """Plugin-mode status: audit chain + ATMU intent log + LLM daemon.

    Reads only modules that actually exist in the local-mode build —
    audit JSONL (PreToolUse + PostToolUse records), ``IntentLog``
    (M10 ATMU 2PC state), and the LLM keep-alive daemon (PR #30).
    Cost / malfunction-classifier / blockchain-anchor sections are
    deferred (D7/D10 + post-launch) and surface as a single line so
    operators see what's missing rather than guessing from a crash.

    With ``--performance``: appends a performance dashboard
    aggregating Stop-hook session retrospectives (PR #46) and
    related multi-hook signals (PR #45 / #47).
    """

    audit_path = Path(
        os.environ.get(
            "AEGIS_LOCAL_AUDIT", str(Path.home() / ".aegis" / "audit.jsonl")
        )
    )
    decisions: dict[str, int] = {"ALLOW": 0, "BLOCK": 0, "REQUIRE_APPROVAL": 0}
    outcomes: dict[str, int] = {
        "success": 0, "failure": 0, "timeout": 0, "partial": 0,
    }
    n_pre = n_post = 0

    if audit_path.exists():
        with audit_path.open("r", encoding="utf-8") as fh:
            for raw_line in fh:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("hook") == "PostToolUse":
                    n_post += 1
                    s = str(rec.get("status", ""))
                    if s in outcomes:
                        outcomes[s] += 1
                elif "decision" in rec:
                    n_pre += 1
                    d = str(rec["decision"])
                    if d in decisions:
                        decisions[d] += 1

    chain_ok, broken_at, chain_total = True, -1, 0
    if audit_path.exists():
        try:
            from aegis.audit.local_chain import verify_chain

            chain_ok, broken_at, chain_total = verify_chain(audit_path)
        except Exception:  # noqa: BLE001
            pass

    intent_db = Path(
        os.environ.get(
            "AEGIS_INTENT_LOG_DB",
            str(Path.home() / ".aegis" / "intent_log.sqlite"),
        )
    )
    atmu_counts: dict[str, int] = {}
    if intent_db.exists():
        try:
            from aegis.atmu import IntentLog, TxState

            log = IntentLog(str(intent_db))
            try:
                for s in TxState:
                    atmu_counts[s.value] = log.count_state(s)
            finally:
                log.close()
        except Exception:  # noqa: BLE001
            pass

    daemon_status = "stopped"
    daemon_model: str | None = None
    try:
        from aegis.judge.llm_daemon import DaemonClient

        client = DaemonClient()
        if client.is_running():
            daemon_status = "running"
            ping = client.ping(timeout_s=1.0)
            if ping:
                daemon_model = (
                    ping.get("model_name") or ping.get("model_path") or None
                )
    except Exception:  # noqa: BLE001
        pass

    print("AegisData status (plugin mode)")
    print("==============================")
    chain_label = (
        _green("OK") if chain_ok else _red(f"BROKEN @ idx {broken_at}")
    )
    print(f"  audit chain:  {chain_total:>6,} records   {chain_label}")
    print(f"                {audit_path}")
    print()
    print(
        f"  PreToolUse:   {n_pre:>6,}   "
        f"ALLOW {decisions['ALLOW']:,}  "
        f"BLOCK {decisions['BLOCK']:,}  "
        f"ASK {decisions['REQUIRE_APPROVAL']:,}"
    )
    print(
        f"  PostToolUse:  {n_post:>6,}   "
        f"ok {outcomes['success']:,}  "
        f"fail {outcomes['failure']:,}  "
        f"timeout {outcomes['timeout']:,}  "
        f"partial {outcomes['partial']:,}"
    )
    print()
    if atmu_counts:
        total = sum(atmu_counts.values())
        committed = atmu_counts.get("committed", 0)
        aborted = atmu_counts.get("aborted", 0)
        prepared = atmu_counts.get("prepared", 0)
        tentative = atmu_counts.get("tentative", 0)
        print(
            f"  ATMU intents: {total:>6,}   "
            f"committed {committed:,}  aborted {aborted:,}  "
            f"prepared {prepared:,}  tentative {tentative:,}"
        )
    else:
        print(
            f"  ATMU intents: {_yellow('no DB yet — runs after first PreToolUse')}"
        )
    print(f"                {intent_db}")
    print()
    if daemon_model:
        print(f"  sLLM daemon:  {_green(daemon_status)}   ({daemon_model})")
    else:
        print(
            f"  sLLM daemon:  "
            f"{_green(daemon_status) if daemon_status == 'running' else _yellow(daemon_status)}"
        )
    print()
    print(
        f"  cost / health: {_yellow('(D7 / D10 deferred — not tracked in plugin mode)')}"
    )

    # ── Optional performance dashboard ─────────────────────────────
    if getattr(args, "performance", False):
        from aegis.performance.dashboard import (
            build_performance_summary,
            redact_summary,
            summary_to_dict,
        )
        summary = build_performance_summary(audit_path)
        if getattr(args, "redact", False):
            summary = redact_summary(summary)

        if getattr(args, "json", False):
            print()
            print(json.dumps(summary_to_dict(summary), indent=2))
            return 0

        _render_performance_dashboard(summary)

    return 0


def _render_performance_dashboard(summary: Any) -> None:
    """Human-readable rendering of a :class:`PerformanceSummary`.

    Lives next to ``cmd_status`` because the dashboard is its
    optional tail-section; keeping the format inline avoids
    coupling ``aegis.performance.dashboard`` to terminal colour
    helpers it doesn't otherwise need.
    """
    print()
    print("Performance Dashboard")
    print("=====================")
    if summary.n_records_walked == 0:
        print(
            f"  {_yellow('audit chain empty / unreadable — nothing to aggregate')}"
        )
        return
    print(
        f"  records walked:        {summary.n_records_walked:>10,}"
    )
    print(
        f"  sessions (Stop hook):  {summary.n_sessions:>10,}"
    )
    if summary.earliest_session_ts_ns and summary.latest_session_ts_ns:
        import datetime as _dt
        earliest = _dt.datetime.fromtimestamp(
            summary.earliest_session_ts_ns / 1e9
        ).strftime("%Y-%m-%d")
        latest = _dt.datetime.fromtimestamp(
            summary.latest_session_ts_ns / 1e9
        ).strftime("%Y-%m-%d")
        span_s = (
            summary.latest_session_ts_ns - summary.earliest_session_ts_ns
        ) / 1e9
        span_days = max(1, int(span_s // 86400))
        print(f"  window:                {earliest} → {latest}  ({span_days}d)")
    print()

    if summary.n_sessions == 0:
        msg = (
            "no Stop-hook session_retrospective records yet — install "
            "hooks via `aegis install` to start tracking"
        )
        print(f"  {_yellow(msg)}")
        return

    # Cumulative
    print("  Cumulative cost & tokens")
    print(
        f"    billed dollars:      "
        f"${summary.cumulative_billed_dollars:,.4f}"
    )
    print(
        f"    input / output:      "
        f"{int(summary.total_input_tokens):>12,} / "
        f"{int(summary.total_output_tokens):,}"
    )
    print(
        f"    cache_read / write:  "
        f"{int(summary.total_cache_read_tokens):>12,} / "
        f"{int(summary.total_cache_creation_tokens):,}"
    )
    print()

    # Cache efficiency
    print("  Cache efficiency")
    weighted = summary.weighted_cache_hit_rate * 100
    avg_session = summary.avg_session_cache_hit_rate * 100
    weighted_color = (
        _green if weighted >= 70 else (_yellow if weighted >= 40 else _red)
    )
    avg_color = (
        _green if avg_session >= 70
        else (_yellow if avg_session >= 40 else _red)
    )
    print(
        f"    weighted hit_rate:   "
        f"{weighted_color(f'{weighted:5.1f}%')}  "
        f"(Σ cache_read / Σ total_input)"
    )
    print(
        f"    per-session avg:     "
        f"{avg_color(f'{avg_session:5.1f}%')}  "
        f"(arithmetic mean of session hit rates)"
    )
    print()

    # Inefficiency totals
    print("  Inefficiency totals (across all sessions)")
    print(f"    backtracks (Edit revert):   {summary.n_backtracks:>5}")
    print(f"    redundant tool calls:       {summary.n_redundant:>5}")
    print(f"    tool errors:                {summary.n_tool_errors:>5}")
    print(f"    compactions (PreCompact):   {summary.n_compactions:>5}")
    print(f"    user retries:               {summary.n_user_retries:>5}")
    print()

    # Per-session distribution
    print("  Per-session")
    print(
        f"    avg cost:              "
        f"${summary.avg_session_billed_dollars:.4f}"
    )
    if summary.n_sessions > 0:
        flagged_pct = (
            summary.sessions_with_inefficiency_signals
            / summary.n_sessions * 100
        )
        flagged_color = (
            _green if flagged_pct < 10
            else (_yellow if flagged_pct < 30 else _red)
        )
        print(
            f"    sessions w/ signals:   "
            f"{summary.sessions_with_inefficiency_signals} / "
            f"{summary.n_sessions}  "
            f"{flagged_color(f'({flagged_pct:.1f}%)')}"
        )

    # Top inefficient tools
    if summary.top_inefficient_tools:
        print()
        print("  Top inefficient tools (post_analysis-derived)")
        for t in summary.top_inefficient_tools:
            sig_total = t.n_backtracks + t.n_redundant + t.n_errors
            print(
                f"    {t.tool:<14} {sig_total:>3} signals  "
                f"(backtrack {t.n_backtracks}, "
                f"redundant {t.n_redundant}, "
                f"error {t.n_errors})  "
                f"in {t.n_calls} calls"
            )

    # Suggested next actions
    print()
    print("  Next actions")
    if summary.cumulative_billed_dollars > 0:
        print(
            "    • aegis cache-lint --transcript <session.jsonl>      "
            "  (find prompt-cache anti-patterns)"
        )
    if summary.n_sessions >= 2:
        print(
            "    • aegis cache-lint --transcript <after.jsonl> "
            "--compare-with <before.jsonl>"
        )
        print(
            "      (closed-loop verification: how much projected savings "
            "actually landed)"
        )


def cmd_verify_audit(args: argparse.Namespace) -> int:
    """Verify the local audit chain (v2.1.5).

    For local-mode (Solo Free) installs, walks ``~/.aegis/audit.jsonl``
    line-by-line and recomputes each ``prev_hash``/``this_hash`` pair.
    A single mutated record breaks every subsequent recompute, so this
    catches both silent edits and re-orderings.

    For sidecar-mode installs, the canonical verifier is the running
    service's ``/forensic/replay`` endpoint (M5/M9/M15 Ed25519 + Merkle
    + AES-GCM journal); the CLI just points operators there.
    """
    from aegis.audit.local_chain import verify_chain

    audit_path = (
        Path(args.audit) if args.audit
        else Path.home() / ".aegis" / "audit.jsonl"
    )
    if not audit_path.exists():
        print(f"[verify-audit] no local audit log at {audit_path}")
        print(
            "        sidecar mode: run `curl localhost:8000/forensic/replay` "
            "instead (Ed25519 + Merkle + AES-GCM journal verification)."
        )
        return 1

    from aegis.audit.signing import load_public_key_or_none
    pubkey_loaded = load_public_key_or_none() is not None

    ok, broken_at, total = verify_chain(audit_path)
    if ok:
        print(_green(f"\u2713 verify-audit (local chain) — {total} records intact"))
        print(f"  audit:  {audit_path}")
        if pubkey_loaded:
            print(
                f"  signing pubkey: {_green('loaded')} — signed "
                "records were also Ed25519-verified"
            )
        else:
            print(
                f"  signing pubkey: {_yellow('not configured')} — "
                "chain hash verified, signatures (if any) NOT "
                "cryptographically verified."
            )
            print("                  Run `aegis audit-key init` to enable.")
        return 0
    print(
        _red(
            f"\u2717 verify-audit FAILED — chain broken at record #{broken_at} "
            f"of {total}"
        )
    )
    print(f"  audit:  {audit_path}")
    print(
        "  cause:  prev_hash / this_hash / signature mismatch "
        "(line was mutated post-write)"
    )
    return 1


def cmd_audit_key(args: argparse.Namespace) -> int:
    """`aegis audit-key {init,show}` — manage the optional Ed25519
    signing key for the local audit chain (v4.4)."""
    from aegis.audit.signing import (
        default_private_key_path,
        default_public_key_path,
        init_signing_key,
        load_keypair,
    )

    action = getattr(args, "action", None) or "show"
    private_path = default_private_key_path()
    public_path = default_public_key_path()

    if action == "init":
        force = bool(getattr(args, "force", False))
        if private_path.is_file() and not force:
            print(_yellow(
                f"audit signing key already exists at {private_path} — "
                "refusing to overwrite. Use --force if you really want "
                "to rotate (you'll lose the ability to extend the "
                "previous chain with the new key)."
            ))
            return 1
        kp = init_signing_key(force=force)
        print(_green("✓ audit signing key generated"))
        print(f"  private:      {private_path}    (mode 0600)")
        print(f"  public:       {public_path}     (mode 0644)")
        print(f"  fingerprint:  {kp.fingerprint}")
        print()
        print(
            "  Every subsequent audit append now signs the record. Use "
            "`aegis verify-audit` to verify."
        )
        return 0

    if action == "show":
        if not private_path.is_file() or not public_path.is_file():
            print(_yellow(
                "no audit signing key configured. "
                "Run `aegis audit-key init` to generate one."
            ))
            return 1
        try:
            kp = load_keypair()
        except (FileNotFoundError, ValueError) as e:
            print(_red(f"failed to load keypair: {e}"))
            return 1
        print("audit signing key")
        print(f"  private:      {private_path}")
        print(f"  public:       {public_path}")
        print(f"  fingerprint:  {kp.fingerprint}")
        return 0

    print(f"[audit-key] unknown action: {action!r}")
    return 2


def cmd_replay(args: argparse.Namespace) -> int:
    c = _conn()
    rows = c.execute(
        "SELECT atv_hash, verdict, tool_name FROM intents ORDER BY id DESC LIMIT ?",
        (args.n,),
    ).fetchall()
    print(f"[replay] last {len(rows)} intents:")
    for atv_hash, verdict_json, tool in reversed(rows):
        v = json.loads(verdict_json)
        print(
            f"  {atv_hash[:12]}…  {v['decision']:8}  "
            f"{(tool or '-'):12}  {v.get('reason', '')}"
        )
    return 0


def cmd_policy_replay(args: argparse.Namespace) -> int:
    from replay.engine import replay  # type: ignore[import-not-found]

    out = replay(since_iso=args.since, policy_path=args.policy, limit=args.limit)
    print(f"[policy-replay] policy={out['policy']}")
    print(f"  total replayed:   {out['total']}")
    print(f"  unchanged:        {out['unchanged']}")
    print(f"  newly_blocked:    {len(out['newly_blocked'])}")
    for r in out["newly_blocked"][:10]:
        print(f"    id={r['id']:>5}  tool={r['tool']:<12}  reason={r['reason']}")
    print(f"  newly_allowed:    {len(out['newly_allowed'])}")
    for r in out["newly_allowed"][:10]:
        print(f"    id={r['id']:>5}  tool={r['tool']}")
    return 0


def cmd_cost(args: argparse.Namespace) -> int:
    """Cost dispatcher — routes to ``summary`` or ``replay``.

    * ``aegis cost summary`` — reads ``~/.aegis/audit.jsonl`` and
      aggregates step335 traces / escalations / per-tool / per-session
      into a real-money rollup. No D10 dependency.
    * ``aegis cost replay <transcript> [--budget X] [--model M]
      [--hw-provider sim] [--hw-attack ATTACK]`` — replays a Claude
      Code transcript through the firewall offline so you can
      experiment with different ceilings, models, and HW attack
      injection without burning real tokens.
    """
    action = getattr(args, "action", None)
    if action == "summary":
        return _cmd_cost_summary(args)
    if action == "replay":
        return _cmd_cost_replay(args)
    if action == "multi-agent":
        return _cmd_cost_multi_agent(args)
    # Old `--days N` shape kept for backwards compatibility — print a
    # short hint and exit 0 so users don't get confused by argparse.
    print(_yellow(
        "[cost] usage: aegis cost {summary,replay,multi-agent} ...  see --help"
    ))
    return 2


def _cmd_cost_summary(args: argparse.Namespace) -> int:
    from aegis.cost.summary import summarize

    audit_path = Path(args.audit) if args.audit else (
        Path.home() / ".aegis" / "audit.jsonl"
    )
    s = summarize(audit_path, spike_threshold=float(args.spike_threshold))

    if args.json:
        from dataclasses import asdict

        # asdict expands the nested PerTool/PerSession dataclasses too.
        payload = asdict(s)
        payload["audit_path"] = str(s.audit_path)
        print(json.dumps(payload, indent=2))
        return 0

    if s.n_records_total == 0:
        print(_yellow(f"[cost summary] no records at {audit_path}"))
        print("              (the local hook writes to this file on every "
              "tool call; restart Claude Code or run `aegis status` first)")
        return 0

    # PR-C — multi-agent breakdown view. Shows ONLY the per-aid
    # column, expanded and sorted, so an operator running multiple
    # agents (Telegram bot per channel, Claude Code subagents,
    # OpenClaw multi-provider) sees attribution at a glance.
    if getattr(args, "by_aid", False):
        return _cmd_cost_summary_by_aid(s, audit_path, top=int(args.top))

    print(f"AegisData cost summary  ({audit_path})")
    print("=" * 60)
    print(f"  records:           {s.n_records_total:>8,}  "
          f"(Pre={s.n_pretool}, Post={s.n_posttool})")
    print(f"  decisions:         "
          f"ALLOW {s.n_allow:,}  BLOCK {s.n_block:,}  "
          f"ASK {s.n_approval:,}")
    print(f"  max cumulative $:  ${s.max_cumulative_dollars:>10.4f}")
    print(f"  step335 escalations:    {s.n_step335_escalations:>5,}  "
          "(budget overrun)")
    print(f"  M12 cost-divergence:    {s.n_m12_escalations:>5,}  "
          "(Claim 27)")
    print(f"  spike events (Δ≥${args.spike_threshold:.2f}): "
          f"{len(s.spike_events):>5,}")
    if s.per_tool:
        print()
        print("  Top tools by max cumulative $:")
        for t in s.per_tool[:10]:
            print(f"    {t.tool:<24} calls={t.n_calls:>5}  "
                  f"max=${t.max_cumulative_dollars:>9.4f}  "
                  f"BLOCK={t.n_block}  ASK={t.n_approval}")
    if s.per_session:
        print()
        print("  Top sessions by max cumulative $:")
        for ss in s.per_session[:10]:
            print(f"    {ss.aid[:24]:<24} calls={ss.n_calls:>5}  "
                  f"max=${ss.max_cumulative_dollars:>9.4f}  "
                  f"escalations={ss.n_escalations}")
    if s.spike_events:
        print()
        print(f"  Recent spikes (Δ≥${args.spike_threshold:.2f}):")
        for ev in s.spike_events[-5:]:
            print(f"    aid={ev['aid'][:20]:<20}  tool={ev['tool']:<12}  "
                  f"${ev['from_dollars']:.4f} → ${ev['to_dollars']:.4f}  "
                  f"(+${ev['delta']:.4f})")
    return 0


def _cmd_cost_summary_by_aid(
    summary: Any, audit_path: Path, *, top: int = 10,
) -> int:
    """Multi-agent cost breakdown — `aegis cost summary --by-aid`.

    Shows the per-aid (per-agent / per-session) breakdown that the
    default summary view buries at the bottom of a much larger
    report. Designed for the multi-agent scenarios (Claude Code
    subagents, OpenClaw multi-channel, custom agents on the same
    sidecar) where attribution per aid is the primary signal an
    operator wants.

    Sort: descending by max cumulative $ — the aid that spent the
    most appears first.
    """
    print(f"AegisData cost summary — by aid  ({audit_path})")
    print("=" * 68)
    if not summary.per_session:
        print(_yellow(
            "  no per-aid data — audit log has no `aid` field on any "
            "record. Run `aegis install --mode local` to wire up the "
            "PreToolUse hook (which stamps aid)."
        ))
        return 0

    sessions = summary.per_session
    total = len(sessions)
    if top > 0 and total > top:
        sessions = sessions[:top]
        print(f"  (top {top} of {total} aids — use `--top 0` to see all)")
    else:
        print(f"  ({total} aids total)")
    print()
    print(
        f"  {'aid':<32}  {'calls':>5}  "
        f"{'max $':>9}  {'esc':>4}  {'window':>8}"
    )
    print("  " + "─" * 64)
    for ss in sessions:
        aid_short = ss.aid[:32] if len(ss.aid) > 32 else ss.aid
        # Wall-clock duration of the aid's activity in this audit log.
        if ss.first_seen_ns and ss.last_seen_ns:
            dur_min = max(
                0, (ss.last_seen_ns - ss.first_seen_ns) // 60_000_000_000,
            )
            window = f"{int(dur_min)}m"
        else:
            window = "-"
        print(
            f"  {aid_short:<32}  {ss.n_calls:>5}  "
            f"${ss.max_cumulative_dollars:>8.4f}  "
            f"{ss.n_escalations:>4}  {window:>8}"
        )
    print("  " + "─" * 64)

    # Roll-up totals across the visible aids.
    sum_calls = sum(ss.n_calls for ss in sessions)
    sum_max_dollars = sum(ss.max_cumulative_dollars for ss in sessions)
    sum_esc = sum(ss.n_escalations for ss in sessions)
    print(
        f"  {'(visible total)':<32}  {sum_calls:>5}  "
        f"${sum_max_dollars:>8.4f}  "
        f"{sum_esc:>4}"
    )
    print()
    print(
        "  Tip: `aegis forensic <aid>` for the full timeline of "
        "any aid above."
    )
    return 0


def _cmd_cost_replay(args: argparse.Namespace) -> int:
    from aegis.cost.replay import ReplayConfig, replay

    config = ReplayConfig(
        transcript_path=Path(args.transcript),
        budget_dollars=float(args.budget),
        model_for_cost=str(args.model),
        hw_provider=str(args.hw_provider),
        hw_attack=str(args.hw_attack or ""),
        multiplier=float(args.multiplier),
    )
    if not config.transcript_path.is_file():
        print(_red(f"[cost replay] transcript not found: {config.transcript_path}"))
        return 2

    summary = replay(config)

    if args.json:
        from dataclasses import asdict

        payload = asdict(summary)
        payload["config"]["transcript_path"] = str(
            summary.config.transcript_path
        )
        print(json.dumps(payload, indent=2, default=str))
        return 0

    print(f"AegisData cost replay  ({config.transcript_path})")
    print("=" * 70)
    print(f"  budget:    ${config.budget_dollars}")
    print(f"  model:     {config.model_for_cost}")
    print(f"  HW:        provider={config.hw_provider} attack={config.hw_attack or '(none)'}")
    print(f"  multiplier: {config.multiplier}× M12 escalation baseline")
    print("-" * 70)
    print(f"  turns:           {summary.n_turns_total:>4}")
    print(f"  tool calls:      {summary.n_tool_calls:>4}")
    print(f"  final cum $ (FLOP proxy):   ${summary.final_cumulative_dollars:.4f}")
    print(f"  final cum $ (billed est.):  ${summary.final_cumulative_billed_dollars:.4f}  ← cache-aware")
    print(f"  decisions:       ALLOW {summary.n_allow}  BLOCK {summary.n_block}  ASK {summary.n_approval}")
    print(f"  step335 hits:    {summary.n_step335_escalations}")
    print(f"  M12 hits:        {summary.n_m12_escalations}")
    if summary.first_escalation_turn is not None:
        print(f"  first non-ALLOW: turn {summary.first_escalation_turn}")
    if not summary.calls:
        return 0
    print()
    print(f"  {'turn':>4}  {'tool':<14}  {'cum_$':>9}  {'decision':<18}  reason")
    for c in summary.calls:
        decided = c.decision
        colored = (
            _green(decided) if decided == "ALLOW"
            else _yellow(decided) if decided == "REQUIRE_APPROVAL"
            else _red(decided)
        )
        # Pad with raw decision so columns align (color escapes are zero-width).
        pad = " " * max(0, 18 - len(decided))
        reason = (c.reason[:90] + "…") if len(c.reason) > 90 else c.reason
        print(f"  {c.turn_idx:>4}  {c.tool_name[:14]:<14}  "
              f"${c.cumulative_dollars:>8.4f}  {colored}{pad}  {reason}")
    return 0


def _cmd_cost_multi_agent(args: argparse.Namespace) -> int:
    """Multi-agent (fleet) cost replay — interleaves N transcripts,
    accumulates fleet $, fires notifier on threshold crossings."""
    from aegis.cost.multi_agent import (
        AgentReplayInput,
        FleetThreshold,
        StderrNotifier,
        multi_agent_replay,
    )
    from aegis.cost.replay import ReplayConfig

    raw_paths = [p.strip() for p in str(args.transcripts).split(",")]
    paths = [Path(p) for p in raw_paths if p]
    if not paths:
        print(
            _red("[cost multi-agent] need --transcripts a.jsonl,b.jsonl,..."),
            file=sys.stderr,
        )
        return 2
    missing = [p for p in paths if not p.is_file()]
    if missing:
        for p in missing:
            print(
                _red(f"[cost multi-agent] transcript not found: {p}"),
                file=sys.stderr,
            )
        return 2

    agents = [
        AgentReplayInput(transcript_path=p, aid=f"agent-{i + 1}")
        for i, p in enumerate(paths)
    ]
    template = ReplayConfig(
        transcript_path=paths[0],          # placeholder; overridden per agent
        budget_dollars=float(args.per_agent_budget),
        model_for_cost=str(args.model),
        hw_provider=str(args.hw_provider),
        hw_attack=str(args.hw_attack or ""),
        multiplier=float(args.multiplier),
    )
    thresholds: list[FleetThreshold] = []
    if args.threshold is not None:
        thresholds.append(
            FleetThreshold(
                dollars=float(args.threshold),
                label="warn",
                interactive=bool(args.interactive),
            )
        )
    if args.hard_stop is not None:
        thresholds.append(
            FleetThreshold(
                dollars=float(args.hard_stop),
                label="hard_stop",
                interactive=bool(args.interactive),
            )
        )
    if not thresholds:
        # Sensible defaults so the command does something useful with
        # --transcripts alone.
        thresholds = [
            FleetThreshold(dollars=5.0, label="warn"),
            FleetThreshold(dollars=20.0, label="hard_stop"),
        ]

    notifier = StderrNotifier(interactive=bool(args.interactive))
    summary = multi_agent_replay(
        agents,
        thresholds=thresholds,
        config_template=template,
        notifier=notifier,
    )

    if args.json:
        from dataclasses import asdict

        payload = asdict(summary)
        # ReplayConfig path inside per-call may have Path; serialise
        # deterministically.
        print(json.dumps(payload, indent=2, default=str))
        return 0 if summary.aborted_at_call is None else 3

    print("AegisData multi-agent cost replay")
    print("=" * 70)
    print(f"  agents:                 {summary.n_agents}")
    print(f"  fleet calls:            {summary.n_total_calls}")
    print(f"  final fleet $:          ${summary.final_fleet_dollars:.4f}")
    print(f"  threshold crossings:    {len(summary.crossings)}")
    if summary.aborted_at_call is not None:
        print(_red(f"  ⚠ ABORTED at fleet call #{summary.aborted_at_call}"))
    print()
    print("  Per-agent contribution:")
    for aid, dollars in sorted(
        summary.per_agent_dollars.items(),
        key=lambda kv: kv[1], reverse=True,
    ):
        print(f"    {aid:<12} ${dollars:>10.4f}")
    if summary.crossings:
        print()
        print("  Threshold crossings:")
        for c in summary.crossings:
            label_color = (
                _red if c.threshold.label == "hard_stop" else _yellow
            )
            decision_color = (
                _green if c.operator_decision == "continue" else _red
            )
            print(
                f"    call#{c.crossed_at_call:<4} "
                f"{label_color(c.threshold.label):<22} "
                f"${c.fleet_dollars_before:.4f} → ${c.fleet_dollars_after:.4f}  "
                f"agent={c.aid_at_crossing}  "
                f"→ {decision_color(c.operator_decision)}"
            )
    return 0 if summary.aborted_at_call is None else 3


def cmd_fleet_monitor(args: argparse.Namespace) -> int:
    """Live multi-session cost monitor — start/stop/status."""
    import os as _os
    import subprocess as _subprocess

    from aegis.cost.fleet_monitor import (
        DEFAULT_AUDIT_PATH,
        DEFAULT_PID_PATH,
        DEFAULT_STATE_PATH,
        FleetThreshold,
        is_running,
    )

    audit_path = Path(args.audit) if args.audit else DEFAULT_AUDIT_PATH

    if args.action == "status":
        running = is_running(DEFAULT_PID_PATH)
        if running:
            try:
                pid_info = json.loads(DEFAULT_PID_PATH.read_text())
                pid = pid_info.get("pid")
            except (json.JSONDecodeError, OSError):
                pid = "?"
            print(_green(f"fleet-monitor running   pid={pid}"))
        else:
            print(_yellow("fleet-monitor stopped"))
        if DEFAULT_STATE_PATH.is_file():
            try:
                state = json.loads(DEFAULT_STATE_PATH.read_text())
                print(f"  records seen:   {state.get('n_records_seen', 0)}")
                print(f"  fleet $:        ${state.get('fleet_dollars', 0):.4f}")
                print(f"  fired:          {state.get('fired_thresholds', [])}")
                print(f"  last_offset:    {state.get('last_offset', 0)}")
            except (json.JSONDecodeError, OSError):
                pass
        return 0 if running else 1

    if args.action == "stop":
        if not is_running(DEFAULT_PID_PATH):
            print(_yellow("fleet-monitor not running"))
            return 1
        try:
            pid_info = json.loads(DEFAULT_PID_PATH.read_text())
            pid = int(pid_info["pid"])
            _os.kill(pid, signal.SIGTERM)
            # Wait briefly for the daemon to clean up its PID file.
            for _ in range(30):
                if not is_running(DEFAULT_PID_PATH):
                    print(_green("fleet-monitor stopped"))
                    return 0
                time.sleep(0.1)
            print(_yellow("fleet-monitor SIGTERM sent but PID still alive"))
            return 1
        except (json.JSONDecodeError, OSError, KeyError, ValueError) as e:
            print(_red(f"stop failed: {e}"), file=sys.stderr)
            return 2

    # start
    if is_running(DEFAULT_PID_PATH):
        print(_yellow("fleet-monitor already running — `aegis fleet-monitor stop` first"))
        return 1
    thresholds: list[FleetThreshold] = []
    if args.threshold is not None:
        thresholds.append(FleetThreshold(
            dollars=float(args.threshold), label="warn",
            interactive=bool(args.interactive),
        ))
    if args.hard_stop is not None:
        thresholds.append(FleetThreshold(
            dollars=float(args.hard_stop), label="hard_stop",
            interactive=bool(args.interactive),
        ))
    if not thresholds:
        thresholds = [
            FleetThreshold(dollars=5.0, label="warn"),
            FleetThreshold(dollars=20.0, label="hard_stop"),
        ]

    slack_url = None
    if args.slack_url_env:
        slack_url = _os.environ.get(args.slack_url_env)
        if not slack_url:
            print(_yellow(
                f"  warning: {args.slack_url_env} not set"
            ), file=sys.stderr)

    ntfy_topic = None
    if getattr(args, "ntfy_topic_env", None):
        ntfy_topic = _os.environ.get(args.ntfy_topic_env)
        if not ntfy_topic:
            print(_yellow(
                f"  warning: {args.ntfy_topic_env} not set"
            ), file=sys.stderr)

    crossings_log = (
        str(Path(args.crossings_log).expanduser())
        if getattr(args, "crossings_log", None) else None
    )

    # Daemonise via subprocess so the parent CLI returns immediately.
    # The child re-execs into a `python -c` that calls serve_forever
    # with the same arguments.
    bootstrap = (
        "import json,sys;"
        "sys.path.insert(0,'src');"
        "from pathlib import Path;"
        "from aegis.cost.fleet_monitor import (FleetThreshold, "
        "make_default_notifier, serve_forever, "
        "DEFAULT_AUDIT_PATH, DEFAULT_STATE_PATH, "
        "DEFAULT_PID_PATH, DEFAULT_STOP_FLAG);"
        "args=json.loads(sys.argv[1]);"
        "thresholds=[FleetThreshold(**t) for t in args['thresholds']];"
        "n=make_default_notifier("
        "slack_webhook_url=args.get('slack_url'),"
        "ntfy_topic=args.get('ntfy_topic'),"
        "ntfy_base_url=args.get('ntfy_base_url','https://ntfy.sh'),"
        "crossings_log=args.get('crossings_log'),"
        "interactive=args.get('interactive', False));"
        "sys.exit(serve_forever("
        "audit_path=Path(args['audit_path']),"
        "state_path=DEFAULT_STATE_PATH,"
        "pid_path=DEFAULT_PID_PATH,"
        "stop_flag=DEFAULT_STOP_FLAG,"
        "thresholds=thresholds,"
        "notifier=n,"
        "poll_interval_s=args.get('poll_interval', 1.0)))"
    )
    payload = json.dumps({
        "audit_path": str(audit_path),
        "thresholds": [
            {"dollars": t.dollars, "label": t.label, "interactive": t.interactive}
            for t in thresholds
        ],
        "slack_url": slack_url,
        "ntfy_topic": ntfy_topic,
        "ntfy_base_url": getattr(args, "ntfy_base_url", "https://ntfy.sh"),
        "crossings_log": crossings_log,
        "interactive": bool(args.interactive),
        "poll_interval": float(args.poll_interval),
    })
    py = _hook_python_executable()
    proc = _subprocess.Popen(
        [py, "-c", bootstrap, payload],
        cwd=str(PROJECT_ROOT),
        stdout=_subprocess.DEVNULL,
        stderr=_subprocess.DEVNULL,
        start_new_session=True,
    )
    # Wait for PID file to appear.
    for _ in range(50):
        if is_running(DEFAULT_PID_PATH):
            break
        if proc.poll() is not None:
            print(_red(f"daemon exited with rc={proc.returncode}"), file=sys.stderr)
            return 2
        time.sleep(0.1)
    if not is_running(DEFAULT_PID_PATH):
        print(_red("daemon failed to start"), file=sys.stderr)
        return 2
    print(_green(f"✓ fleet-monitor started  pid={proc.pid}"))
    print(f"  audit:        {audit_path}")
    print(f"  thresholds:   {[(t.label, t.dollars) for t in thresholds]}")
    if slack_url:
        print("  slack:        configured (URL hidden)")
    if ntfy_topic:
        ntfy_url = f"{args.ntfy_base_url.rstrip('/')}/{ntfy_topic}"
        print(f"  ntfy:         {ntfy_url}")
    if crossings_log:
        print(f"  crossings:    {crossings_log}")
    return 0


def cmd_health(_: argparse.Namespace) -> int:
    """Runtime malfunction signal — deferred (D7).

    The runtime malfunction classifier (``aegis.monitor.malfunction``)
    is scheduled for v2.5 D7. Until then we only surface the
    ATMU-derived counters which `aegis status` already shows.
    """
    print(
        _yellow(
            "[health] runtime malfunction classifier is D7 deferred."
        )
    )
    print(
        "         Use `aegis status` for ATMU-derived "
        "(committed / aborted / prepared) counts."
    )
    return 0


def cmd_metrics(args: argparse.Namespace) -> int:
    """Scrape self-hosted inference server /metrics and pretty-print.

    Three modes:

    * Default — single endpoint via ``--vllm-url`` (legacy behaviour,
      backward compatible).
    * ``--all`` — read ``~/.aegis/inference.toml`` and scrape every
      configured endpoint concurrently. Cloud / disabled endpoints
      are listed but not scraped. Gap B (issue #145) per-aid view.
    * ``--aid <name>`` — pick exactly one endpoint from the registry
      by its aid label.

    Currently supports vLLM (OpenClaw + Local OSS LLM track only).
    Cloud LLM tracks have no equivalent — returns a friendly hint.
    """
    from dataclasses import asdict

    from aegis.inference.vllm_metrics import (
        VLLMMetricsError,
        scrape_vllm_metrics,
    )

    # PR-L (Gap B) — multi-endpoint paths read the registry.
    if getattr(args, "all", False) or getattr(args, "aid", None):
        return _cmd_metrics_multi(args)

    try:
        snap = scrape_vllm_metrics(
            args.vllm_url,
            timeout_s=float(args.timeout),
        )
    except VLLMMetricsError as e:
        print(_red(f"[metrics] {e}"), file=sys.stderr)
        print(file=sys.stderr)
        print("  This command targets self-hosted inference servers", file=sys.stderr)
        print("  (vLLM today; Ollama / TGI in follow-up PRs).", file=sys.stderr)
        print(file=sys.stderr)
        print("  If you're on the Claude Code or OpenClaw + Cloud LLM", file=sys.stderr)
        print("  track, this metric surface is not exposed by the cloud", file=sys.stderr)
        print("  provider — see docs/releases/OPENCLAW_LOCAL.ko.md §2.", file=sys.stderr)
        return 1

    if args.json:
        # Stable, machine-readable shape for fleet-monitor / jq.
        out = asdict(snap)
        out["kv_cache_pressure_band"] = snap.kv_cache_pressure_band()
        out["saturated"] = snap.saturated()
        print(json.dumps(out, indent=2, sort_keys=True))
        return 0

    # Human-readable table (default).
    band_color = {
        "low": _green,
        "moderate": _green,
        "high": _yellow,
        "critical": _red,
    }[snap.kv_cache_pressure_band()]

    print(f"vLLM /metrics  ·  {args.vllm_url}")
    print("─" * 60)
    print(
        f"  KV cache         {snap.kv_cache_used_pct * 100:5.1f}%   "
        f"{band_color(f'[{snap.kv_cache_pressure_band()}]')}"
    )
    print(f"  CPU swap cache   {snap.cpu_cache_used_pct * 100:5.1f}%")
    print(
        f"  Queue            {snap.requests_running} running"
        f"  ·  {snap.requests_waiting} waiting"
    )
    print()
    print(
        f"  prompt tokens    {snap.prompt_tokens_total:>12,}  "
        f"(avg {snap.avg_prompt_tokens_per_request:.0f}/req)"
    )
    print(
        f"  output tokens    {snap.generation_tokens_total:>12,}  "
        f"(avg {snap.avg_generation_tokens_per_request:.0f}/req)"
    )
    print()
    print(f"  TTFT  avg        {snap.avg_ttft_seconds * 1000:7.1f} ms")
    print(f"  TPOT  avg        {snap.avg_tpot_seconds * 1000:7.1f} ms")
    if snap.spec_decode_efficiency is not None:
        print()
        print(
            f"  spec-decode      "
            f"{snap.spec_decode_efficiency * 100:5.1f}%  "
            f"acceptance ({snap.spec_decode_accepted_total:,} / "
            f"{snap.spec_decode_emitted_total:,})"
        )
    print("─" * 60)
    if snap.saturated():
        print(_red("  ⚠ inference server saturated — see kv-cache-optimizer advisor"))
    return 0


def _cmd_metrics_multi(args: argparse.Namespace) -> int:
    """Multi-endpoint scrape (Gap B / issue #145).

    Reads ``~/.aegis/inference.toml`` (or ``$AEGIS_INFERENCE_REGISTRY``)
    and scrapes every enabled vLLM endpoint concurrently. Cloud /
    disabled endpoints are recorded as ``EndpointSkipped`` so the
    table remains complete.

    Returns 0 even when some endpoints are unreachable — the scrape is
    a best-effort observation, not a security gate. Returns 1 only
    when:

    * the registry file is malformed (``InferenceRegistryError``),
    * ``--aid <name>`` was passed but no such endpoint exists,
    * the registry is empty (nothing to scrape — likely a config
      mistake the user wants to know about).
    """
    from dataclasses import asdict

    from aegis.inference import (
        EndpointSkipped,
        EndpointUnreachable,
        InferenceMetrics,
        InferenceRegistryError,
        load_registry,
        scrape_all,
    )

    try:
        registry = load_registry()
    except InferenceRegistryError as e:
        print(_red(f"[metrics] inference.toml: {e}"), file=sys.stderr)
        return 1

    if registry.is_empty():
        path = registry.source_path or "~/.aegis/inference.toml"
        print(
            _yellow(
                f"[metrics] no endpoints in registry ({path}) — "
                "see docs/INFERENCE_REGISTRY.md for the schema."
            ),
            file=sys.stderr,
        )
        return 1

    aid_filter = getattr(args, "aid", None)
    if aid_filter:
        ep = registry.by_aid(aid_filter)
        if ep is None:
            print(
                _red(
                    f"[metrics] no endpoint with aid={aid_filter!r} "
                    f"in {registry.source_path}. "
                    f"Configured aids: {sorted(e.aid for e in registry.endpoints)}"
                ),
                file=sys.stderr,
            )
            return 1
        # Filter to a single-row registry by replacing the tuple. The
        # frozen dataclass requires dataclasses.replace, but
        # InferenceRegistry is small enough that constructing a new
        # one is cleanest.
        from aegis.inference import InferenceRegistry

        registry = InferenceRegistry(
            endpoints=(ep,),
            defaults_timeout_s=registry.defaults_timeout_s,
            source_path=registry.source_path,
        )

    results = scrape_all(registry)

    if args.json:
        # Stable JSON shape: {aid: {kind: ..., ...}} so a downstream
        # jq pipe can dispatch on `kind`.
        out: dict[str, dict[str, Any]] = {}
        for aid, r in sorted(results.items()):
            if isinstance(r, InferenceMetrics):
                row = asdict(r)
                row["kind"] = "metrics"
                row["kv_cache_pressure_band"] = r.kv_cache_pressure_band()
                row["saturated"] = r.saturated()
            elif isinstance(r, EndpointUnreachable):
                row = asdict(r)
                row["kind"] = "unreachable"
            else:
                assert isinstance(r, EndpointSkipped)
                row = asdict(r)
                row["kind"] = "skipped"
            out[aid] = row
        print(json.dumps(out, indent=2, sort_keys=True))
        return 0

    # Human-readable table.
    print(f"inference.toml  ·  {registry.source_path}")
    print(f"{len(results)} endpoint(s)")
    print("─" * 78)
    print(
        f"{'aid':<24}  {'provider':<10}  {'kv':>6}  {'queue':>10}  status"
    )
    print("─" * 78)
    for aid, r in sorted(results.items()):
        if isinstance(r, InferenceMetrics):
            band = r.kv_cache_pressure_band()
            band_color = {
                "low": _green,
                "moderate": _green,
                "high": _yellow,
                "critical": _red,
            }[band]
            ep = registry.by_aid(aid)
            provider = ep.provider if ep else "?"
            queue = f"{r.requests_running}/{r.requests_waiting}"
            print(
                f"{aid:<24}  {provider:<10}  "
                f"{r.kv_cache_used_pct * 100:5.1f}%  "
                f"{queue:>10}  {band_color(band)}"
            )
        elif isinstance(r, EndpointUnreachable):
            print(
                f"{aid:<24}  {'vllm':<10}  {'  N/A':>6}  {'      N/A':>10}  "
                f"{_red('unreachable')}  ({r.reason[:40]})"
            )
        else:
            assert isinstance(r, EndpointSkipped)
            label = r.provider_name or r.provider
            print(
                f"{aid:<24}  {label[:10]:<10}  {'  N/A':>6}  {'      N/A':>10}  "
                f"{_yellow('skipped')}  ({r.reason[:40]})"
            )
    print("─" * 78)
    return 0


# ──────────────────────────────────────────────────────────────────
# `aegis license` — Solo Pro / Team / Enterprise gate (issue #149)
#
# This is the CLI side of `src/aegis/license/`. Five actions:
#
#   activate <path>   — verify + persist `~/.aegis/license.jwt`
#   status            — print active tier + license_id + expiry
#   deactivate        — remove the file, revert to Solo Free
#   verify <path>     — validate without activating (CI / dry-run)
#   refresh           — opt-in CRL fetch (placeholder until issuer
#                       service ships; see docs/LICENSE_KEY.md §3)
#
# All five are *no-ops* against the runtime today: no other code
# path calls `has_feature()` yet, so activating a license doesn't
# change behavior. The CLI exists so we can ship the issuer service
# and start onboarding paying design partners *without* a runtime
# code change blocking them. Wiring `--profile pro/cloud` etc. to
# the gate is a separate follow-up PR per feature.
# ──────────────────────────────────────────────────────────────────


def cmd_license(args: argparse.Namespace) -> int:
    """Dispatch on `args.action`."""
    action = getattr(args, "action", None)
    if action == "activate":
        return _cmd_license_activate(args)
    if action == "status":
        return _cmd_license_status(args)
    if action == "deactivate":
        return _cmd_license_deactivate(args)
    if action == "verify":
        return _cmd_license_verify(args)
    if action == "refresh":
        return _cmd_license_refresh(args)
    print(_red(f"[license] unknown action: {action!r}"), file=sys.stderr)
    return 1


def _cmd_license_activate(args: argparse.Namespace) -> int:
    from aegis.license import LicenseVerifyError, activate_from_path

    src = Path(args.path)
    if not src.exists():
        print(_red(f"[license] file not found: {src}"), file=sys.stderr)
        return 1
    try:
        claims = activate_from_path(src)
    except LicenseVerifyError as e:
        print(
            _red(f"[license] activate failed: {e.reason} — {e}"),
            file=sys.stderr,
        )
        print(
            "  License file at " + str(src) + " was NOT installed; "
            "the runtime is in Solo Free mode.",
            file=sys.stderr,
        )
        return 1
    print(_green(f"✅ activated tier={claims.tier} license_id={claims.license_id}"))
    print(f"   expires in {claims.expires_in_seconds // 86400}d")
    print(f"   features: {len(claims.features) or '(tier defaults)'}")
    return 0


def _cmd_license_status(args: argparse.Namespace) -> int:
    from aegis.license import (
        init_active_from_disk,
        license_path,
    )

    # Reload from disk so `status` always reflects what's persisted.
    claims = init_active_from_disk()
    path = license_path()

    if claims is None:
        print("license:    Solo Free (no key)")
        print(f"file:       {path}  ({'exists but invalid' if path.exists() else 'absent'})")
        if path.exists():
            print()
            print(
                "  The license file exists but failed verification — "
                "run `aegis license verify <path>` to see why."
            )
        return 0

    print(f"tier:       {claims.tier}")
    print(f"license_id: {claims.license_id}")
    print(f"issuer kid: {claims.kid}")
    print(f"seats:      {claims.seats}")
    days = claims.expires_in_seconds // 86400
    print(f"expires in: {days}d")
    print(f"file:       {path}")
    if claims.burnin_bind:
        print(f"burnin_bind: {claims.burnin_bind[:16]}...")
    explicit = (
        f"explicit allow-list ({len(claims.features)} entries)"
        if claims.features
        else "(tier defaults)"
    )
    print(f"features:   {explicit}")
    return 0


def _cmd_license_deactivate(args: argparse.Namespace) -> int:
    from aegis.license import deactivate

    removed = deactivate()
    if removed:
        print(_green("✅ license removed; runtime is now Solo Free"))
    else:
        print(_yellow("[license] no license to remove (already Solo Free)"))
    return 0


def _cmd_license_verify(args: argparse.Namespace) -> int:
    """Validate a license file without activating it. Useful for CI
    pipelines that want to verify a key the issuer just minted, and
    for the `--dry-run` UX before the user commits to activate."""
    from aegis.license import LicenseVerifyError, verify_license

    src = Path(args.path)
    if not src.exists():
        print(_red(f"[license] file not found: {src}"), file=sys.stderr)
        return 1
    token = src.read_text(encoding="utf-8").strip()
    try:
        claims = verify_license(token)
    except LicenseVerifyError as e:
        print(_red(f"❌ verify failed: {e.reason}"))
        print(f"   {e}")
        return 1
    print(_green(
        f"✅ verify OK: tier={claims.tier} "
        f"license_id={claims.license_id} expires_in="
        f"{claims.expires_in_seconds // 86400}d"
    ))
    return 0


def _cmd_license_refresh(args: argparse.Namespace) -> int:
    """Opt-in CRL refresh. Not yet implemented — the issuer service
    that publishes the CRL doesn't exist yet. The command is shipped
    in v0.2.x so the operator's muscle memory works once the issuer
    service launches.

    Per `docs/LICENSE_KEY.md §3 Layer B`, when this lights up it
    fetches a small JSON CRL from the issuer endpoint and caches it
    at `~/.aegis/license-crl.json`. The CRL fetch is the *only*
    network call a paid-tier install ever makes; the default Solo
    Free install never fetches anything.
    """
    print(_yellow(
        "[license] refresh is not yet active — the issuer CRL endpoint"
    ))
    print(
        "          doesn't exist yet (issuer service ships in a separate"
    )
    print(
        "          PR). The Solo Free contract still holds: 0 outbound"
    )
    print(
        "          requests on the default install."
    )
    print()
    print(
        "          Track issuer-service rollout: "
        "https://github.com/happyikas/Aegis-ATV/issues/149"
    )
    return 0


def cmd_rollback(args: argparse.Namespace) -> int:
    from aegis.rollback.snapshot import bulk_restore, restore

    if args.session or args.since:
        out = bulk_restore(
            session_id=args.session,
            since_iso=args.since,
            dry_run=args.dry_run,
            allow_git=args.allow_git,
        )
        label = "would restore" if args.dry_run else "restored"
        print(
            f"[bulk-rollback] candidates={out['candidates']}  "
            f"{label}={len(out['restored'])}  skipped={len(out['skipped'])}"
        )
        for r in out["restored"][:30]:
            print(f"  ✓ {r}")
        for s in out["skipped"][:10]:
            print(f"  · {s}")
        return 0 if out["restored"] else 2

    if not args.invocation_id:
        print("[rollback] need invocation_id, --session, or --since")
        return 2
    out = restore(args.invocation_id, allow_git=args.allow_git, dry_run=args.dry_run)
    print(f"[rollback] {args.invocation_id}{' (DRY-RUN)' if args.dry_run else ''}")
    if args.dry_run:
        for cap in out.get("would_restore", []):
            print(f"  ? {cap}")
        return 0
    for r in out.get("restored", []):
        print(f"  ✓ restored: {r}")
    for s in out.get("skipped", []):
        print(f"  · skipped:  {s}")
    return 0 if out.get("restored") else 2


def _parse_window_secs(spec: str) -> int:
    if spec.endswith("d"):
        return int(spec[:-1]) * 86400
    if spec.endswith("h"):
        return int(spec[:-1]) * 3600
    return int(spec)


def cmd_snapshots(args: argparse.Namespace) -> int:
    from aegis.rollback.snapshot import list_snapshots, prune

    if args.action == "prune":
        secs = _parse_window_secs(args.older_than)
        n = prune(older_than_secs=secs)
        print(f"[snapshots prune] removed {n} snapshots older than {args.older_than}")
        return 0
    for snap in list_snapshots(limit=args.limit):
        cap = ",".join(snap.get("captured", []))[:80]
        print(
            f"  {snap['invocation_id']}  "
            f"tool={snap.get('tool', '?'):<12} captured={cap}"
        )
    return 0


def _resolve_aegis_cmd() -> str:
    """Return the bash command that invokes ``aegis`` from anywhere on the
    user's machine — used by the slash-command templates.

    Resolution priority:
      1. If ``aegis`` is on the user's $PATH (brew install / pipx install /
         curl|sh installer with ~/.local/bin), use the bare command.
      2. Else fall back to ``uv run --project <PROJECT_ROOT> aegis``, which
         works from a fresh source clone without a global install.
    """
    on_path = shutil.which("aegis")
    if on_path:
        return "aegis"
    # Source-clone fallback. PROJECT_ROOT is the directory we were imported
    # from; uv handles dep resolution automatically.
    return f'uv run --project "{PROJECT_ROOT}" aegis'


def _install_slash_commands(*, dry_run: bool = False) -> tuple[int, list[str]]:
    """Render ``tools/claude_commands/aegis-*.md`` templates into
    ``~/.claude/commands/`` so users can invoke ``/aegis-report`` etc.
    from inside Claude Code without leaving the session.

    The template's ``{AEGIS_CMD}`` placeholder is replaced at install time
    with the resolved command (see :func:`_resolve_aegis_cmd`).

    Returns ``(count_written, list_of_filenames)``. Idempotent: re-running
    overwrites whatever is there.
    """
    if not SLASH_COMMANDS_SRC.exists():
        return 0, []
    if not dry_run:
        SLASH_COMMANDS_DST.mkdir(parents=True, exist_ok=True)
    aegis_cmd = _resolve_aegis_cmd()
    written: list[str] = []
    for tmpl in sorted(SLASH_COMMANDS_SRC.glob("aegis-*.md")):
        body = tmpl.read_text(encoding="utf-8")
        rendered = body.replace("{AEGIS_CMD}", aegis_cmd)
        # Append our marker so uninstall can identify Aegis-owned files
        # without nuking the user's own slash commands in the same dir.
        if SLASH_COMMANDS_MARKER not in rendered:
            rendered = f"{SLASH_COMMANDS_MARKER}\n{rendered}"
        target = SLASH_COMMANDS_DST / tmpl.name
        if not dry_run:
            target.write_text(rendered, encoding="utf-8")
        written.append(target.name)
    return len(written), written


def _uninstall_slash_commands(*, dry_run: bool = False) -> tuple[int, list[str]]:
    """Remove only Aegis-owned slash command files (those carrying the
    ``SLASH_COMMANDS_MARKER`` marker). User-owned files in the same
    directory are preserved verbatim.
    """
    if not SLASH_COMMANDS_DST.exists():
        return 0, []
    removed: list[str] = []
    for f in sorted(SLASH_COMMANDS_DST.glob("aegis-*.md")):
        try:
            head = f.read_text(encoding="utf-8")[:200]
        except OSError:
            continue
        if SLASH_COMMANDS_MARKER not in head:
            continue
        if not dry_run:
            f.unlink()
        removed.append(f.name)
    return len(removed), removed


def _validate_plugin_manifest() -> tuple[bool, str]:
    """Return ``(ok, message)``. message is the version on success or a reason."""
    if not PLUGIN_MANIFEST.exists():
        return False, f"plugin manifest not found: {PLUGIN_MANIFEST}"
    try:
        manifest = json.loads(PLUGIN_MANIFEST.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        return False, f"plugin manifest is not valid JSON: {e}"
    if not isinstance(manifest, dict):
        return False, "plugin manifest must be a JSON object"
    name = manifest.get("name")
    version = manifest.get("version")
    if not name:
        return False, "plugin manifest missing 'name' field"
    if not version:
        return False, "plugin manifest missing 'version' field"
    return True, str(version)


VALID_LOCAL_JUDGES = ("dummy", "hybrid", "local-phi")
VALID_LOCAL_EMBEDDINGS = ("dummy", "bge-local")
VALID_LOCAL_PROFILES = ("free", "pro", "cloud")


# ── Profile resolution ───────────────────────────────────────────────


# Each profile sets baselines for (judge, embedding, advisor_enabled,
# advisor_provider) and lists model files to auto-download. Explicit
# `--judge` / `--embedding` flags override the profile baseline.
#
#   free   — Solo Free contract: 0 cloud calls, 0 model files. Today's
#            default behaviour, made explicit.
#   pro    — Real local intelligence: M13 attribution head + bge-local
#            embeddings + local-phi judge fallback. ~700 MB GGUF
#            download. Still 0 cloud calls.
#   cloud  — Maximum accuracy: same as pro plus Anthropic Haiku judge
#            for grey-zone calls. Requires ANTHROPIC_API_KEY at runtime;
#            cost is ~$0.50–2.00/month for typical solo dev usage.
_PROFILE_DEFAULTS: dict[str, dict[str, object]] = {
    "free": {
        "judge": "dummy",
        "embedding": "dummy",
        "advisor_enabled": False,
        "advisor_provider": None,
        "auto_pull": (),
    },
    "pro": {
        "judge": "hybrid",
        "embedding": "bge-local",
        "advisor_enabled": True,
        "advisor_provider": "heuristic",
        "auto_pull": ("bge-base-en", "llama-3.2-1b"),
    },
    "cloud": {
        "judge": "hybrid",
        "embedding": "bge-local",
        "advisor_enabled": True,
        "advisor_provider": "haiku",
        "auto_pull": ("bge-base-en", "llama-3.2-1b"),
    },
}


def _resolve_profile(
    profile: str,
    *,
    judge_arg: str | None,
    embedding_arg: str | None,
) -> dict[str, object]:
    """Apply profile baselines, letting explicit --judge / --embedding override.

    Returns dict with: judge, embedding, advisor_enabled, advisor_provider,
    auto_pull. The auto_pull tuple lists model spec names the install
    command should download before patching settings.json.
    """
    if profile not in _PROFILE_DEFAULTS:
        raise ValueError(
            f"--profile must be one of {VALID_LOCAL_PROFILES}, got {profile!r}"
        )
    base = dict(_PROFILE_DEFAULTS[profile])
    if judge_arg is not None:
        base["judge"] = judge_arg
    if embedding_arg is not None:
        base["embedding"] = embedding_arg
    return base


def _hook_python_executable() -> str:
    """Return the Python interpreter Claude Code should use for hooks.

    The Aegis hooks ``import numpy``, ``import pydantic``, etc., so they
    must run inside the project's venv — bare ``python3`` on macOS is
    typically system Python without our deps and will crash with
    ``ModuleNotFoundError: numpy`` the moment Claude Code fires the hook.

    Resolution order:

    1. ``<repo_root>/.venv/bin/python`` — the canonical ``uv``-managed
       venv. Present after ``uv sync``.
    2. ``sys.executable`` — the current interpreter (likely the venv's
       python when ``aegis`` is invoked via ``uv run aegis install``).
    3. ``"python3"`` — last-resort PATH lookup.
    """
    venv_py = PROJECT_ROOT / ".venv" / "bin" / "python"
    if venv_py.exists():
        return str(venv_py)
    if sys.executable and Path(sys.executable).exists():
        return sys.executable
    return "python3"


def _build_pretool_command(
    mode: str, *,
    judge: str = "dummy",
    embedding: str = "dummy",
    advisor_enabled: bool = False,
    advisor_provider: str | None = None,
) -> str:
    """Compose the shell command embedded into the PreToolUse hook.

    Sidecar mode uses the existing ``tools/aegis_hook.py`` (POST /evaluate);
    local mode uses ``tools/aegis_local_hook.py`` (in-process firewall) and
    pre-pends:

    * ``AEGIS_EMBEDDING_PROVIDER`` — ``dummy`` (SHA3 noise, no LLM, the
      Solo Free default until ``aegis pull-model --model bge-base-en``
      is run) or ``bge-local`` (real BGE encoder via llama-cpp).
    * ``AEGIS_EMBEDDING_MODEL_PATH`` — set when ``embedding=bge-local``;
      points at ``./models/bge-base-en-v1.5-q4_k_m.gguf``.
    * ``AEGIS_JUDGE_PROVIDER`` — ``dummy`` (keyword-only), ``local-phi``
      (real local LLM), or ``hybrid`` (M13 cascade with local-phi as
      Tier 2). Required for AWS-secret + loop scenarios to BLOCK.
    * ``AEGIS_JUDGE_MODEL_PATH`` — set when ``judge`` ∈ {local-phi,
      hybrid}; points at ``./models/Llama-3.2-1B-Instruct-Q4_K_M.gguf``.
    * ``AEGIS_POLICY_DIR``  — absolute path to ``policies/`` so step310
      can find ``sensitive_paths.json`` from any cwd.
    * ``PYTHONPATH``  — absolute path to ``src/`` so the spawned
      subprocess resolves the ``aegis`` package without ``uv sync``.
    """
    if mode == "local":
        if judge not in VALID_LOCAL_JUDGES:
            raise ValueError(
                f"--judge must be one of {VALID_LOCAL_JUDGES}, got {judge!r}"
            )
        if embedding not in VALID_LOCAL_EMBEDDINGS:
            raise ValueError(
                f"--embedding must be one of {VALID_LOCAL_EMBEDDINGS}, "
                f"got {embedding!r}"
            )
        py = _hook_python_executable()
        # ── Personal Solo Free defaults (PR #101 anti-self-DoS) ─────
        # AEGIS_APPROVE_AS_BLOCK=0  — REQUIRE_APPROVAL surfaces as an
        #     informational stderr line instead of a hard BLOCK. Local-
        #     mode users haven't configured an approval queue, so a
        #     hard BLOCK on every gray-zone call (cost gate, loop
        #     detector, etc.) reduces to a self-DoS the moment the
        #     audit chain accumulates traffic.
        # AEGIS_TOKEN_BUDGET=99999999 — the cost gate is meant for
        #     enterprise per-tenant budget enforcement (Sidecar). On
        #     a Solo Free laptop nobody sets a budget, so the default
        #     $1 ceiling crosses within hours of normal usage and
        #     starts blocking every tool. Disabled by setting a
        #     wildcard-large ceiling. Sidecar deployments still
        #     enforce real budgets via the budget store.
        # Both defaults are designed so a Personal install NEVER
        # locks out its own user. Sidecar mode keeps the strict
        # defaults (REQUIRE_APPROVAL → BLOCK, real budgets).
        prefix = (
            f"AEGIS_APPROVE_AS_BLOCK=0 "
            f"AEGIS_TOKEN_BUDGET=99999999 "
            f"AEGIS_EMBEDDING_PROVIDER={embedding} "
            f"AEGIS_JUDGE_PROVIDER={judge} "
            f"AEGIS_POLICY_DIR={POLICIES_DIR} "
            f"PYTHONPATH={SRC_DIR}"
        )
        # PR-A: profile=pro / cloud activates the cost / performance /
        # security advisor pipeline (default is OFF — see
        # tools/aegis_local_hook.py:56). When enabled, the per-decision
        # advisor evaluates non-ALLOW verdicts and stamps
        # explain.action_advice into the audit log.
        if advisor_enabled:
            prefix = f"{prefix} AEGIS_ADVISOR_ENABLED=1"
            if advisor_provider:
                prefix = f"{prefix} AEGIS_ADVISOR_PROVIDER={advisor_provider}"
        # When local-phi/hybrid is requested, embed AEGIS_JUDGE_MODEL_PATH
        # so the judge enters real mode without manual .env editing. If
        # the file is absent, LocalPhiJudge falls back to stub mode and
        # emits a clear "model file does not exist" reason; the install
        # pre-flight check (_gguf_status_for_install) surfaces this so
        # the user catches it before restarting Claude Code.
        if judge in ("local-phi", "hybrid"):
            from aegis.judge.model_registry import (
                default_model,
                model_target_path,
            )
            target = model_target_path(default_model(), MODELS_DIR)
            prefix = f"{prefix} AEGIS_JUDGE_MODEL_PATH={target}"
        # Same idea for the embedding side: bge-local needs the GGUF
        # path. Falls back to dummy at runtime if the file is missing.
        if embedding == "bge-local":
            from aegis.judge.model_registry import (
                default_embedding_model,
                model_target_path,
            )
            etarget = model_target_path(default_embedding_model(), MODELS_DIR)
            prefix = f"{prefix} AEGIS_EMBEDDING_MODEL_PATH={etarget}"
        return f"{prefix} {py} {LOCAL_HOOK_SCRIPT}"
    return f"{_hook_python_executable()} {HOOK_SCRIPT}"


def _gguf_status_for_install(judge: str) -> tuple[bool, str]:
    """Pre-flight check called by ``cmd_install`` when judge needs a GGUF.

    Returns ``(ok, message)``. ``ok=False`` means the install will
    succeed but the hook will fall back to stub mode at runtime — we
    print the message as a yellow warning, not a hard error, so the
    user still gets a working install (just with degraded judging).
    """
    from aegis.judge.model_registry import default_model, model_target_path

    target = model_target_path(default_model(), MODELS_DIR)
    if not target.exists():
        return False, (
            f"GGUF not found at {target}. The hook will fall back to "
            f"stub mode (M13 attribution head only — no real LLM). "
            f"Run `uv run aegis pull-model` to download "
            f"{default_model().name} (~{default_model().size_mb} MB) "
            f"and `uv sync --extra local-llm` for llama-cpp-python."
        )
    llama_ok, llama_msg = _check_llama_cpp_installed()
    if not llama_ok:
        return False, llama_msg
    return True, f"local-sLLM ready: {target} (real LLM verdicts active)"


def _bge_status_for_install(embedding: str) -> tuple[bool, str]:
    """Pre-flight check for ``--embedding bge-local`` — GGUF + llama-cpp.

    Same contract as ``_gguf_status_for_install``: install never blocks,
    a missing model just degrades to dummy embedding at runtime.
    """
    from aegis.judge.model_registry import (
        default_embedding_model,
        model_target_path,
    )

    target = model_target_path(default_embedding_model(), MODELS_DIR)
    if not target.exists():
        return False, (
            f"Embedding GGUF not found at {target}. ATV "
            f"agent_state_embedding will fall back to deterministic "
            f"SHA3 noise (semantic similarity disabled). Run "
            f"`uv run aegis pull-model --model bge-base-en` to download "
            f"the {default_embedding_model().name} encoder "
            f"(~{default_embedding_model().size_mb} MB)."
        )
    llama_ok, llama_msg = _check_llama_cpp_installed()
    if not llama_ok:
        return False, llama_msg
    return True, (
        f"local embedding ready: {target.name} "
        f"({default_embedding_model().embedding_dim}-D real BGE encoder)"
    )


def _build_posttool_command(mode: str) -> str:
    """Compose the shell command embedded into the PostToolUse hook.

    Both modes share the same ``tools/hooks/post_tool.py`` script —
    PostToolUse closes the ATMU intent record (2PC phase 2) and feeds
    /tool-outcome (sidecar) or just appends to the local audit chain
    (local). We pre-pend ``PYTHONPATH=src`` so the subprocess can
    ``import aegis.audit.local_chain`` without ``uv sync`` first, and
    use the venv's interpreter so deps resolve.
    """
    py = _hook_python_executable()
    if mode == "local":
        return f"PYTHONPATH={SRC_DIR} {py} {POST_HOOK_SCRIPT}"
    return f"{py} {POST_HOOK_SCRIPT}"


def _pretool_hook_marker(mode: str) -> str:
    """Substring searched in existing settings to detect a prior install."""
    return str(LOCAL_HOOK_SCRIPT) if mode == "local" else str(HOOK_SCRIPT)


# Substrings that identify any Aegis-owned hook entry in settings.json,
# regardless of which repo path / judge / mode wrote it. ``--force``
# uses these to evict stale entries before installing fresh ones, so
# users who moved the repo or switched modes don't accumulate dead
# command lines.
_AEGIS_HOOK_FINGERPRINTS = (
    "aegis_local_hook.py",
    "aegis_hook.py",
    "tools/hooks/post_tool.py",
    "tools/hooks/session_end.py",
    "tools/hooks/pre_compact.py",
    "tools/hooks/user_prompt_submit.py",
    "tools/hooks/session_start.py",     # Sprint 1 PR4
)


def _is_aegis_owned(command: str) -> bool:
    return any(fp in command for fp in _AEGIS_HOOK_FINGERPRINTS)


def _drop_aegis_entries(hooks_section: dict[str, list[dict[str, Any]]]) -> int:
    """Remove every Aegis-owned hook entry from ``hooks_section``.

    Returns the number of entries dropped. Used by ``aegis install
    --force`` so stale entries (old repo paths, wrong python
    interpreter, leftover from a removed v2.0 install, etc.) get
    evicted instead of accumulating.
    """
    n_dropped = 0
    for stage in (
        "PreToolUse", "PostToolUse", "Stop",
        "PreCompact", "UserPromptSubmit",   # PR #47
        "SessionStart",                      # PR4 (Sprint 1)
    ):
        entries = hooks_section.get(stage, [])
        keep: list[dict[str, Any]] = []
        for entry in entries:
            ours = any(
                _is_aegis_owned(h.get("command", ""))
                for h in entry.get("hooks", [])
            )
            if ours:
                n_dropped += 1
            else:
                keep.append(entry)
        hooks_section[stage] = keep
    return n_dropped


def _default_baseline_path() -> Path:
    """Where ``aegis baseline init`` writes by default — repo-local."""
    return Path.cwd() / ".aegis" / "instruction_baseline.json"


def cmd_baseline(args: argparse.Namespace) -> int:
    """v2.2.1 Day-1 #3 — manage the instruction baseline manifest.

    Three subactions:

    * ``aegis baseline init``     — snapshot current CLAUDE.md /
      AGENTS.md / .mcp.json / plugin & skill manifests into
      ``.aegis/instruction_baseline.json``. Subsequent PreToolUse
      calls verify against this file.
    * ``aegis baseline status``   — show the diff between the live
      tree and the baseline (additions, removals, modifications).
    * ``aegis baseline reattest`` — re-snapshot and overwrite, after
      a reviewed change. Drops the firewall's in-process cache so
      the next PreToolUse picks up the new manifest.
    """
    from aegis.instruction_baseline import (
        DEFAULT_MODEL_WEIGHT_PATTERNS,
        diff_baseline,
        load_baseline,
        snapshot,
        write_baseline,
    )

    root = Path(args.root).resolve() if args.root else Path.cwd()
    baseline_path = (
        Path(args.baseline) if args.baseline else _default_baseline_path()
    )

    # PR-E (Local OSS LLM track) — resolve which model-weight pattern
    # set to use. None disables; explicit list overrides default.
    weight_patterns: tuple[str, ...] | None
    if getattr(args, "model_weight_paths", None):
        weight_patterns = tuple(args.model_weight_paths)
    elif getattr(args, "include_model_weights", False):
        weight_patterns = DEFAULT_MODEL_WEIGHT_PATTERNS
    else:
        weight_patterns = None

    if args.action == "init":
        if baseline_path.exists() and not args.force:
            print(
                _yellow(
                    f"baseline already exists at {baseline_path} — re-run with "
                    "--force to overwrite, or use `aegis baseline reattest`."
                )
            )
            return 1
        bl = snapshot(root, model_weight_patterns=weight_patterns)
        write_baseline(bl, baseline_path)
        print(_green(f"\u2713 instruction baseline written → {baseline_path}"))
        print(f"  root:  {root}")
        print(f"  files: {len(bl.files)} tracked")
        for rel in sorted(bl.files):
            print(f"    {bl.files[rel][:12]}…  {rel}")
        if bl.model_weights:
            print(f"  model weights: {len(bl.model_weights)} tracked")
            for rel in sorted(bl.model_weights):
                print(f"    {bl.model_weights[rel][:12]}…  {rel}")
        print()
        print(
            f"Set AEGIS_INSTRUCTION_BASELINE_PATH={baseline_path} in your env "
            "to enable step309 drift checking on every PreToolUse."
        )
        return 0

    if args.action == "status":
        if not baseline_path.exists():
            print(
                _red(
                    f"no baseline at {baseline_path}. Run "
                    "`aegis baseline init` first."
                )
            )
            return 1
        bl = load_baseline(baseline_path)
        report = diff_baseline(bl, root)
        if report.is_clean:
            n_files = len(bl.files)
            n_weights = len(bl.model_weights)
            extra = (
                f" + {n_weights} model weights" if n_weights else ""
            )
            print(_green(
                f"\u2713 baseline intact ({n_files} files tracked{extra})"
            ))
            return 0
        # PR-E \u2014 surface model-weight drift first (higher severity).
        if report.has_model_drift:
            print(_red(
                f"\u2717 model-weight drift detected: {report.summary()}"
            ))
            for rel in report.added_weights:
                print(f"  + {rel}  (NEW model file)")
            for rel in report.removed_weights:
                print(f"  - {rel}  (REMOVED model file)")
            for rel, old, new in report.modified_weights:
                print(f"  ~ {rel}  (MODEL WEIGHT CHANGED)")
                print(f"      was: {old[:16]}\u2026")
                print(f"      now: {new[:16]}\u2026")
            print()
        if not report.has_instruction_drift:
            print(
                "Until reviewed, every PreToolUse is BLOCKed by step309. "
                "If the change is intentional, run `aegis baseline reattest"
                "` (add `--include-model-weights` to also re-attest model "
                "artifacts)."
            )
            return 1
        print(_red(f"\u2717 instruction drift detected: {report.summary()}"))
        for rel in report.added:
            print(f"  + {rel}  (NEW)")
        for rel in report.removed:
            print(f"  - {rel}  (REMOVED)")
        for rel, old, new in report.modified:
            print(f"  ~ {rel}")
            print(f"      was: {old[:16]}…")
            print(f"      now: {new[:16]}…")
        print()
        print(
            "Until reviewed, every PreToolUse is BLOCKed by step309. "
            "If the change is intentional, run `aegis baseline reattest"
            "` (add `--include-model-weights` to also re-attest model "
            "artifacts)."
        )
        return 1

    if args.action == "reattest":
        bl = snapshot(root, model_weight_patterns=weight_patterns)
        write_baseline(bl, baseline_path)
        from aegis.firewall.step309_instruction_drift import reset_baseline_cache

        reset_baseline_cache()
        # PR-E \u2014 re-attest also picks up model-weight changes when
        # weight_patterns is non-None (i.e., user passed
        # --include-model-weights).
        print(_green(f"\u2713 baseline re-attested → {baseline_path}"))
        print(f"  files: {len(bl.files)} tracked")
        if bl.model_weights:
            print(f"  model weights: {len(bl.model_weights)} tracked")
        return 0

    return 2


def _extract_audit_fields(rec: dict[str, object]) -> dict[str, object]:
    """Pull (decision, reason, tool, ts_ns) out of an audit record.

    Two schemas are supported:

    * **Local hook** (``tools/aegis_local_hook.py``) — flat top-level
      fields ``{decision, reason, tool, aid, ts_ns, prev_hash, this_hash}``.
    * **Sidecar service** (``aegis.audit.jsonl_store``) — nested under
      ``payload.header`` with ``decision`` AND ``tool_name``; ``reason``
      lives in the SQLite ``audit.sqlite`` companion (not the JSONL).
      We surface tool_name as a fallback so the verbose `--verbose`
      table is still useful even when reason is absent.

    Returns a normalised dict the report aggregator can consume.
    """
    decision = rec.get("decision")
    if not decision:
        # Sidecar nested path
        payload = rec.get("payload")
        if isinstance(payload, dict):
            header = payload.get("header")
            if isinstance(header, dict):
                decision = header.get("decision")
    decision_str = str(decision or "").upper()

    reason = rec.get("reason")
    if not reason:
        payload = rec.get("payload")
        if isinstance(payload, dict):
            header = payload.get("header")
            if isinstance(header, dict):
                reason = header.get("reason")
    reason_str = str(reason or "").lower()

    tool = rec.get("tool") or rec.get("tool_name")
    if not tool:
        payload = rec.get("payload")
        if isinstance(payload, dict):
            header = payload.get("header")
            if isinstance(header, dict):
                tool = header.get("tool_name")
    tool_str = str(tool or "?")

    ts = rec.get("ts_ns")
    if ts is None:
        payload = rec.get("payload")
        if isinstance(payload, dict):
            header = payload.get("header")
            if isinstance(header, dict):
                ts = header.get("timestamp_ns")
            if ts is None:
                ts = payload.get("signed_at_ns")
    try:
        ts_int = int(ts or 0)
    except (TypeError, ValueError):
        ts_int = 0

    aid = rec.get("aid")
    if not aid:
        payload = rec.get("payload")
        if isinstance(payload, dict):
            header = payload.get("header")
            if isinstance(header, dict):
                aid = header.get("aid") or header.get("session_id")
    aid_str = str(aid or "")

    # PR-D — surface channel from both flat (local hook) and nested
    # (sidecar payload.header) audit-record schemas.
    channel = rec.get("channel")
    if not channel:
        payload = rec.get("payload")
        if isinstance(payload, dict):
            header = payload.get("header")
            if isinstance(header, dict):
                channel = header.get("channel")
    channel_str = str(channel or "")

    # PR-F — surface provider for `aegis report --by-provider` and the
    # provider-drift advisor. Same dual-schema lookup as channel.
    provider = rec.get("provider")
    if not provider:
        payload = rec.get("payload")
        if isinstance(payload, dict):
            header = payload.get("header")
            if isinstance(header, dict):
                provider = header.get("provider")
    provider_str = str(provider or "")

    return {
        "decision": decision_str,
        "reason": reason_str,
        "tool": tool_str,
        "ts_ns": ts_int,
        "aid": aid_str,
        "channel": channel_str,
        "provider": provider_str,
    }


def _cmd_report_explain(
    audit_path: Path, target: str, *, as_json: bool = False,
) -> int:
    """Render a single decision's full explanation block.

    ``target`` is a trace_id prefix (any unique starting substring is
    accepted) or the literal ``"LAST"`` / ``"last"`` for the most-
    recent record. Reads the local audit JSONL line-by-line — no need
    to load the full file into memory.

    The renderer pulls these from the record's ``explain`` block (added
    in the same PR that introduced this command):

    1. Decision header (verdict + reason + latency)
    2. Firewall step traces (filtered to non-trivial)
    3. M13 attribution top contributors
    4. RAG retrieval (case count + top cosine + top label + top text)
    5. Session drift snapshot (current + max + n_calls)
    6. ATV fingerprint (dim + SHA3) for replay verification

    Records written before this enrichment landed have no ``explain``
    block; we degrade gracefully to "Decision header only" with a
    yellow note.

    ``as_json=True`` skips the human-readable rendering and writes a
    machine-readable JSON object to stdout (one line, the audit
    record itself plus its embedded ``explain`` block). This is the
    surface CI / jq pipelines / `aegis report --explain LAST --json`
    consume — schema is just the audit record, stable across
    versions, easy to diff.
    """
    target_lc = target.strip().lower()
    want_last = target_lc in ("last", "*")
    found: dict | None = None
    last_record: dict | None = None

    with audit_path.open(encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            # Only PreToolUse decision records carry decision/reason —
            # skip PostToolUse forensic-only records.
            if "decision" not in rec:
                continue
            last_record = rec
            if not want_last:
                trace = str(rec.get("trace_id", ""))
                if trace.startswith(target):
                    found = rec
                    break

    if want_last:
        found = last_record
    if found is None:
        if as_json:
            # Machine-readable error envelope so CI doesn't choke on prose.
            print(json.dumps({
                "error": "not_found",
                "target": target,
                "audit_path": str(audit_path),
            }))
        elif want_last:
            print(_red(f"[report] no PreToolUse decisions in {audit_path}"))
        else:
            print(_red(
                f"[report] no record matches trace_id prefix {target!r} "
                f"in {audit_path}"
            ))
        return 1

    rec = found

    # JSON mode: dump the record (including its embedded explain block)
    # to stdout and bail. The schema is intentionally just "the audit
    # record" — no synthetic re-shaping — so users / CI can write
    # stable jq expressions.
    if as_json:
        print(json.dumps(rec, ensure_ascii=False, sort_keys=True))
        return 0

    decision = str(rec.get("decision", "?"))
    badge = {"ALLOW": "✅", "BLOCK": "⛔", "REQUIRE_APPROVAL": "⚠️"}.get(
        decision, "?"
    )
    print(f"AegisData Decision Explanation  {badge}")
    print("═══════════════════════════════════════════════════════════════════")
    print(f"  trace:     {str(rec.get('trace_id', ''))[:16]}…")
    print(f"  decision:  {decision}")
    print(f"  tool:      {rec.get('tool', '')}")
    print(f"  aid:       {rec.get('aid', '')}")
    print(f"  latency:   {rec.get('latency_ms', '?')} ms")
    reason = str(rec.get("reason", ""))
    if reason:
        # Wrap long reason on whitespace.
        print(f"  reason:    {reason[:300]}")
    print()

    explain = rec.get("explain") or {}
    if not explain:
        print(_yellow(
            "  (no explain block — record predates `aegis report --explain` "
            "or hook ran without enrichment)"
        ))
        return 0

    # ── Step traces ──────────────────────────────────────────────────
    traces = explain.get("step_traces") or {}
    if traces:
        print("  Firewall steps (non-trivial):")
        for k, v in traces.items():
            # Step keys come in as ``aegis.firewall.stepXXX_name.run`` —
            # the human-friendly name is the module piece, not "run".
            parts = str(k).split(".")
            short = parts[-2] if len(parts) >= 2 and parts[-1] == "run" else k
            print(f"    {short:<28}  {str(v)[:90]}")
        print()

    # ── M13 attribution ──────────────────────────────────────────────
    m13_top = explain.get("m13_top") or []
    m13_score = explain.get("m13_score")
    if m13_top:
        score_str = (
            f"  (combined score = {m13_score:.4f})"
            if isinstance(m13_score, (int, float)) else ""
        )
        print(f"  M13 attribution top contributors:{score_str}")
        for entry in m13_top[:5]:
            name = entry.get("subfield", "?")
            score = entry.get("score", 0.0)
            bar_len = int(round(min(1.0, max(0.0, float(score))) * 20))
            bar = "█" * bar_len + "·" * (20 - bar_len)
            print(f"    {name:<32}  [{bar}]  {float(score):.3f}")
        print()

    # ── RAG ──────────────────────────────────────────────────────────
    rag = explain.get("rag")
    if rag:
        print(
            f"  step340 RAG ({rag.get('n_retrieved', 0)} retrieved):"
        )
        print(
            f"    top cos:    {rag.get('top_cos', 0.0):.3f}\n"
            f"    top label:  {rag.get('top_label', '?')}\n"
            f"    top case:   {str(rag.get('top_text', ''))[:90]}"
        )
        print()

    # ── Session drift ────────────────────────────────────────────────
    drift = explain.get("session_drift")
    if drift:
        cur = drift.get("topic_drift", 0.0)
        mx = drift.get("max_drift", 0.0)
        n = drift.get("n_calls", 0)
        print("  Session behavioural drift:")
        print(
            f"    current topic_drift: {float(cur):.3f}  "
            f"max so far: {float(mx):.3f}  (call {n} of session)"
        )
        print()

    # ── ATV fingerprint ──────────────────────────────────────────────
    atv_dim = explain.get("atv_dim")
    atv_sha3 = explain.get("atv_sha3")
    if atv_dim or atv_sha3:
        print(
            f"  ATV: {atv_dim}-D, SHA3 = {str(atv_sha3 or '')[:24]}…  "
            f"(use `aegis verify-audit` to replay)"
        )
        print()

    return 0


def cmd_report(args: argparse.Namespace) -> int:
    """Print a 5-line Agent Risk Report for the most recent session.

    Reads ``~/.aegis/audit.jsonl`` (local mode) or the path passed via
    ``--audit``. Recognises both the local-hook flat schema and the
    sidecar-service nested ``payload.header`` schema; aggregates by
    decision + reason + redundant flag and prints an emoji-led summary
    mirroring the report shape from the must-install strategy doc:

        ✅ N safe tool calls auto-approved
        ⚠️ K high-risk actions required approval
        ⛔ B destructive commands blocked
        ⛔ P poisoned-instruction sources detected
        💸 D redundant calls deduplicated
        🔁 L potential loops aborted
        🧾 Full signed local audit: <path>

    Sidecar JSONL stores ``reason`` in the SQLite companion only, so
    the poisoned/loop/destructive split degrades to "all-destructive"
    for those records — counts remain accurate. For full-fidelity
    reasons against a sidecar audit, point at the running service's
    ``/forensic/replay`` endpoint instead.
    """
    audit_path = (
        Path(args.audit) if args.audit
        else Path.home() / ".aegis" / "audit.jsonl"
    )
    since_secs = _parse_window_secs(args.since) if args.since else None

    if not audit_path.exists():
        print(f"[report] no audit log at {audit_path}")
        print("        (start a Claude Code session with `aegis install --mode local`")
        print("         or `--mode sidecar` so the hook can append decisions.)")
        return 1

    # ── explain mode short-circuits the aggregate report ─────────────
    if getattr(args, "explain", None):
        return _cmd_report_explain(
            audit_path, args.explain,
            as_json=bool(getattr(args, "json", False)),
        )

    # ── Multi-agent + multi-LLM cross-grouping (Gap A) ───────────────
    # When the user passes BOTH --by-aid and --by-provider, OR the
    # explicit --by-aid-and-provider flag, route to the cross-table
    # view. This check runs BEFORE the single-dimension routes below,
    # so combined flags don't fall through.
    by_aid_and_provider = getattr(args, "by_aid_and_provider", False) or (
        getattr(args, "by_aid", False)
        and getattr(args, "by_provider", False)
    )
    if by_aid_and_provider:
        return _cmd_report_by_aid_and_provider(
            audit_path, since_secs,
            with_live=getattr(args, "with_live", False),
        )

    # ── PR-C: --by-aid multi-agent breakdown ─────────────────────────
    if getattr(args, "by_aid", False):
        return _cmd_report_by_aid(audit_path, since_secs)

    # ── PR-D: --by-channel OpenClaw multi-channel breakdown ──────────
    if getattr(args, "by_channel", False):
        return _cmd_report_by_channel(audit_path, since_secs)

    # ── PR-F: --by-provider OpenClaw multi-provider breakdown ────────
    if getattr(args, "by_provider", False):
        return _cmd_report_by_provider(audit_path, since_secs)

    cutoff_ns = int(time.time() - since_secs) * 1_000_000_000 if since_secs else 0

    n_total = 0
    n_safe = 0
    n_approval = 0
    n_block_destructive = 0
    n_block_poisoned = 0
    n_loop_aborted = 0
    n_redundant = 0
    n_sidecar_no_reason = 0
    by_reason: dict[str, int] = {}

    with audit_path.open(encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue

            fields = _extract_audit_fields(rec)
            ts = int(fields["ts_ns"])  # type: ignore[arg-type]
            if cutoff_ns and ts and ts < cutoff_ns:
                continue
            decision = str(fields["decision"])
            reason = str(fields["reason"])
            tool = str(fields["tool"])
            n_total += 1
            if not reason:
                n_sidecar_no_reason += 1

            if decision == "ALLOW":
                n_safe += 1
                if "redundant" in reason:
                    n_redundant += 1
            elif decision == "REQUIRE_APPROVAL":
                n_approval += 1
                if "loop" in reason or "step336" in reason:
                    n_loop_aborted += 1
            elif decision == "BLOCK":
                if "instruction_drift" in reason or "poisoned" in reason:
                    n_block_poisoned += 1
                else:
                    n_block_destructive += 1

            # Sidecar fallback: classify by tool so the verbose table is
            # still useful without fetching SQLite reason text.
            tag = reason[:60] if reason else f"{decision} {tool}".strip()
            by_reason[tag] = by_reason.get(tag, 0) + 1

    print("AegisData Agent Risk Report")
    print("===========================")
    if since_secs:
        print(f"  window:    last {args.since}")
    print(f"  audit log: {audit_path}  ({n_total} entries)")
    print()
    print(f"  ✅  {n_safe:>4} safe tool calls auto-approved")
    print(f"  ⚠️   {n_approval:>4} high-risk actions required approval")
    print(f"  ⛔  {n_block_destructive:>4} destructive commands blocked")
    print(f"  ⛔  {n_block_poisoned:>4} poisoned-instruction sources detected")
    print(f"  💸  {n_redundant:>4} redundant calls deduplicated")
    print(f"  🔁  {n_loop_aborted:>4} potential loops aborted")
    print(f"  🧾  Full signed local audit: {audit_path}")

    # Surface the sidecar JSONL limitation so users aren't surprised
    # the poisoned/loop split looks empty against a sidecar log.
    if n_sidecar_no_reason and n_sidecar_no_reason == n_total:
        print()
        print(_yellow(
            "Note: this audit log carries no `reason` text — looks like a "
            "sidecar service JSONL. Counts are accurate; for the "
            "destructive-vs-poisoned split, query the SQLite companion "
            "(`audit.sqlite`) or use `curl localhost:8000/forensic/replay`."
        ))

    if args.verbose and by_reason:
        print()
        print("  Top reasons (count × tag):")
        top = sorted(by_reason.items(), key=lambda kv: -kv[1])[:10]
        for tag, c in top:
            print(f"    {c:>4} × {tag}")

    return 0


def _cmd_report_by_aid(
    audit_path: Path, since_secs: int | None,
) -> int:
    """`aegis report --by-aid` — per-agent risk breakdown.

    Same emoji-led counters as the default report, but bucketed by
    aid (agent / session). Adds a [sidechain] tag to aids that have
    any sub-agent activity (Claude Code Task tool, OpenClaw nested
    plugin call). Designed for multi-agent ops where an operator
    needs to spot which specific aid is racking up BLOCKs.
    """
    cutoff_ns = (
        int(time.time() - since_secs) * 1_000_000_000 if since_secs else 0
    )

    # aid → counters
    by_aid: dict[str, dict[str, int]] = {}
    aids_with_sidechain: set[str] = set()
    n_total = 0

    with audit_path.open(encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue

            fields = _extract_audit_fields(rec)
            ts = int(fields["ts_ns"])  # type: ignore[arg-type]
            if cutoff_ns and ts and ts < cutoff_ns:
                continue
            decision = str(fields["decision"])
            reason = str(fields["reason"])
            aid = str(fields.get("aid", "")) or "(no-aid)"
            n_total += 1

            counters = by_aid.setdefault(aid, {
                "n_total": 0,
                "n_safe": 0,
                "n_approval": 0,
                "n_block_destructive": 0,
                "n_block_poisoned": 0,
                "n_loop_aborted": 0,
                "n_redundant": 0,
            })
            counters["n_total"] += 1

            if decision == "ALLOW":
                counters["n_safe"] += 1
                if "redundant" in reason:
                    counters["n_redundant"] += 1
            elif decision == "REQUIRE_APPROVAL":
                counters["n_approval"] += 1
                if "loop" in reason or "step336" in reason:
                    counters["n_loop_aborted"] += 1
            elif decision == "BLOCK":
                if "instruction_drift" in reason or "poisoned" in reason:
                    counters["n_block_poisoned"] += 1
                else:
                    counters["n_block_destructive"] += 1

            # PR-A multi-agent attribution — flag aids with any
            # sidechain (subagent) activity.
            if rec.get("is_sidechain"):
                aids_with_sidechain.add(aid)

    print("AegisData Agent Risk Report — by aid")
    print("====================================")
    if since_secs:
        # since_secs comes from _parse_window_secs which we don't
        # have the raw string for at this depth — just print seconds.
        print(f"  window:    last {since_secs // 60} min")
    print(f"  audit log: {audit_path}  ({n_total} entries, {len(by_aid)} aids)")
    print()

    if not by_aid:
        print(_yellow("  no records in window"))
        return 0

    # Sort aids by total severity (BLOCKs heavily weighted), so the
    # operator's eye lands on the most-suspicious aid first.
    def _severity(c: dict[str, int]) -> int:
        return (
            c["n_block_destructive"] * 100
            + c["n_block_poisoned"] * 100
            + c["n_approval"] * 10
            + c["n_total"]
        )

    sorted_aids = sorted(
        by_aid.items(), key=lambda kv: -_severity(kv[1]),
    )

    for aid, c in sorted_aids:
        flag = " [sidechain]" if aid in aids_with_sidechain else ""
        aid_display = aid[:32] + flag
        print(f"  ── {aid_display}")
        print(f"     ✅  {c['n_safe']:>4} safe tool calls auto-approved")
        print(f"     ⚠️   {c['n_approval']:>4} required approval")
        print(f"     ⛔  {c['n_block_destructive']:>4} destructive blocked")
        print(f"     ⛔  {c['n_block_poisoned']:>4} poisoned blocked")
        print(f"     💸  {c['n_redundant']:>4} redundant deduplicated")
        print(f"     🔁  {c['n_loop_aborted']:>4} loops aborted")
        print(f"     ─── total: {c['n_total']}")
        print()

    print(f"  🧾  Full signed local audit: {audit_path}")
    return 0


def _cmd_report_by_channel(
    audit_path: Path, since_secs: int | None,
) -> int:
    """`aegis report --by-channel` — OpenClaw multi-channel risk breakdown.

    Companion to ``--by-aid``. Channel is the input surface that
    triggered the tool call: ``telegram`` / ``discord`` / ``slack`` /
    ``cli`` / ``web`` / custom. Records without a channel field
    (Claude Code track) bucket under ``(no-channel)``.

    Useful when one OpenClaw deployment serves multiple chat
    platforms — surfaces per-channel BLOCK / approval rates so the
    operator spots a compromised channel quickly.
    """
    cutoff_ns = (
        int(time.time() - since_secs) * 1_000_000_000 if since_secs else 0
    )

    by_channel: dict[str, dict[str, int]] = {}
    n_total = 0

    with audit_path.open(encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue

            fields = _extract_audit_fields(rec)
            ts = int(fields["ts_ns"])  # type: ignore[arg-type]
            if cutoff_ns and ts and ts < cutoff_ns:
                continue
            decision = str(fields["decision"])
            reason = str(fields["reason"])
            channel = str(fields.get("channel", "")) or "(no-channel)"
            n_total += 1

            counters = by_channel.setdefault(channel, {
                "n_total": 0,
                "n_safe": 0,
                "n_approval": 0,
                "n_block_destructive": 0,
                "n_block_poisoned": 0,
                "n_loop_aborted": 0,
                "n_redundant": 0,
            })
            counters["n_total"] += 1

            if decision == "ALLOW":
                counters["n_safe"] += 1
                if "redundant" in reason:
                    counters["n_redundant"] += 1
            elif decision == "REQUIRE_APPROVAL":
                counters["n_approval"] += 1
                if "loop" in reason or "step336" in reason:
                    counters["n_loop_aborted"] += 1
            elif decision == "BLOCK":
                if "instruction_drift" in reason or "poisoned" in reason:
                    counters["n_block_poisoned"] += 1
                else:
                    counters["n_block_destructive"] += 1

    print("AegisData Agent Risk Report — by channel")
    print("========================================")
    if since_secs:
        print(f"  window:    last {since_secs // 60} min")
    print(
        f"  audit log: {audit_path}  ({n_total} entries, "
        f"{len(by_channel)} channels)"
    )
    print()

    if not by_channel:
        print(_yellow("  no records in window"))
        return 0

    # Severity weighting matches --by-aid for visual parity. The
    # channel with the most BLOCKs lands at the top.
    def _severity(c: dict[str, int]) -> int:
        return (
            c["n_block_destructive"] * 100
            + c["n_block_poisoned"] * 100
            + c["n_approval"] * 10
            + c["n_total"]
        )

    sorted_channels = sorted(
        by_channel.items(), key=lambda kv: -_severity(kv[1]),
    )

    for channel, c in sorted_channels:
        print(f"  ── {channel}")
        print(f"     ✅  {c['n_safe']:>4} safe tool calls auto-approved")
        print(f"     ⚠️   {c['n_approval']:>4} required approval")
        print(f"     ⛔  {c['n_block_destructive']:>4} destructive blocked")
        print(f"     ⛔  {c['n_block_poisoned']:>4} poisoned blocked")
        print(f"     💸  {c['n_redundant']:>4} redundant deduplicated")
        print(f"     🔁  {c['n_loop_aborted']:>4} loops aborted")
        print(f"     ─── total: {c['n_total']}")
        print()

    print(f"  🧾  Full signed local audit: {audit_path}")
    return 0


def _cmd_report_by_provider(
    audit_path: Path, since_secs: int | None,
) -> int:
    """`aegis report --by-provider` — OpenClaw multi-provider breakdown.

    Companion to ``--by-aid`` and ``--by-channel``. Provider is the
    LLM that produced the tool_use intent (anthropic-claude /
    openai-gpt-4o / google-gemini / local-llama-3.1-8b / etc.).
    Records without a provider field bucket under ``(no-provider)``.

    On top of the standard per-provider counters, this view computes
    a **provider-drift advisor signal** when 2+ providers are present
    in the window: if any provider's BLOCK rate diverges by ≥3× from
    the cross-provider median, surface a yellow banner.

    Why this matters: vendor switching is a real cost-cutting move
    operators make ("openai is cheaper this month, switch to it"),
    but RLHF / safety tuning differs across providers — the same
    prompt may BLOCK on Anthropic and ALLOW on OpenAI. The drift
    advisor catches "you saved 30% on cost but doubled your BLOCK
    rate — investigate" before it becomes a real incident.
    """
    cutoff_ns = (
        int(time.time() - since_secs) * 1_000_000_000 if since_secs else 0
    )

    by_provider: dict[str, dict[str, int]] = {}
    n_total = 0

    with audit_path.open(encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue

            fields = _extract_audit_fields(rec)
            ts = int(fields["ts_ns"])  # type: ignore[arg-type]
            if cutoff_ns and ts and ts < cutoff_ns:
                continue
            decision = str(fields["decision"])
            reason = str(fields["reason"])
            provider = str(fields.get("provider", "")) or "(no-provider)"
            n_total += 1

            counters = by_provider.setdefault(provider, {
                "n_total": 0,
                "n_safe": 0,
                "n_approval": 0,
                "n_block_destructive": 0,
                "n_block_poisoned": 0,
                "n_loop_aborted": 0,
                "n_redundant": 0,
            })
            counters["n_total"] += 1

            if decision == "ALLOW":
                counters["n_safe"] += 1
                if "redundant" in reason:
                    counters["n_redundant"] += 1
            elif decision == "REQUIRE_APPROVAL":
                counters["n_approval"] += 1
                if "loop" in reason or "step336" in reason:
                    counters["n_loop_aborted"] += 1
            elif decision == "BLOCK":
                if "instruction_drift" in reason or "poisoned" in reason:
                    counters["n_block_poisoned"] += 1
                else:
                    counters["n_block_destructive"] += 1

    print("AegisData Agent Risk Report — by provider")
    print("=========================================")
    if since_secs:
        print(f"  window:    last {since_secs // 60} min")
    print(
        f"  audit log: {audit_path}  ({n_total} entries, "
        f"{len(by_provider)} providers)"
    )
    print()

    if not by_provider:
        print(_yellow("  no records in window"))
        return 0

    # PR-F drift advisor — only meaningful when 2+ real providers
    # are present. (no-provider) bucket is excluded from the
    # divergence calculation since it's a *missing-data* category,
    # not a real provider.
    drift_lines = _compute_provider_drift(by_provider)

    # Severity weighting — same as --by-aid for visual parity.
    def _severity(c: dict[str, int]) -> int:
        return (
            c["n_block_destructive"] * 100
            + c["n_block_poisoned"] * 100
            + c["n_approval"] * 10
            + c["n_total"]
        )

    sorted_providers = sorted(
        by_provider.items(), key=lambda kv: -_severity(kv[1]),
    )

    for provider, c in sorted_providers:
        block_rate = (
            (c["n_block_destructive"] + c["n_block_poisoned"])
            / max(1, c["n_total"]) * 100
        )
        print(f"  ── {provider}  (BLOCK rate: {block_rate:.1f}%)")
        print(f"     ✅  {c['n_safe']:>4} safe tool calls auto-approved")
        print(f"     ⚠️   {c['n_approval']:>4} required approval")
        print(f"     ⛔  {c['n_block_destructive']:>4} destructive blocked")
        print(f"     ⛔  {c['n_block_poisoned']:>4} poisoned blocked")
        print(f"     💸  {c['n_redundant']:>4} redundant deduplicated")
        print(f"     🔁  {c['n_loop_aborted']:>4} loops aborted")
        print(f"     ─── total: {c['n_total']}")
        print()

    if drift_lines:
        print(_yellow("⚠ Provider-divergence advisor:"))
        for line in drift_lines:
            print(f"  {line}")
        print()

    print(f"  🧾  Full signed local audit: {audit_path}")
    return 0


def _cmd_report_by_aid_and_provider(
    audit_path: Path, since_secs: int | None,
    *, with_live: bool = False,
) -> int:
    """`aegis report --by-aid-and-provider` — multi-agent + multi-LLM
    cross-grouping (Gap A).

    For each aid, prints a sub-block per provider it used. The single-
    dimension --by-aid view collapses across providers; this view
    keeps the dimensions separate, so you can see "Agent A's BLOCK
    rate on Claude vs Agent A's BLOCK rate on GPT" at a glance.

    Surfaces a *per-agent* provider-drift banner when an aid's BLOCK
    rate diverges by ≥3× across the providers it has used (similar
    to the global ``_compute_provider_drift`` advisor, but scoped to
    a single aid). This is the multi-LLM-per-multi-agent canary —
    flags the case where one agent's safety profile shifted after
    a vendor migration.

    Records without aid bucket under '(no-aid)'; without provider
    under '(no-provider)'. The bucket pair '(no-aid)' × '(no-
    provider)' is collapsed to a single row.

    Args:
        audit_path: Path to the local ``audit.jsonl``.
        since_secs: Optional time window (seconds back from now).
        with_live: Gap B (issue #145). When True and
            ``~/.aegis/inference.toml`` exists, also scrapes per-aid
            live metrics from the registry and prints a "live: KV=
            87% queue=2/0" line under each aid block. Cloud /
            unreachable / unregistered aids print "live: n/a".
    """
    cutoff_ns = (
        int(time.time() - since_secs) * 1_000_000_000 if since_secs else 0
    )

    # (aid, provider) → counter dict
    by_pair: dict[tuple[str, str], dict[str, int]] = {}
    n_total = 0

    with audit_path.open(encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue

            fields = _extract_audit_fields(rec)
            ts = int(fields["ts_ns"])  # type: ignore[arg-type]
            if cutoff_ns and ts and ts < cutoff_ns:
                continue
            decision = str(fields["decision"])
            reason = str(fields["reason"])
            aid = str(fields.get("aid", "")) or "(no-aid)"
            provider = str(fields.get("provider", "")) or "(no-provider)"
            n_total += 1

            counters = by_pair.setdefault((aid, provider), {
                "n_total": 0,
                "n_safe": 0,
                "n_approval": 0,
                "n_block_destructive": 0,
                "n_block_poisoned": 0,
                "n_loop_aborted": 0,
                "n_redundant": 0,
            })
            counters["n_total"] += 1

            if decision == "ALLOW":
                counters["n_safe"] += 1
                if "redundant" in reason:
                    counters["n_redundant"] += 1
            elif decision == "REQUIRE_APPROVAL":
                counters["n_approval"] += 1
                if "loop" in reason or "step336" in reason:
                    counters["n_loop_aborted"] += 1
            elif decision == "BLOCK":
                if "instruction_drift" in reason or "poisoned" in reason:
                    counters["n_block_poisoned"] += 1
                else:
                    counters["n_block_destructive"] += 1

    # PR-L Gap B (issue #145) — pre-fetch live metrics once if
    # --with-live is set so each aid block can render its row
    # without N separate scrape round-trips during the print loop.
    live_results: dict[str, Any] = {}
    live_registry_note: str | None = None
    if with_live:
        try:
            from aegis.inference import (
                InferenceRegistryError,
                load_registry,
                scrape_all,
            )

            try:
                registry = load_registry()
            except InferenceRegistryError as e:
                live_registry_note = (
                    f"--with-live: inference.toml malformed ({e}) "
                    "— skipping live cross-reference"
                )
            else:
                if registry.is_empty():
                    live_registry_note = (
                        "--with-live: no inference.toml found at "
                        f"{registry.source_path} — skipping live "
                        "cross-reference"
                    )
                else:
                    live_results = scrape_all(registry)
        except ImportError:
            live_registry_note = (
                "--with-live: aegis.inference module unavailable"
            )

    print("AegisData Agent Risk Report — by aid × provider")
    print("================================================")
    if since_secs:
        print(f"  window:    last {since_secs // 60} min")
    n_aids = len({aid for aid, _ in by_pair})
    n_providers = len({p for _, p in by_pair})
    print(
        f"  audit log: {audit_path}  ({n_total} entries, "
        f"{n_aids} aids × {n_providers} providers, "
        f"{len(by_pair)} pairs)"
    )
    if live_registry_note:
        print(f"  {_yellow(live_registry_note)}")
    print()

    if not by_pair:
        print(_yellow("  no records in window"))
        return 0

    # Group by aid, with providers nested under each aid.
    aids_to_providers: dict[str, list[tuple[str, dict[str, int]]]] = {}
    for (aid, provider), counters in by_pair.items():
        aids_to_providers.setdefault(aid, []).append((provider, counters))

    # Per-aid severity weighting (sum across all providers used).
    def _aid_severity(providers: list[tuple[str, dict[str, int]]]) -> int:
        total = 0
        for _, c in providers:
            total += (
                c["n_block_destructive"] * 100
                + c["n_block_poisoned"] * 100
                + c["n_approval"] * 10
                + c["n_total"]
            )
        return total

    sorted_aids = sorted(
        aids_to_providers.items(),
        key=lambda kv: -_aid_severity(kv[1]),
    )

    # Track per-aid drift advisor lines for the bottom banner.
    drift_alerts: list[str] = []

    for aid, providers in sorted_aids:
        # Sort providers within the aid by call count desc.
        providers_sorted = sorted(
            providers, key=lambda kv: -kv[1]["n_total"],
        )
        aid_total = sum(c["n_total"] for _, c in providers_sorted)
        n_provs = len(providers_sorted)
        prov_label = "provider" if n_provs == 1 else "providers"
        print(f"  ── {aid}  ({aid_total} calls across {n_provs} {prov_label})")
        for provider, c in providers_sorted:
            block_rate = (
                (c["n_block_destructive"] + c["n_block_poisoned"])
                / max(1, c["n_total"]) * 100
            )
            print(
                f"     • {provider:<30} {c['n_total']:>4} calls  "
                f"BLOCK {block_rate:>5.1f}%  "
                f"(✅{c['n_safe']:>3} ⚠️{c['n_approval']:>3} "
                f"⛔{c['n_block_destructive'] + c['n_block_poisoned']:>3})"
            )

        # Per-aid drift detection — only when this aid used ≥2 real
        # providers (excluding (no-provider)) with enough samples.
        real_providers = [
            (p, c) for p, c in providers_sorted
            if p != "(no-provider)" and c["n_total"] >= 5
        ]
        if len(real_providers) >= 2:
            rates = [
                (p, (c["n_block_destructive"] + c["n_block_poisoned"])
                 / max(1, c["n_total"]))
                for p, c in real_providers
            ]
            nonzero = [r for _, r in rates if r > 0]
            if nonzero:
                max_rate = max(rates, key=lambda kv: kv[1])
                min_rate = min(rates, key=lambda kv: kv[1])
                if min_rate[1] > 0:
                    ratio = max_rate[1] / min_rate[1]
                    if ratio >= 3.0:
                        drift_alerts.append(
                            f"{aid}: {max_rate[0]} BLOCKs at "
                            f"{max_rate[1] * 100:.1f}% vs {min_rate[0]} "
                            f"at {min_rate[1] * 100:.1f}% — "
                            f"{ratio:.1f}× divergence"
                        )
                else:
                    # One provider has 0% BLOCK while others don't —
                    # the zero-block one is the suspicious side
                    # (peers BLOCK, this one doesn't).
                    suspect = min_rate[0]
                    drift_alerts.append(
                        f"{aid}: {suspect} BLOCKs at 0.0% while peer "
                        f"providers BLOCK at up to "
                        f"{max_rate[1] * 100:.1f}% — investigate"
                    )

        # PR-L Gap B — live cross-reference under each aid block.
        if with_live and live_results:
            from aegis.inference import (
                EndpointSkipped,
                EndpointUnreachable,
                InferenceMetrics,
            )

            r = live_results.get(aid)
            if r is None:
                print("     live: not in registry")
            elif isinstance(r, InferenceMetrics):
                print(
                    f"     live: KV={r.kv_cache_used_pct * 100:.1f}%  "
                    f"queue={r.requests_running}/{r.requests_waiting}  "
                    f"band={r.kv_cache_pressure_band()}"
                )
            elif isinstance(r, EndpointUnreachable):
                print(
                    f"     live: {_red('unreachable')} ({r.reason[:50]})"
                )
            else:
                assert isinstance(r, EndpointSkipped)
                print(
                    f"     live: {_yellow('skipped')} ({r.reason[:50]})"
                )
        print()

    if drift_alerts:
        print(_yellow(
            "⚠ Per-agent provider-divergence advisor "
            "(safety profile shift after vendor migration):"
        ))
        for line in drift_alerts:
            print(f"  {line}")
        print()

    print(f"  🧾  Full signed local audit: {audit_path}")
    return 0


def _compute_provider_drift(
    by_provider: dict[str, dict[str, int]],
    *,
    min_calls_per_provider: int = 5,
    divergence_multiplier: float = 3.0,
) -> list[str]:
    """PR-F provider-drift advisor (CLI side).

    Compares per-provider BLOCK rates pairwise and surfaces outliers
    when ``max_rate / min_rate >= divergence_multiplier``. Returns
    human-readable lines (caller prints them).

    Stays silent when:

    * Only one real provider (nothing to compare).
    * Any candidate provider has < ``min_calls_per_provider`` calls
      (sample size too small for meaningful comparison).
    * The max/min ratio across providers is below the multiplier.

    The pairwise max/min comparison handles the n=2 case correctly
    (median-vs-individual collapses into "always 1×" for n=2). For
    n>=3 it surfaces the same outliers a median-based detector would.
    """
    real_providers = {
        name: c for name, c in by_provider.items()
        if name != "(no-provider)" and c["n_total"] >= min_calls_per_provider
    }
    if len(real_providers) < 2:
        return []

    # BLOCK rate per provider (destructive + poisoned out of total).
    rates: dict[str, float] = {}
    for name, c in real_providers.items():
        n_block = c["n_block_destructive"] + c["n_block_poisoned"]
        rates[name] = n_block / max(1, c["n_total"])

    max_rate = max(rates.values())
    nonzero_rates = [r for r in rates.values() if r > 0]

    # Edge case: nobody blocked. Nothing meaningful to flag.
    if max_rate <= 0:
        return []

    # Edge case: some providers have 0% but at least one has > 0%.
    # The zero-block provider is the suspicious one — its peers are
    # BLOCKing but it isn't, suggesting it may be skipping safety
    # checks the peers enforce.
    if len(nonzero_rates) < len(rates):
        zero_block_providers = [
            name for name, r in rates.items() if r == 0
        ]
        peer_max = max_rate
        return [
            f"{name} BLOCKs at 0.0% while peers BLOCK at up to "
            f"{peer_max * 100:.1f}% — investigate provider-specific "
            "safety differences"
            for name in sorted(zero_block_providers)
        ]

    # All providers have non-zero rates. Compare max to min.
    min_rate = min(nonzero_rates)
    ratio = max_rate / max(min_rate, 1e-9)
    if ratio < divergence_multiplier:
        return []

    out: list[str] = []
    for name, rate in sorted(rates.items(), key=lambda kv: -kv[1]):
        # Flag the high outliers (rates >= multiplier × min_rate).
        if rate >= min_rate * divergence_multiplier and rate > min_rate:
            out.append(
                f"{name} BLOCKs at {rate * 100:.1f}% — "
                f"{rate / min_rate:.1f}× the lowest-blocking peer "
                f"({min_rate * 100:.1f}%); investigate provider-"
                "specific safety differences"
            )
        # And flag the low outliers (rates that the highs are >= multiplier of).
        elif max_rate >= rate * divergence_multiplier and rate < max_rate:
            out.append(
                f"{name} BLOCKs at {rate * 100:.1f}% — "
                f"only {rate / max_rate:.2f}× the highest-blocking "
                f"peer ({max_rate * 100:.1f}%); may be skipping "
                "safety checks peers enforce"
            )
    return out


# ─────────────────────────────────────────────────────────────────────
# PR-C: aegis forensic <selector> — single-session timeline
# ─────────────────────────────────────────────────────────────────────


def _format_ts(ts_ns: int) -> str:
    """ISO-style timestamp with millisecond precision for the timeline."""
    if ts_ns <= 0:
        return "                       "
    secs = ts_ns / 1_000_000_000
    import datetime as _dt

    dt = _dt.datetime.fromtimestamp(secs)
    return dt.strftime("%Y-%m-%d %H:%M:%S.") + f"{int((secs % 1) * 1000):03d}"


def _render_logit_metrics(payload: object) -> None:
    """PR-G — render logit-metrics block under a forensic timeline row.

    Stays silent when the audit record has no logit_metrics field
    (Cloud LLM tracks always; Local OSS LLM tracks until vLLM
    --return-logprobs is wired up). Renders three lines on a hit:
    confidence band, mean / min logprob, sample of low-confidence
    tokens.
    """
    if not isinstance(payload, dict) or not payload:
        return
    try:
        from aegis.inference.logit_metrics import LogitMetrics
        m = LogitMetrics.from_dict(payload)
    except (ImportError, ValueError, TypeError):
        return
    if m.n_tokens <= 0:
        return

    band = m.confidence_band()
    band_color = {
        "high": _green,
        "moderate": _green,
        "low": _yellow,
        "critical": _red,
        "unknown": _yellow,
    }[band]
    print(
        f"                  └─ logits: {m.n_tokens} tokens, "
        f"band {band_color(f'[{band}]')} "
        f"(mean={m.mean_logprob:.3f}, min={m.min_logprob:.3f})"
    )
    if m.n_low_confidence_tokens:
        print(
            f"                     low-confidence: "
            f"{m.n_low_confidence_tokens}/"
            f"{m.n_tokens} tokens "
            f"(< {m.low_confidence_threshold:.2f})"
        )
    if m.sample_low_confidence_tokens:
        sample_str = ", ".join(
            f"{tok!r}({lp:.2f})"
            for tok, lp in m.sample_low_confidence_tokens[:3]
        )
        print(f"                     sample:         {sample_str}")


def _decision_glyph(decision: str) -> str:
    return {
        "ALLOW": "✅ ALLOW           ",
        "BLOCK": "⛔ BLOCK           ",
        "REQUIRE_APPROVAL": "⚠️  REQUIRE_APPROVAL",
    }.get(decision, f"   {decision}")


def cmd_forensic(args: argparse.Namespace) -> int:
    """`aegis forensic <selector>` — postmortem timeline for one session.

    The launch-prep audit identified that there was no single command
    to answer "show me the full forensic timeline of session X."
    Operators stitched it from `aegis report --explain`, `tail | jq`,
    `aegis replay`, etc. This command reads the signed audit log once
    and emits a chronologically-ordered timeline with:

    * decision + tool + reason (one line per call)
    * step traces of interest (BLOCK / REQUIRE_APPROVAL / m13 hybrid)
    * top-3 step latencies if AEGIS_STEP_TIMING_ENABLED was on (PR-D)
    * advisor_gate signal if --profile pro/cloud was active (PR-A)
    * cumulative-cost cursor across the session

    Selector resolution:

    * Positional `<selector>`:
        - "LAST" / "last" → the AID of the most-recent record
        - exact match against `aid` field
        - prefix match against `trace_id` (handy for sharing a partial
          trace from a Slack message)

    * `--trace TRACE_ID` filters to a single call within the session.
    * `--audit PATH` overrides the default ~/.aegis/audit.jsonl.
    * `--since 7d` / `--limit N` narrow the window.
    * `--json` emits a structured object for downstream tooling.

    Read-only — never modifies audit.jsonl.
    """
    audit_path = (
        Path(args.audit) if args.audit
        else Path.home() / ".aegis" / "audit.jsonl"
    )
    if not audit_path.exists():
        print(f"[forensic] no audit log at {audit_path}")
        print(
            "        (start a Claude Code session with `aegis install "
            "--mode local` so the hook can append decisions.)"
        )
        return 1

    selector = args.selector
    trace_filter = getattr(args, "trace", None)
    since_secs = _parse_window_secs(args.since) if args.since else None
    cutoff_ns = (
        int(time.time() - since_secs) * 1_000_000_000 if since_secs else 0
    )
    limit = int(getattr(args, "limit", 0) or 0)

    # ── Pass 1: scan + filter, collect matching records in memory ────
    records: list[dict[str, Any]] = []
    last_aid: str | None = None
    with audit_path.open("r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = int(rec.get("ts_ns", 0) or 0)
            if cutoff_ns and ts < cutoff_ns:
                continue
            # A record carries either flat-style fields (local hook,
            # decision/aid/trace_id at top level) or nested
            # payload.header (sidecar). Only the local schema is the
            # forensic target — the sidecar already exposes
            # /forensic/replay endpoint.
            if "decision" not in rec:
                continue
            last_aid = rec.get("aid") or last_aid
            records.append(rec)

    if not records:
        print(_yellow(f"[forensic] no decision records in {audit_path}"))
        return 1

    # Resolve "last" → most-recent AID.
    if selector and selector.lower() == "last":
        if last_aid is None:
            print(_yellow("[forensic] could not infer LAST aid — log empty"))
            return 1
        selector = last_aid

    if not selector:
        print(_red("[forensic] selector required (positional or 'last')"),
              file=sys.stderr)
        return 2

    # ── Pass 2: filter to selector + optional trace ─────────────────
    matched = [
        r for r in records
        if r.get("aid") == selector
        or (
            isinstance(r.get("trace_id"), str)
            and str(r.get("trace_id", "")).startswith(selector)
        )
    ]
    if trace_filter:
        matched = [r for r in matched if r.get("trace_id") == trace_filter]
    if limit:
        matched = matched[-limit:]

    if not matched:
        print(_yellow(
            f"[forensic] no records match selector={selector!r}"
            + (f" --trace {trace_filter}" if trace_filter else "")
        ))
        return 1

    # ── Render ──────────────────────────────────────────────────────
    if getattr(args, "json", False):
        out = {
            "selector": selector,
            "trace_filter": trace_filter,
            "audit_path": str(audit_path),
            "count": len(matched),
            "first_ts_ns": int(matched[0].get("ts_ns", 0) or 0),
            "last_ts_ns": int(matched[-1].get("ts_ns", 0) or 0),
            "records": matched,
        }
        print(json.dumps(out, default=str))
        return 0

    # Human-readable timeline.
    first_ts = int(matched[0].get("ts_ns", 0) or 0)
    last_ts = int(matched[-1].get("ts_ns", 0) or 0)
    duration_s = (last_ts - first_ts) / 1_000_000_000 if last_ts > first_ts else 0

    print(_green(
        f"forensic timeline — selector={selector}, {len(matched)} record(s)"
    ))
    print(f"  audit:    {audit_path}")
    if first_ts and last_ts:
        print(
            f"  window:   {_format_ts(first_ts)}  →  {_format_ts(last_ts)}"
            f"  ({duration_s:,.1f}s)"
        )
    if trace_filter:
        print(f"  trace_id: {trace_filter}")
    print()

    counts: dict[str, int] = {"ALLOW": 0, "BLOCK": 0, "REQUIRE_APPROVAL": 0}
    total_latency_ms = 0.0
    for r in matched:
        d = str(r.get("decision", "?"))
        counts[d] = counts.get(d, 0) + 1
        with contextlib.suppress(TypeError, ValueError):
            total_latency_ms += float(r.get("latency_ms", 0) or 0)
        ts_str = _format_ts(int(r.get("ts_ns", 0) or 0))
        trace_short = str(r.get("trace_id", ""))[:8] or "--------"
        tool = str(r.get("tool", "?"))[:6].ljust(6)
        latency = float(r.get("latency_ms", 0) or 0)
        print(
            f"  {ts_str}  [trace={trace_short}…]  {tool}  "
            f"{_decision_glyph(d)}  ({latency:>6.1f} ms)"
        )
        reason = r.get("reason")
        if reason:
            reason_str = str(reason)
            if len(reason_str) > 90:
                reason_str = reason_str[:87] + "..."
            print(f"                  └─ reason: {reason_str}")
        explain = r.get("explain") or {}
        # PR-D step timings — if present, show top 3.
        timings = explain.get("step_timings_us") or {}
        if isinstance(timings, dict) and timings:
            top3 = sorted(
                timings.items(), key=lambda kv: -int(kv[1] or 0),
            )[:3]
            tt = "  ".join(
                f"{k.split('.')[-2]}={v}us" for k, v in top3
            )
            print(f"                  └─ slow steps: {tt}")
        # PR-A advisor gate — only when --profile pro/cloud is active.
        gate = explain.get("advisor_gate") or {}
        if isinstance(gate, dict) and gate.get("invoked"):
            gate_reason = str(gate.get("reason", ""))[:60]
            print(f"                  └─ advisor: invoked ({gate_reason})")

        # PR-G — logit-level forensic. Only renders when the user
        # passed --logits AND the audit record has the data (vLLM
        # --return-logprobs path, Local OSS LLM track only).
        if getattr(args, "logits", False):
            _render_logit_metrics(explain.get("logit_metrics"))

    # Summary footer.
    print()
    print(_green("summary:"))
    print(f"  events:           {len(matched)}")
    for d in ("ALLOW", "REQUIRE_APPROVAL", "BLOCK"):
        c = counts.get(d, 0)
        if c:
            pct = (100 * c) / len(matched)
            print(f"  {d:<17} {c:>4}  ({pct:.0f}%)")
    print(f"  total latency:    {total_latency_ms:,.1f} ms")
    print()
    print("artifacts (run separately):")
    print("  uv run aegis verify-audit         # chain integrity")
    print("  uv run aegis report               # session-level summary")
    return 0


# ─────────────────────────────────────────────────────────────────────
# PR-F: aegis advise — surface live advisor recommendations
# ─────────────────────────────────────────────────────────────────────


# Closed catalog from src/aegis/judge/advisor.py — eight advisor names
# bucketed into the three Personal MVP advisor categories. Update this
# table when new advisors are added to the judge prompt.
_ADVISOR_CATEGORIES: dict[str, str] = {
    # Cost domain
    "cost-optimizer":     "cost",
    "kv-cache-optimizer": "cost",
    "context-compactor":  "cost",
    # Performance domain
    "test-runner":     "performance",
    "loop-breaker":    "performance",
    "human-clarifier": "performance",
    # Security domain
    "security-reviewer":   "security",
    "permission-escalator": "security",
}

_PRIORITY_GLYPH: dict[str, str] = {
    "high":   "🔴 HIGH  ",
    "medium": "🟡 MEDIUM",
    "low":    "🟢 LOW   ",
}

_PRIORITY_RANK: dict[str, int] = {"high": 0, "medium": 1, "low": 2}


def _format_action_step(step: dict[str, Any]) -> str:
    """One-line render of an action_steps[] entry."""
    verb = str(step.get("verb", "?"))
    params = step.get("parameters") or {}
    impact = str(step.get("expected_impact", "")).strip()
    # Pick the most informative param keys per verb for a compact line.
    key_hints: list[str] = []
    if "turn_indices_rel" in params:
        idx = params["turn_indices_rel"]
        key_hints.append(f"turns={idx if isinstance(idx, list) else idx}")
        if "saved_dollars_estimate" in params:
            key_hints.append(f"saves ${params['saved_dollars_estimate']:.2f}")
    elif "from_model" in params:
        key_hints.append(
            f"{params.get('from_model')} → {params.get('to_model')}"
        )
    elif "from_tool" in params:
        key_hints.append(
            f"{params.get('from_tool')} → {params.get('to_tool')}"
        )
    elif "channel" in params:
        key_hints.append(f"channel={params.get('channel')}")
    elif "diagnostic_command" in params:
        key_hints.append(f"cmd={params.get('diagnostic_command')}")
    elif "clarifying_question" in params:
        q = str(params.get("clarifying_question", ""))
        key_hints.append(f"ask=\"{q[:50]}\"")
    rendered = " ".join(key_hints)
    head = f"{verb}" + (f"  {rendered}" if rendered else "")
    return f"{head}    [{impact}]" if impact else head


def cmd_advise(args: argparse.Namespace) -> int:
    """`aegis advise [selector]` — live advisor recommendations.

    The "no dummy product-ready" audit identified that --profile pro /
    cloud writes per-decision advisor recommendations into
    ``explain.action_advice`` of every audit record, but the user has
    no way to surface them — they only land in audit.jsonl. This
    command reads recent records and renders the recommendations
    grouped by domain (cost / performance / security), deduplicated
    and sorted by priority + recency.

    Selector defaults to "last" (most-recent AID's recommendations).
    Pass an AID for cross-session aggregation, or "all" to read every
    record in the window regardless of AID.

    Filters:
      --category cost|performance|security|all     (default: all)
      --priority high|medium|low|all                (default: all)
      --since DURATION                              (default: 7d)
      --limit N                                     (max recommendations
                                                     to render; default 20)

    Read-only — never modifies audit.jsonl.
    """
    audit_path = (
        Path(args.audit) if args.audit
        else Path.home() / ".aegis" / "audit.jsonl"
    )
    if not audit_path.exists():
        print(f"[advise] no audit log at {audit_path}")
        print(
            "        (start a Claude Code session with `aegis install "
            "--mode local --profile pro` so the advisor pipeline can run.)"
        )
        return 1

    selector = args.selector or "last"
    since_secs = _parse_window_secs(args.since) if args.since else 7 * 86400
    cutoff_ns = int(time.time() - since_secs) * 1_000_000_000
    cat_filter = (args.category or "all").lower()
    pri_filter = (args.priority or "all").lower()
    limit = int(args.limit or 20)

    # ── Pass 1: scan records, grab those with action_advice ─────────
    records: list[tuple[int, str, dict[str, Any]]] = []
    last_aid: str | None = None
    n_walked = 0
    with audit_path.open("r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = int(rec.get("ts_ns", 0) or 0)
            if ts < cutoff_ns:
                continue
            n_walked += 1
            aid = str(rec.get("aid", ""))
            if aid:
                last_aid = aid
            advice = (rec.get("explain") or {}).get("action_advice")
            if not isinstance(advice, dict):
                continue
            recs = advice.get("recommended_advisors") or []
            if not recs:
                continue
            records.append((ts, aid, advice))

    if not records:
        print(_yellow(
            f"[advise] no advisor recommendations in the last "
            f"{int(since_secs/86400)}d at {audit_path}"
        ))
        print(
            "         enable the advisor pipeline with "
            "`aegis install --mode local --profile pro` "
            "(or --profile cloud)."
        )
        return 1

    # Resolve selector.
    if selector.lower() == "last":
        if last_aid is None:
            print(_yellow("[advise] could not infer LAST aid"))
            return 1
        scope_aid: str | None = last_aid
    elif selector.lower() == "all":
        scope_aid = None
    else:
        scope_aid = selector

    if scope_aid is not None:
        records = [(ts, a, adv) for ts, a, adv in records if a == scope_aid]
        if not records:
            print(_yellow(
                f"[advise] no advisor records for selector={scope_aid!r}"
            ))
            return 1

    # ── Pass 2: aggregate recommendations across records ────────────
    # Dedupe key: (advisor, action sentence). Count occurrences, keep
    # the highest-priority + most-recent example so the rendered
    # priority and timestamp reflect the worst the user has seen.
    aggregated: dict[tuple[str, str], dict[str, Any]] = {}
    for ts, _aid, adv in records:
        for r in (adv.get("recommended_advisors") or []):
            advisor = str(r.get("advisor", "?"))
            action = str(r.get("action", ""))
            key = (advisor, action)
            slot = aggregated.setdefault(key, {
                "advisor": advisor,
                "action": action,
                "category": _ADVISOR_CATEGORIES.get(advisor, "other"),
                "priority": str(r.get("priority", "low")).lower(),
                "reasoning": str(r.get("reasoning", "")),
                "cited_signals": list(r.get("cited_signals", []) or []),
                "action_steps": list(r.get("action_steps", []) or []),
                "count": 0,
                "last_ts_ns": 0,
            })
            slot["count"] += 1
            slot["last_ts_ns"] = max(int(slot["last_ts_ns"]), ts)
            new_pri = str(r.get("priority", "low")).lower()
            if _PRIORITY_RANK.get(new_pri, 99) < _PRIORITY_RANK.get(
                str(slot["priority"]), 99,
            ):
                slot["priority"] = new_pri

    # Filter by category / priority.
    filtered: list[dict[str, Any]] = []
    for slot in aggregated.values():
        if cat_filter != "all" and slot["category"] != cat_filter:
            continue
        if pri_filter != "all" and slot["priority"] != pri_filter:
            continue
        filtered.append(slot)

    # Sort by priority asc (high first), then count desc, then recency desc.
    filtered.sort(key=lambda s: (
        _PRIORITY_RANK.get(str(s["priority"]), 99),
        -int(s["count"]),
        -int(s["last_ts_ns"]),
    ))
    if limit:
        filtered = filtered[:limit]

    # ── Render ──────────────────────────────────────────────────────
    if getattr(args, "json", False):
        out = {
            "selector": selector,
            "scope_aid": scope_aid,
            "since_secs": since_secs,
            "records_walked": n_walked,
            "advisor_invocations": len(records),
            "recommendations": filtered,
            "category_counts": {
                cat: sum(
                    1 for s in aggregated.values() if s["category"] == cat
                )
                for cat in ("cost", "performance", "security")
            },
        }
        print(json.dumps(out, default=str))
        return 0

    if not filtered:
        print(_yellow(
            f"[advise] no recommendations match category={cat_filter}, "
            f"priority={pri_filter}"
        ))
        return 0

    days = since_secs / 86400
    print(_green(
        f"aegis advise — {len(filtered)} recommendation(s) "
        f"from last {days:.0f}d"
    ))
    print(f"  audit:               {audit_path}")
    if scope_aid:
        print(f"  scope AID:           {scope_aid}")
    else:
        print("  scope:               all sessions in window")
    print(f"  records walked:      {n_walked:,}")
    print(f"  advisor invocations: {len(records):,}")
    print()

    cat_counts: dict[str, int] = {"cost": 0, "performance": 0, "security": 0}
    for slot in aggregated.values():
        cat = str(slot["category"])
        if cat in cat_counts:
            cat_counts[cat] += 1

    for slot in filtered:
        glyph = _PRIORITY_GLYPH.get(str(slot["priority"]), "         ")
        cat = str(slot["category"]).upper()
        count_part = (
            f" ×{slot['count']}" if int(slot["count"]) > 1 else ""
        )
        advisor = str(slot["advisor"])
        print(f"  {glyph}  [{cat:<11}]  {advisor}{count_part}")
        action_str = str(slot["action"])
        if len(action_str) > 96:
            action_str = action_str[:93] + "..."
        print(f"             \"{action_str}\"")
        reasoning = str(slot["reasoning"])
        if reasoning:
            if len(reasoning) > 100:
                reasoning = reasoning[:97] + "..."
            print(f"             Why: {reasoning}")
        signals = slot["cited_signals"]
        if isinstance(signals, list) and signals:
            sig_str = ", ".join(str(s) for s in signals[:5])
            print(f"             Cited: {sig_str}")
        steps = slot["action_steps"]
        if isinstance(steps, list) and steps:
            print("             Steps:")
            for step in steps[:3]:
                if isinstance(step, dict):
                    print(f"               · {_format_action_step(step)}")
        print()

    # Footer category breakdown.
    print(_green("by category:"))
    for cat, c in cat_counts.items():
        print(f"  {cat:<12} {c:>3} active")
    return 0


def cmd_cache_lint(args: argparse.Namespace) -> int:
    """`aegis cache-lint` — diagnose Anthropic prompt-cache breakage.

    Three modes (combine freely):

    * ``--transcript <path>`` — observe per-turn cache efficiency in
      a Claude Code transcript ``.jsonl`` and flag the turns where
      the cache broke (efficiency dropped ≥ ``--break-threshold`` pp
      vs the prior turn). Each break is attributed to a likely cause.

    * ``--system-prompt <path>`` — static lint of a system-prompt /
      tool-catalog string for the classic "broken cache"
      anti-patterns: dates, UUIDs, time-of-day strings, dynamic
      preludes ("Today is …", "Generated at …", etc.).

    * ``--compare-with <path>`` — closed-loop verification mode.
      Treats ``--transcript`` as the AFTER (post-fix) session and
      ``--compare-with`` as the BEFORE baseline. Diffs the two
      cache_lint reports and reports the realisation rate
      (observed savings ÷ projected savings).

    ``--json`` emits the full report (or comparison) as a single
    JSON document on stdout.
    """
    from aegis.performance.cache_lint import (
        DEFAULT_BREAK_THRESHOLD_PP,
        analyze_system_prompt,
        analyze_transcript,
        report_to_dict,
    )

    transcript = getattr(args, "transcript", None)
    compare_with = getattr(args, "compare_with", None)
    sys_prompt_path = getattr(args, "system_prompt", None)
    threshold = float(
        getattr(args, "break_threshold", DEFAULT_BREAK_THRESHOLD_PP)
    )
    as_json = bool(getattr(args, "json", False))

    sys_prompt_text: str | None = None
    if sys_prompt_path:
        p = Path(sys_prompt_path)
        if not p.is_file():
            print(f"[cache-lint] system-prompt file not found: {p}")
            return 2
        sys_prompt_text = p.read_text(encoding="utf-8")

    # Closed-loop comparison short-circuit.
    if compare_with:
        if not transcript:
            print(
                "[cache-lint] --compare-with requires --transcript "
                "(the AFTER session)"
            )
            return 2
        from aegis.performance.cache_lint_loop import (
            compare_transcripts,
            comparison_to_dict,
        )

        cmp = compare_transcripts(
            before_path=Path(compare_with),
            after_path=Path(transcript),
            after_system_prompt=sys_prompt_text,
            break_threshold_pp=threshold,
        )
        if as_json:
            print(json.dumps(comparison_to_dict(cmp), indent=2))
            return 0

        # Human-readable diff rendering.
        print("AegisData Prompt Cache Lint — Closed-Loop Comparison")
        print("=====================================================")
        print(f"  before:  {compare_with}")
        print(f"  after:   {transcript}")
        print()
        print(
            f"  before  hit_rate = "
            f"{cmp.before.observed_cache_hit_rate * 100:5.1f}%  "
            f"({len(cmp.before.breaks)} break(s), "
            f"{len(cmp.before.static_findings)} static finding(s))"
        )
        print(
            f"  after   hit_rate = "
            f"{cmp.after.observed_cache_hit_rate * 100:5.1f}%  "
            f"({len(cmp.after.breaks)} break(s), "
            f"{len(cmp.after.static_findings)} static finding(s))"
        )
        print()
        sign = "+" if cmp.cache_hit_rate_delta >= 0 else ""
        print(
            f"  Δ hit_rate:           {sign}"
            f"{cmp.cache_hit_rate_delta * 100:.1f} pp"
        )
        print(
            f"  tokens recovered:     "
            f"{cmp.token_savings_realised:+,} tokens / session"
        )
        print(
            f"  realisation rate:     "
            f"{cmp.realisation_rate * 100:.0f}%   "
            f"(realised ÷ projected)"
        )
        print()
        if cmp.breaks_resolved:
            print(f"✓ Resolved {len(cmp.breaks_resolved)} break(s):")
            for b in cmp.breaks_resolved:
                print(
                    f"    turn {b.turn_idx}  −{b.drop_pp:.0f} pp  "
                    f"({b.attribution[:60]})"
                )
        if cmp.breaks_persisting:
            print(
                f"~ Persisting {len(cmp.breaks_persisting)} break(s) "
                "(recommendation not applied):"
            )
            for b in cmp.breaks_persisting:
                print(f"    turn {b.turn_idx}  ({b.attribution[:60]})")
        if cmp.new_breaks:
            print(f"⚠ NEW {len(cmp.new_breaks)} break(s) (regression):")
            for b in cmp.new_breaks:
                print(f"    turn {b.turn_idx}  ({b.attribution[:60]})")
        if cmp.static_findings_resolved:
            print(
                f"✓ Removed {len(cmp.static_findings_resolved)} "
                "static-lint anti-pattern(s)"
            )
        if cmp.static_findings_persisting:
            print(
                f"~ {len(cmp.static_findings_persisting)} static "
                "anti-pattern(s) still present"
            )
        if cmp.new_static_findings:
            print(
                f"⚠ NEW {len(cmp.new_static_findings)} static "
                "anti-pattern(s) introduced"
            )
        return 0

    if transcript:
        report = analyze_transcript(
            Path(transcript),
            break_threshold_pp=threshold,
            system_prompt=sys_prompt_text,
        )
    elif sys_prompt_text is not None:
        # Static-only run: no transcript, just findings on the prompt.
        from aegis.performance.cache_lint import CacheLintReport

        report = CacheLintReport(
            transcript_path=None, n_turns=0,
            static_findings=analyze_system_prompt(sys_prompt_text),
        )
    else:
        print(
            "[cache-lint] usage: aegis cache-lint --transcript <path>"
            " [--system-prompt <path>]"
        )
        return 2

    if as_json:
        print(json.dumps(report_to_dict(report), indent=2))
        return 0

    # Human-readable rendering.
    print("AegisData Prompt Cache Lint Report")
    print("==================================")
    if report.transcript_path:
        print(f"  transcript:  {report.transcript_path}")
        print(f"  n_turns:     {report.n_turns}")
    if report.n_turns > 0:
        print()
        print(
            f"  observed cache_hit_rate:    "
            f"{report.observed_cache_hit_rate * 100:5.1f}%"
        )
        print(
            f"  theoretical max (no breaks):"
            f"{report.theoretical_max_cache_hit_rate * 100:5.1f}%"
        )
        print(
            f"  potential token savings:    "
            f"{report.potential_token_savings:,} tokens"
        )

    if report.breaks:
        print()
        print(f"Cache breaks detected: {len(report.breaks)}")
        print("─" * 50)
        for b in report.breaks:
            print(
                f"  ⚠ turn {b.turn_idx}  "
                f"{b.before_efficiency * 100:.0f}% → "
                f"{b.after_efficiency * 100:.0f}%  "
                f"(−{b.drop_pp:.1f} pp, "
                f"~{b.tokens_lost_estimate:,} tokens lost)"
            )
            print(f"    cause:      {b.attribution}")
            print(f"    suggestion: {b.suggestion}")

    if report.static_findings:
        print()
        print(
            f"Static lint findings: {len(report.static_findings)}"
        )
        print("─" * 50)
        for f in report.static_findings:
            sev_marker = (
                "✗" if f.severity == "error"
                else ("⚠" if f.severity == "warning" else "·")
            )
            print(
                f"  {sev_marker} char {f.position:>5}  "
                f"[{f.pattern_name}]  "
                f"matched: {f.matched_excerpt!r}"
            )
            print(f"    → {f.suggestion}")

    if not report.breaks and not report.static_findings:
        print()
        print("  ✓ no cache-breaking patterns detected")

    return 0


def _cmd_install_rescue() -> int:
    """Restore ``~/.claude/settings.json`` from the most-recent
    ``settings.json.bak.<ts>`` backup. PR #101 anti-self-DoS escape
    hatch — when an install replaces a working hook with one that
    locks the user out (cost gate, missing approve-as-block flag,
    etc.) the user runs ``aegis install --rescue`` from a separate
    terminal to roll back."""
    backups = sorted(
        SETTINGS_PATH.parent.glob("settings.json.bak.*"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not backups:
        print(
            _red("[install --rescue] no settings.json.bak.* backups found"),
            file=sys.stderr,
        )
        print(
            f"  (looked under {SETTINGS_PATH.parent})",
            file=sys.stderr,
        )
        return 1
    latest = backups[0]
    shutil.copy2(latest, SETTINGS_PATH)
    print(_green(f"✓ restored {SETTINGS_PATH} from {latest.name}"))
    print(
        f"  ({len(backups)} backup{'s' if len(backups) != 1 else ''} "
        "available, oldest first):"
    )
    for b in reversed(backups[:5]):
        ts = time.strftime(
            "%Y-%m-%d %H:%M:%S", time.localtime(b.stat().st_mtime),
        )
        print(f"    {ts}  {b.name}")
    if len(backups) > 5:
        print(f"    … +{len(backups) - 5} older")
    print()
    print("Restart Claude Code if hooks aren't behaving as expected.")
    return 0


def _cmd_install_openclaw_stub(target: str) -> int:
    """Print a friendly roadmap message for the two preview release
    tracks (openclaw-local / openclaw-cloud) and exit cleanly.

    The Claude Code track is the current GA release; the OpenClaw
    tracks are documented in docs/releases/<TRACK>.ko.md but the
    actual @openclaw/plugin-aegis TypeScript plugin lands in a
    separate PR series (3-4 weeks). Until then this stub keeps the
    --target flag wired up so users get the expected --help output
    and a clear "what's coming" pointer instead of an installer that
    silently does nothing or crashes.
    """
    track_meta = {
        "openclaw-local": {
            "label": "OpenClaw + Local OSS LLM",
            "doc": "docs/releases/OPENCLAW_LOCAL.ko.md",
            "eta": "2026 H2",
            "headline": (
                "air-gapped agentic AI with model-weight baseline + "
                "logit-level forensic + GPU/KV-cache server metrics"
            ),
        },
        "openclaw-cloud": {
            "label": "OpenClaw + Cloud LLM API",
            "doc": "docs/releases/OPENCLAW_CLOUD.ko.md",
            "eta": "2026 H1",
            "headline": (
                "multi-channel agent ops (Telegram/Discord/Slack/Web) "
                "with multi-provider drift detection"
            ),
        },
    }
    meta = track_meta[target]
    print()
    print(_yellow(f"  Aegis ATV — {meta['label']} (Preview)"))
    print()
    print(f"  Status:    Preview, GA expected {meta['eta']}")
    print(f"  Headline:  {meta['headline']}")
    print()
    print("  This release track is documented but the underlying")
    print("  @openclaw/plugin-aegis plugin is not yet shipping.")
    print(f"  See {meta['doc']} for the preview spec, scope, and")
    print("  per-feature roadmap.")
    print()
    print("  In the meantime, the same Aegis core (16-step firewall,")
    print("  audit chain, Coach/Live/Doctor) is fully usable on the")
    print("  Claude Code track:")
    print()
    print(_green("    aegis install --target claude-code --mode local"))
    print()
    print("  Subscribe to releases to get notified when this track GA's:")
    print("    https://github.com/happyikas/Aegis-ATV/releases")
    print()
    return 0


def _install_license_gate_ok(*, mode: str, profile: str) -> bool:
    """License gate for ``aegis install`` (LICENSE_KEY.md §9 steps 5 + 7).

    Returns True when the install can proceed. Returns False AND prints
    a structured message to stderr when the active license doesn't
    grant the requested tier's feature. The free / local path always
    returns True (no gate).

    Gates:
      * ``--mode sidecar`` → requires ``sidecar.multi-tenant`` (Team+)
      * ``--mode local --profile pro/cloud`` → requires ``advisor.full`` (Pro+)

    The license is reloaded from disk at this check so a freshly-
    activated key takes effect without restarting the shell.
    """
    # Free path: no gate.
    free_install = mode == "local" and profile == "free"
    if free_install:
        return True

    # Defensive: any error loading the license module/state must NOT
    # break install. Treat unreadable license state as Solo Free; the
    # gate refuses the requested upgrade with the standard message.
    try:
        from aegis.license import get_active_tier, has_feature, init_active_from_disk
        init_active_from_disk()
    except Exception:  # noqa: BLE001 — defensive
        # Fall through with safe defaults.
        def has_feature(_: str) -> bool:  # type: ignore[no-redef]
            return False

        def get_active_tier() -> str:  # type: ignore[no-redef]
            return "free"

    if mode == "sidecar":
        if has_feature("sidecar.multi-tenant"):
            return True
        active = get_active_tier()
        print(
            _red(
                "  --mode sidecar requires a Team or Enterprise license."
            ),
            file=sys.stderr,
        )
        print(
            f"  Active tier: {active}. To proceed:",
            file=sys.stderr,
        )
        print(
            "    • `aegis license activate <key.jwt>` (Team / Enterprise key), OR",
            file=sys.stderr,
        )
        print(
            "    • `aegis install --mode local` (Solo Free / Pro in-process hook)",
            file=sys.stderr,
        )
        print(
            "  See PRICING.md for tier breakdown.",
            file=sys.stderr,
        )
        return False

    if mode == "local" and profile in ("pro", "cloud"):
        if has_feature("advisor.full"):
            return True
        active = get_active_tier()
        print(
            _red(
                f"  --profile {profile} requires a Pro / Team / Enterprise license."
            ),
            file=sys.stderr,
        )
        print(
            f"  Active tier: {active}. To proceed:",
            file=sys.stderr,
        )
        print(
            "    • `aegis license activate <key.jwt>` (Pro / Team / Enterprise), OR",
            file=sys.stderr,
        )
        print(
            "    • `aegis install --mode local --profile free` "
            "(the unconditional Solo Free tier)",
            file=sys.stderr,
        )
        print(
            "  See PRICING.md for tier breakdown.",
            file=sys.stderr,
        )
        return False

    return True


def cmd_install(args: argparse.Namespace) -> int:
    """Idempotently install Aegis hooks into ``~/.claude/settings.json``.

    Absorbs the safety properties of the legacy ``tools/install_hook.py``
    (settings.json backup, JSON validation refusal, idempotency on the
    Aegis-owned PreToolUse entry) and adds:

    * ``--mode sidecar`` (default) — registers
      ``tools/aegis_hook.py`` so the hook POSTs to ``localhost:8000/evaluate``.
      Requires ``docker compose up -d`` to be running.
    * ``--mode local`` — registers ``tools/aegis_local_hook.py`` so
      the firewall pipeline runs in-process; no service required (Solo
      Free tier).
    * ``--judge dummy|hybrid`` (local mode only) — chooses the offline
      sLLM judge stack. ``dummy`` is keyword-only (fastest, may miss
      AWS-secret + loop scenarios); ``hybrid`` is heuristic + keyword
      + M13 attribution head (recommended for real coding-AI traffic).
    * PostToolUse hook — both modes register
      ``tools/hooks/post_tool.py`` so the ATMU intent (opened by
      PreToolUse) is closed with the committed status / result hash.
      Required for the audit chain to reflect actual tool execution.
    * Stop hook — both modes also register
      ``tools/hooks/session_end.py`` so transcript cost data is
      back-filled when each Claude Code session ends (D6).
    * Plugin manifest validation — refuses to install if
      ``.claude-plugin/plugin.json`` is missing or malformed.
    """
    # PR #101 — --rescue path: restore the most-recent settings.json
    # backup. Useful when a previous install replaced the live hook
    # with one that locks the user out (cost gate, missing
    # AEGIS_APPROVE_AS_BLOCK=0, etc.). No-op if no backup exists.
    if getattr(args, "rescue", False):
        return _cmd_install_rescue()

    # Three release tracks (claude-code / openclaw-local / openclaw-cloud).
    # Only claude-code is GA right now — the OpenClaw tracks print a
    # roadmap message and exit cleanly, pointing the user at
    # docs/releases/<TRACK>.ko.md for the preview specification.
    target = getattr(args, "target", "claude-code")
    if target in ("openclaw-local", "openclaw-cloud"):
        return _cmd_install_openclaw_stub(target)

    mode = args.mode
    profile = getattr(args, "profile", "free")

    # ── License gate ──────────────────────────────────────────────
    # Per docs/LICENSE_KEY.md §9 steps 5 + 7. Refuse paid tiers when
    # the active license doesn't grant the feature. This check runs
    # *before* the auto-pull step so we never waste time downloading
    # GGUF models the user can't legally activate.
    #
    # Solo Free path: profile=free + mode=local. No license needed,
    # no check fires. The fast path is unaffected.
    if not _install_license_gate_ok(mode=mode, profile=profile):
        return 1
    if mode == "local":
        if profile not in VALID_LOCAL_PROFILES:
            print(
                _red(
                    f"--profile must be one of {VALID_LOCAL_PROFILES}, "
                    f"got {profile!r}"
                ),
                file=sys.stderr,
            )
            return 2
        # Resolve baseline from profile, letting explicit --judge /
        # --embedding override. Args parsed with default=None so we
        # can distinguish "user explicit" from "use profile default".
        resolved = _resolve_profile(
            profile,
            judge_arg=getattr(args, "judge", None),
            embedding_arg=getattr(args, "embedding", None),
        )
        judge = str(resolved["judge"])
        embedding = str(resolved["embedding"])
        advisor_enabled = bool(resolved["advisor_enabled"])
        advisor_provider = resolved["advisor_provider"]
        # _PROFILE_DEFAULTS is typed dict[str, dict[str, object]] so this
        # narrowing satisfies mypy without forcing a TypedDict.
        _ap_raw = resolved["auto_pull"]
        _profile_pull: tuple[object, ...] = (
            tuple(_ap_raw) if isinstance(_ap_raw, (tuple, list)) else ()
        )
        # Filter by what the *resolved* configuration actually uses.
        # If the user overrode --judge dummy / --embedding dummy on top
        # of profile=pro, do not download models that won't be loaded
        # — respect the explicit override.
        _needs: list[object] = []
        if embedding == "bge-local" and "bge-base-en" in _profile_pull:
            _needs.append("bge-base-en")
        if judge in ("local-phi", "hybrid") and "llama-3.2-1b" in _profile_pull:
            _needs.append("llama-3.2-1b")
        auto_pull_specs: tuple[object, ...] = tuple(_needs)
    else:
        # Sidecar mode keeps the legacy defaults — profile is local-only.
        judge = getattr(args, "judge", None) or "dummy"
        embedding = getattr(args, "embedding", None) or "dummy"
        advisor_enabled = False
        advisor_provider = None
        auto_pull_specs = ()
    if mode == "local" and judge not in VALID_LOCAL_JUDGES:
        print(
            _red(f"--judge must be one of {VALID_LOCAL_JUDGES}, got {judge!r}"),
            file=sys.stderr,
        )
        return 2
    if mode == "local" and embedding not in VALID_LOCAL_EMBEDDINGS:
        print(
            _red(
                f"--embedding must be one of {VALID_LOCAL_EMBEDDINGS}, "
                f"got {embedding!r}"
            ),
            file=sys.stderr,
        )
        return 2

    # Cloud profile: warn (don't fail) if ANTHROPIC_API_KEY isn't set.
    # The user may set it in ~/.zshrc post-install; the firewall handles
    # missing key gracefully (falls back to the local-phi tier of the
    # hybrid judge with a one-time stderr warning).
    if mode == "local" and profile == "cloud" and not os.environ.get(
        "ANTHROPIC_API_KEY"
    ):
        print(_yellow(
            "  warning: --profile cloud activates the Haiku judge but "
            "ANTHROPIC_API_KEY is not set. The hybrid chain falls back to "
            "local-phi when the key is missing — set the key in your shell "
            "profile to enable cloud-tier accuracy."
        ))

    ok, info = _validate_plugin_manifest()
    if not ok:
        print(_red(info), file=sys.stderr)
        return 1
    suffix = (
        f", profile={profile}, judge={judge}, embedding={embedding}"
        if mode == "local" else ""
    )
    print(f"[install] plugin v{info}, mode={mode}{suffix}")

    # PR-A: profile=pro / cloud auto-pulls required GGUF model files
    # before patching settings.json, so the hook never starts up
    # pointing at a missing model. Idempotent — pull-model fast-paths
    # if the target already exists.
    if mode == "local" and auto_pull_specs:
        rc = _auto_pull_models(auto_pull_specs)
        if rc != 0:
            print(_red(
                f"[install] auto-pull failed (rc={rc}); refusing to patch "
                f"settings.json with a profile that needs missing models. "
                f"Re-run after fixing the download, or use --profile free."
            ), file=sys.stderr)
            return rc

    # When the judge needs a local GGUF, pre-flight the model file +
    # llama-cpp-python so the user knows immediately if the install
    # will degrade to stub mode at runtime.
    if mode == "local" and judge in ("local-phi", "hybrid"):
        gguf_ok, gguf_msg = _gguf_status_for_install(judge)
        if gguf_ok:
            print(_green(f"  {gguf_msg}"))
        else:
            print(_yellow(f"  warning: {gguf_msg}"))
    if mode == "local" and embedding == "bge-local":
        bge_ok, bge_msg = _bge_status_for_install(embedding)
        if bge_ok:
            print(_green(f"  {bge_msg}"))
        else:
            print(_yellow(f"  warning: {bge_msg}"))

    pretool_script = LOCAL_HOOK_SCRIPT if mode == "local" else HOOK_SCRIPT
    if not pretool_script.exists():
        print(_red(f"hook script not found: {pretool_script}"), file=sys.stderr)
        return 1
    if not pretool_script.stat().st_mode & 0o100:
        print(_yellow(f"making {pretool_script.name} executable"))
        pretool_script.chmod(pretool_script.stat().st_mode | 0o111)
    if not POST_HOOK_SCRIPT.exists():
        print(_red(f"hook script not found: {POST_HOOK_SCRIPT}"), file=sys.stderr)
        return 1

    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)

    if SETTINGS_PATH.exists():
        try:
            existing = json.loads(SETTINGS_PATH.read_text())
        except json.JSONDecodeError as e:
            print(
                _red(
                    f"existing settings.json is not valid JSON ({e}); "
                    "refusing to touch it."
                ),
                file=sys.stderr,
            )
            return 1
        backup = SETTINGS_PATH.with_name(f"settings.json.bak.{int(time.time())}")
        shutil.copy2(SETTINGS_PATH, backup)
        print(_yellow(f"backed up existing settings → {backup.name}"))
    else:
        existing = {}
        print(f"creating new {SETTINGS_PATH}")

    pretool_cmd = _build_pretool_command(
        mode, judge=judge, embedding=embedding,
        advisor_enabled=advisor_enabled,
        advisor_provider=advisor_provider if isinstance(advisor_provider, str) else None,
    )
    pretool_marker = _pretool_hook_marker(mode)
    pretool_entry = {
        "matcher": "*",
        "hooks": [{"type": "command", "command": pretool_cmd}],
    }

    hooks_section = existing.setdefault("hooks", {})

    if args.force:
        n_dropped = _drop_aegis_entries(hooks_section)
        if n_dropped:
            print(_yellow(
                f"--force: evicted {n_dropped} stale Aegis hook entr"
                f"{'y' if n_dropped == 1 else 'ies'}"
            ))

    pretooluse = hooks_section.setdefault("PreToolUse", [])

    for entry in pretooluse:
        for h in entry.get("hooks", []):
            if pretool_marker in h.get("command", "") and not args.force:
                print(_green(f"already installed — {h['command']!r}"))
                print("(re-run with --force to replace it)")
                return 0

    pretooluse.append(pretool_entry)

    # Register PostToolUse — closes ATMU intent (2PC phase 2). Idempotent.
    posttool_cmd = _build_posttool_command(mode)
    posttool_hooks = hooks_section.setdefault("PostToolUse", [])
    posttool_already = any(
        str(POST_HOOK_SCRIPT) in h.get("command", "")
        for entry in posttool_hooks
        for h in entry.get("hooks", [])
    )
    if not posttool_already:
        posttool_hooks.append({
            "matcher": "*",
            "hooks": [{"type": "command", "command": posttool_cmd}],
        })

    # Always register the Stop hook (D6 cost auto-import); idempotent.
    stop_hooks = hooks_section.setdefault("Stop", [])
    stop_already = any(
        str(STOP_HOOK_SCRIPT) in h.get("command", "")
        for entry in stop_hooks
        for h in entry.get("hooks", [])
    )
    if not stop_already:
        stop_cmd = f"{_hook_python_executable()} {STOP_HOOK_SCRIPT}"
        stop_hooks.append({"hooks": [{"type": "command", "command": stop_cmd}]})

    # PR #47 — PreCompact + UserPromptSubmit forensic hooks. Both are
    # additive, never block, and need PYTHONPATH=src to import the
    # aegis package.
    py = _hook_python_executable()
    env_prefix = f"PYTHONPATH={SRC_DIR}"

    precompact_hooks = hooks_section.setdefault("PreCompact", [])
    precompact_already = any(
        str(PRECOMPACT_HOOK_SCRIPT) in h.get("command", "")
        for entry in precompact_hooks
        for h in entry.get("hooks", [])
    )
    if not precompact_already:
        precompact_cmd = f"{env_prefix} {py} {PRECOMPACT_HOOK_SCRIPT}"
        precompact_hooks.append({
            "hooks": [{"type": "command", "command": precompact_cmd}],
        })

    user_prompt_hooks = hooks_section.setdefault("UserPromptSubmit", [])
    user_prompt_already = any(
        str(USER_PROMPT_HOOK_SCRIPT) in h.get("command", "")
        for entry in user_prompt_hooks
        for h in entry.get("hooks", [])
    )
    if not user_prompt_already:
        user_prompt_cmd = f"{env_prefix} {py} {USER_PROMPT_HOOK_SCRIPT}"
        user_prompt_hooks.append({
            "hooks": [{"type": "command", "command": user_prompt_cmd}],
        })

    # Sprint 1 PR4 — SessionStart welcome hint. Fires once on first
    # session post-install, guides new users to the slash commands and
    # the killer feature (audit chain). Subsequent sessions are silent
    # via the ~/.aegis/.welcomed marker.
    session_start_hooks = hooks_section.setdefault("SessionStart", [])
    session_start_already = any(
        str(SESSION_START_HOOK_SCRIPT) in h.get("command", "")
        for entry in session_start_hooks
        for h in entry.get("hooks", [])
    )
    if not session_start_already:
        session_start_cmd = f"{env_prefix} {py} {SESSION_START_HOOK_SCRIPT}"
        session_start_hooks.append({
            "hooks": [{"type": "command", "command": session_start_cmd}],
        })

    SETTINGS_PATH.write_text(json.dumps(existing, indent=2) + "\n")

    print(_green(f"\u2713 installed Aegis hooks → {SETTINGS_PATH}"))
    print(f"  PreToolUse:  {pretool_cmd}")
    if not posttool_already:
        print(f"  PostToolUse: {posttool_cmd}")
    if not stop_already:
        print(f"  Stop:        {_hook_python_executable()} {STOP_HOOK_SCRIPT}")
    if not precompact_already:
        print(f"  PreCompact:  {PRECOMPACT_HOOK_SCRIPT}")
    if not user_prompt_already:
        print(f"  UserPromptSubmit: {USER_PROMPT_HOOK_SCRIPT}")
    if not session_start_already:
        print(f"  SessionStart: {SESSION_START_HOOK_SCRIPT}")
    print('  matcher: "*" (every tool — narrow this in settings.json if too noisy)')
    print()
    if mode == "sidecar":
        print("Sidecar mode: start the Aegis service with `docker compose up -d`")
        print("  (the hook POSTs to localhost:8000/evaluate)")
    else:
        # Profile-aware banner — surfaces the intelligence tier so users
        # know what to expect (no cloud calls, semantic retrieval on,
        # haiku grey-zone judge, etc.).
        if profile == "free":
            print(_green(
                "Local mode (profile=free): in-process firewall — "
                "no service needed, 0 cloud calls, 0 model files."
            ))
            print(_yellow(
                "  upgrade with `aegis install --mode local --profile pro`  "
                "(M13 attribution head + bge-local + advisor; ~700 MB)"
            ))
        elif profile == "pro":
            print(_green(
                "Local mode (profile=pro): real semantic retrieval + "
                "M13 attribution + local-phi judge + advisor — "
                "still 0 cloud calls."
            ))
        else:  # cloud
            print(_green(
                "Local mode (profile=cloud): pro stack + haiku judge for "
                "grey-zone calls. Cloud calls go to Anthropic only when "
                "the local-phi tier is uncertain."
            ))

    # PR3 — install Claude Code custom slash commands. Default ON; opt-out
    # via --no-commands. Local-mode only (sidecar mode users typically
    # don't share the same Claude Code installation per-host).
    if mode == "local" and not getattr(args, "no_commands", False):
        try:
            n_cmds, cmd_names = _install_slash_commands()
            if n_cmds:
                print(_green(
                    f"  installed {n_cmds} Claude Code slash command(s) "
                    f"→ {SLASH_COMMANDS_DST}"
                ))
                cmd_short = ", ".join(
                    f"/{c.replace('.md', '')}" for c in cmd_names[:3]
                )
                more = f" +{n_cmds - 3} more" if n_cmds > 3 else ""
                print(_green(
                    f"  available in next Claude Code session: {cmd_short}{more}"
                ))
                print(_green(
                    "  type /aegis-help for the full list (or pass "
                    "--no-commands to skip on next install)"
                ))
        except Exception as e:  # noqa: BLE001 — slash commands are non-blocking
            print(_yellow(
                f"  warning: could not install slash commands ({e}); "
                "core hook is still active. Re-run install or pass "
                "--no-commands to silence this."
            ))
    print()
    print(_green("─── NEXT STEPS ──────────────────"))
    print("  1. Restart Claude Code (full quit & relaunch — tab reopen")
    print("     is not enough; the hook loads from settings.json at")
    print("     process start).")
    print("  2. Try a destructive operation in Claude Code; Aegis will")
    print("     BLOCK before the tool runs.")
    print("  3. After a few sessions, inspect what got caught:")
    print(_green("       /aegis-report                   # 5-line risk summary (in Claude Code)"))
    print(_green("       /aegis-verify                   # cryptographic chain check"))
    print(_green("       /aegis-forensic last            # postmortem timeline"))
    print(_green("       /aegis-help                     # all available commands"))
    print(_green("     ── or from your shell ──"))
    print(_green("       uv run aegis report"))
    print(_green("       uv run aegis verify-audit"))
    print(_green("       uv run aegis policy diff --since 7d"))
    print()
    print("  Quickstart guide:  docs/PERSONAL_QUICKSTART.md")
    print("  Provider upgrade:  uv run aegis pull-model --recommend")
    print(
        "  Sign audit log:    uv run aegis audit-key init"
        "  (optional — adds Ed25519 signatures on every append)"
    )

    legacy_present = any(
        "install_hook.py" in h.get("command", "")
        for entry in pretooluse
        for h in entry.get("hooks", [])
    )
    if legacy_present:
        print()
        print(
            _yellow(
                "Note: detected legacy `tools/install_hook.py` entry in your "
                "settings. The CLI's own entry is now installed alongside it; "
                "you may remove the legacy line manually if no longer needed."
            )
        )

    return 0


def cmd_uninstall(args: argparse.Namespace) -> int:
    """Remove Aegis-owned hooks from ``~/.claude/settings.json``.

    Uses the same fingerprint set ``--force`` install relies on
    (``aegis_local_hook.py``, ``aegis_hook.py``, ``post_tool.py``,
    ``session_end.py``) so this is the precise inverse of install:
    every entry the install command would have added gets removed,
    nothing else is touched.

    Behaviour:

    * Unknown / non-Aegis hooks (prettier, gitleaks, third-party
      extensions) are **preserved** verbatim.
    * Empty hook stages (``PreToolUse: []``, ``Stop: []`` after the
      removal) are also preserved — Claude Code tolerates them and
      we don't want to disturb a settings.json the user may have
      hand-edited.
    * settings.json is backed up to ``settings.json.bak.<ts>`` before
      writing — same convention ``aegis install`` follows. Use
      ``--no-backup`` to skip.
    * If no Aegis-owned hooks are found, the command prints a green
      "nothing to remove" notice and exits 0 — idempotent.
    * ``--dry-run`` prints which entries WOULD be removed without
      modifying the file. Useful for double-checking before running.

    The user must restart Claude Code for the change to take
    effect (Claude Code reads ``settings.json`` once at session
    start). The exit message reminds them.
    """
    if not SETTINGS_PATH.exists():
        print(_yellow(f"no settings.json at {SETTINGS_PATH} — nothing to do"))
        return 0

    try:
        existing = json.loads(SETTINGS_PATH.read_text())
    except json.JSONDecodeError as e:
        print(
            _red(f"existing settings.json is not valid JSON ({e}); refusing to touch it."),
            file=sys.stderr,
        )
        return 1

    hooks_section = existing.get("hooks", {})
    if not isinstance(hooks_section, dict):
        print(_yellow("settings.json has no `hooks` section — nothing to do"))
        return 0

    # Walk the rotation set so dry-run can show exactly what would
    # be removed (using the same predicate as the live drop).
    to_remove: list[tuple[str, str]] = []
    for stage in (
        "PreToolUse", "PostToolUse", "Stop",
        "PreCompact", "UserPromptSubmit",   # PR #47
        "SessionStart",                      # PR4 (Sprint 1)
    ):
        for entry in hooks_section.get(stage, []):
            for h in entry.get("hooks", []):
                cmd = h.get("command", "")
                if _is_aegis_owned(cmd):
                    to_remove.append((stage, cmd))

    if not to_remove:
        print(_green(f"✓ no Aegis-owned hooks in {SETTINGS_PATH} — nothing to remove"))
        return 0

    print(f"[uninstall] settings.json: {SETTINGS_PATH}")
    print(f"[uninstall] would remove {len(to_remove)} Aegis-owned hook entry(ies):")
    for stage, cmd in to_remove:
        print(f"    {_yellow(stage):<24}  {cmd[:90]}")

    if args.dry_run:
        print()
        print(_green("dry-run — settings.json NOT modified"))
        return 0

    if not args.no_backup:
        backup = SETTINGS_PATH.with_name(f"settings.json.bak.{int(time.time())}")
        try:
            shutil.copy2(SETTINGS_PATH, backup)
            print(_yellow(f"backed up existing settings → {backup.name}"))
        except OSError as e:
            print(_red(f"backup failed: {e}"), file=sys.stderr)
            return 1

    n_dropped = _drop_aegis_entries(hooks_section)
    SETTINGS_PATH.write_text(json.dumps(existing, indent=2) + "\n")
    print(_green(f"✓ removed {n_dropped} Aegis hook entry(ies) from {SETTINGS_PATH}"))

    # PR3 — also remove our slash commands. Only files carrying the
    # SLASH_COMMANDS_MARKER are touched, so user-owned commands in the
    # same directory survive verbatim.
    try:
        n_cmds, cmd_names = _uninstall_slash_commands(
            dry_run=getattr(args, "dry_run", False),
        )
        if n_cmds:
            verb = "would remove" if getattr(args, "dry_run", False) else "✓ removed"
            print(_green(
                f"{verb} {n_cmds} slash command(s) from {SLASH_COMMANDS_DST}"
            ))
    except Exception as e:  # noqa: BLE001
        print(_yellow(f"  warning: could not clean slash commands: {e}"))

    print()
    print(_yellow("Restart Claude Code for the change to take effect."))
    print(
        _yellow(
            "  Per-session state at ~/.aegis/audit.jsonl, ~/.aegis/sessions/, "
            "~/.aegis/shadow.jsonl is preserved — delete manually if you want\n"
            "  a fully clean slate (or keep them for `aegis verify-audit`)."
        ),
    )
    return 0


def _human_size(n_bytes: int) -> str:
    """`9_876_543` → `'9.4 MB'`. For pull-model progress UX."""
    for unit in ("B", "KB", "MB", "GB"):
        if n_bytes < 1024 or unit == "GB":
            return f"{n_bytes:.1f} {unit}" if unit != "B" else f"{n_bytes} B"
        n_bytes /= 1024  # type: ignore[assignment]
    return f"{n_bytes:.1f} GB"


def _auto_pull_models(spec_names: tuple[object, ...]) -> int:
    """Idempotently download each model spec listed by a profile.

    Calls cmd_pull_model() programmatically per spec. Each pull is a
    no-op when the target file already exists with the right size, so
    re-running install is cheap. Returns 0 on full success, non-zero
    on the first failure (caller refuses to patch settings.json).

    Used by --profile pro / cloud to make the install one-shot — users
    don't have to remember to run pull-model separately.
    """
    import argparse as _ap

    if not spec_names:
        return 0
    print(_green(
        f"[install] auto-pulling {len(spec_names)} model file(s) for "
        f"profile (idempotent — already-present files fast-path)"
    ))
    for name in spec_names:
        ns = _ap.Namespace(
            model=str(name),
            list=False,
            force=False,
            recommend=False,
        )
        rc = cmd_pull_model(ns)
        if rc != 0:
            print(_red(
                f"[install] auto-pull failed for spec={name!r} (rc={rc})"
            ), file=sys.stderr)
            return rc
    return 0


def _check_llama_cpp_installed() -> tuple[bool, str]:
    """Return ``(ok, message)`` describing whether llama-cpp-python is usable."""
    try:
        import importlib

        importlib.import_module("llama_cpp")
        return True, "llama-cpp-python: installed"
    except ImportError:
        return False, (
            "llama-cpp-python is not installed. Solo Free real-sLLM mode "
            "needs it. Install with:\n"
            "  uv sync --extra local-llm\n"
            "  (Apple Silicon: prefix with CMAKE_ARGS=\"-DGGML_METAL=on\" "
            "for Metal acceleration)"
        )


def cmd_pull_model(args: argparse.Namespace) -> int:
    """Download a Solo Free local-sLLM GGUF into ``./models/``.

    Default model: ``llama-3.2-1b`` (770 MB, ~80 ms/verdict on M1
    CPU-only, Llama 3.2 Community License). See
    ``aegis.judge.model_registry`` for the full catalogue.

    Idempotent: if the target file already exists with the right size,
    skips the download and prints the path. ``--force`` re-downloads.

    Side effects:
    1. Creates ``./models/<filename>.gguf``.
    2. Prints the line you should add to your ``.env`` so the local-phi
       judge picks it up:  ``AEGIS_JUDGE_MODEL_PATH=...``.
    3. Prints next-step command (``aegis install --judge local-phi``).

    No external dependencies — uses ``httpx`` (already in core deps) so
    you can run this with just ``uv sync`` (no ``--extra local-llm``
    needed). The actual *use* of the GGUF still requires
    ``llama-cpp-python`` from the optional extra.
    """
    from aegis.judge.model_registry import (
        DEFAULT_EMBEDDING_NAME,
        DEFAULT_MODEL_NAME,
        get_model,
        list_models,
        model_target_path,
        render_recommendations,
    )

    if getattr(args, "recommend", False):
        print(render_recommendations())
        return 0

    if args.list:
        defaults = {DEFAULT_MODEL_NAME, DEFAULT_EMBEDDING_NAME}
        print(f"{'name':<16} {'kind':<10} {'size':>8}  description")
        print("─" * 92)
        for m in list_models():
            marker = " (default)" if m.name in defaults else ""
            alias_part = (
                f" (alias: {', '.join(m.aliases)})" if m.aliases else ""
            )
            print(
                f"{m.name:<16} {m.kind:<10} {m.size_mb:>5} MB  "
                f"{m.description}{alias_part}{marker}"
            )
        return 0

    try:
        spec = get_model(args.model)
    except KeyError as e:
        print(_red(str(e)), file=sys.stderr)
        return 2

    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    target = model_target_path(spec, MODELS_DIR)

    if target.exists() and not args.force:
        actual_mb = target.stat().st_size / 1_000_000
        print(_green(
            f"✓ already present: {target}  ({actual_mb:.0f} MB)"
        ))
        print("  (re-download with --force)")
        _print_pull_next_steps(target, spec)
        return 0

    print(f"[pull-model] {spec.name} — {spec.description}")
    print(f"[pull-model] license: {spec.license}")
    print(f"[pull-model] target:  {target}")
    print(f"[pull-model] source:  {spec.url}")
    print(f"[pull-model] size:    ~{spec.size_mb} MB")
    print()

    try:
        import httpx
    except ImportError:
        print(_red("httpx not installed — should be in core deps. Run `uv sync`."),
              file=sys.stderr)
        return 1

    tmp = target.with_suffix(target.suffix + ".part")
    try:
        with httpx.stream(
            "GET", spec.url, follow_redirects=True, timeout=httpx.Timeout(60.0),
        ) as r:
            if r.status_code != 200:
                print(_red(f"download failed: HTTP {r.status_code}"),
                      file=sys.stderr)
                return 1
            total = int(r.headers.get("content-length", 0))
            written = 0
            last_pct = -1
            with tmp.open("wb") as f:
                for chunk in r.iter_bytes(chunk_size=1024 * 1024):
                    f.write(chunk)
                    written += len(chunk)
                    if total > 0:
                        pct = int(written * 100 / total)
                        if pct != last_pct and pct % 5 == 0:
                            print(
                                f"  {pct:3d}%  "
                                f"{_human_size(written)} / {_human_size(total)}",
                                end="\r", flush=True,
                            )
                            last_pct = pct
        print()  # newline after progress
    except (httpx.HTTPError, OSError) as e:
        if tmp.exists():
            tmp.unlink()
        print(_red(f"download failed: {e}"), file=sys.stderr)
        return 1

    tmp.rename(target)
    actual_mb = target.stat().st_size / 1_000_000
    print(_green(f"✓ downloaded: {target}  ({actual_mb:.0f} MB)"))

    if spec.sha256:
        import hashlib
        h = hashlib.sha256()
        with target.open("rb") as f:
            for chunk in iter(lambda: f.read(64 * 1024), b""):
                h.update(chunk)
        if h.hexdigest() != spec.sha256:
            print(_red(
                f"sha256 mismatch! expected {spec.sha256}, got {h.hexdigest()}\n"
                f"removing corrupted file: {target}"
            ), file=sys.stderr)
            target.unlink()
            return 1
        print(_green("✓ sha256 verified"))

    _print_pull_next_steps(target, spec)
    return 0


def _print_pull_next_steps(target: Path, spec: object | None = None) -> None:
    """Print user-facing next-step instructions for the model just pulled.

    Branches on the model's ``kind`` so judge GGUFs get judge-specific
    guidance (env var ``AEGIS_JUDGE_MODEL_PATH`` + ``--judge local-phi``)
    and embedding GGUFs get embedding-specific guidance
    (``AEGIS_EMBEDDING_MODEL_PATH`` + ``--embedding bge-local``).
    """
    kind = getattr(spec, "kind", "judge")
    print()
    print("Next steps:")
    if kind == "embedding":
        print("  1. Add to .env:")
        print(f"       AEGIS_EMBEDDING_MODEL_PATH={target}")
        print("       AEGIS_EMBEDDING_PROVIDER=bge-local")
        print("  2. Install the optional llama-cpp-python (if not already):")
        print("       uv sync --extra local-llm")
        print("     (Apple Silicon Metal: prefix with")
        print("       CMAKE_ARGS=\"-DGGML_METAL=on\"  for GPU acceleration)")
        print("  3. Wire the hook (any judge mode works):")
        print("       uv run aegis install --mode local --judge hybrid "
              "--embedding bge-local --force")
        print("  4. Verify:")
        print("       ./scripts/dogfood_check.sh --hybrid")
    else:
        print("  1. Add to .env:")
        print(f"       AEGIS_JUDGE_MODEL_PATH={target}")
        print("  2. Install the optional llama-cpp-python:")
        print("       uv sync --extra local-llm")
        print("     (Apple Silicon Metal: prefix with")
        print("       CMAKE_ARGS=\"-DGGML_METAL=on\"  for GPU acceleration)")
        print("  3. Wire the hook:")
        print("       uv run aegis install --mode local --judge local-phi --force")
        print("  4. Verify:")
        print("       ./scripts/dogfood_check.sh --judge local-phi")


def cmd_advisor_calibration(args: argparse.Namespace) -> int:
    """v2.7.2 Phase D — feedback-driven advisor-gate retraining.

    Reads ``audit.jsonl``, computes per-signal accuracy from accumulated
    PostToolUse retrospectives, and optionally writes an updated
    calibration JSON. Three actions:

    * ``analyse``   — print accuracy stats only.
    * ``recommend`` — also print proposed new thresholds (dry-run).
    * ``apply``     — persist the new thresholds to disk.
    """
    from pathlib import Path as PathT

    from aegis.burnin.calibration_feedback import (
        analyse_audit,
        apply_recommended_calibration,
        render_feedback_report,
    )

    audit_path = PathT(
        args.audit
        or os.environ.get(
            "AEGIS_LOCAL_AUDIT", str(PathT.home() / ".aegis" / "audit.jsonl")
        )
    )
    if not audit_path.is_file():
        print(f"audit not found: {audit_path}", file=sys.stderr)
        return 1

    report = analyse_audit(audit_path)
    print(render_feedback_report(report))

    if args.action == "analyse":
        return 0

    if args.action == "recommend":
        if report.recommended_calibration is None:
            print(
                "\nNo recommendation — see Notes above.", file=sys.stderr
            )
            return 0
        if not report.calibration_changed:
            print("\nRecommended calibration is identical to current.")
            return 0
        print("\n→ run `aegis advisor-calibration apply` to persist.")
        return 0

    # action == "apply"
    if report.recommended_calibration is None:
        print("\nNothing to apply — see Notes above.", file=sys.stderr)
        return 1
    output = PathT(args.output) if args.output else None
    written = apply_recommended_calibration(report, output_path=output)
    if written is None:
        print("\nNothing to apply.", file=sys.stderr)
        return 1
    print(f"\n✓ wrote new calibration to {written}")
    return 0


def cmd_burnin(args: argparse.Namespace) -> int:
    # train-m13 / compare-m13 / shadow-status have zero dependency on the
    # legacy burnin.retrain module (which is a sidecar-only port).
    # Dispatch early so the import doesn't fail in plugin-mode installs.
    if args.action == "train-m13":
        return _cmd_burnin_train_m13(args)
    if args.action == "compare-m13":
        return _cmd_burnin_compare_m13(args)
    if args.action == "shadow-status":
        return _cmd_burnin_shadow_status(args)
    if args.action == "export-baseline":
        return _cmd_burnin_export_baseline(args)

    from burnin.retrain import retrain, revert  # type: ignore[import-not-found]

    if args.action == "retrain":
        r = retrain(since=args.since, dry_run=args.dry_run)
        print(f"[burnin {args.action}] status={r.get('status')}")
        print(f"  real samples:    {r.get('n_real', 0):,}")
        print(f"  total samples:   {r.get('n_total', 0):,}")
        print(f"  sanity ok:       {r.get('sanity_ok')}")
        if r.get("model_kb"):
            print(f"  model:           burnin/iforest_v1.pkl ({r['model_kb']} KB)")
        if r.get("error"):
            print(f"  error:           {r['error']}")
        return 0 if r.get("status") in ("active", "dry_run_ok") else 1
    if args.action == "revert":
        r = revert()
        print(f"[burnin revert] status={r['status']}  from={r.get('from')}")
        return 0
    if args.action == "train-m13":
        return _cmd_burnin_train_m13(args)
    return 2


def _cmd_burnin_train_m13(args: argparse.Namespace) -> int:
    """Train M13 attribution-head v2 weights from synthetic / shadow data.

    Default behaviour (no ``--corpus``) generates a fresh synthetic corpus
    via :func:`aegis.burnin.m13_data.generate` and trains v2. With
    ``--corpus path.jsonl`` the trainer reads (ATV, label) pairs from a
    Burn-in Shadow dump instead — this is the path real production
    deployments will take once shadow data is collected.

    Output: ``models/m13_attribution_head_v2.json`` — drop-in
    replacement for v1 in :class:`AttributionHead`.
    """
    from pathlib import Path

    from aegis.burnin.m13_data import generate
    from aegis.burnin.m13_train import train_v2, write_v2_json

    out_path = (
        Path(args.out) if args.out
        else PROJECT_ROOT / "models" / "m13_attribution_head_v2.json"
    )

    if args.corpus:
        # Replay from Shadow dump — JSONL of (tool_name, args, label, ...)
        import json as _json
        import time as _time

        from aegis.burnin.m13_data import LabeledExample
        from aegis.schema import ATVHeader, ATVInput, CostEfficiencyMetrics

        corpus: list = []
        with Path(args.corpus).open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                rec = _json.loads(line)
                corpus.append(LabeledExample(
                    category=rec.get("category", "shadow"),
                    label=rec["label"],
                    inp=ATVInput(
                        header=ATVHeader(
                            trace_id=rec.get("trace_id", "t"),
                            span_id=rec.get("span_id", "s"),
                            tenant_id=rec.get("tenant_id", "shadow"),
                            aid=rec.get("aid", "shadow"),
                            timestamp_ns=_time.time_ns(),
                        ),
                        agent_state_text=rec.get("agent_state_text", ""),
                        plan_text=rec.get("plan_text", ""),
                        tool_name=rec["tool_name"],
                        tool_args_json=rec["tool_args_json"],
                        safety_flags={},
                        memory_fingerprint="sha3:shadow",
                        cost_estimate=CostEfficiencyMetrics(
                            input_token_count=10, output_token_count=5,
                        ),
                    ),
                ))
        print(f"[train-m13] loaded {len(corpus)} examples from {args.corpus}")
    else:
        corpus = generate(per_category=args.per_category, seed=args.seed)
        print(
            f"[train-m13] generated {len(corpus)} synthetic examples "
            f"(per_category={args.per_category}, seed={args.seed})"
        )

    if len(corpus) < 30:
        print(
            _red(f"refusing to train on {len(corpus)} examples — "
                 "minimum 30 required for 30-feature classifier"),
            file=sys.stderr,
        )
        return 1

    print("[train-m13] training v2 weights via class-balanced NNLS…")
    result = train_v2(corpus, test_fraction=args.test_fraction)

    print(f"[train-m13] split: {result.n_train} train / {result.n_test} test")
    print(f"[train-m13] train accuracy: {result.train_accuracy:.3f}")
    print(f"[train-m13] test  accuracy: {result.test_accuracy:.3f}")
    print(
        f"[train-m13] thresholds: approval={result.threshold_approval:.3f}, "
        f"block={result.threshold_block:.3f}"
    )

    print()
    print("[train-m13] top 8 weights:")
    top = sorted(
        result.subfield_weights.items(), key=lambda kv: -kv[1],
    )[:8]
    for name, w in top:
        print(f"  {name:<32}  {w:.4f}")

    sha = write_v2_json(result, out_path)
    print()
    print(_green(f"✓ wrote {out_path}"))
    print(f"  model_hash (SHA3-256): {sha[:32]}…")
    print()
    print("Next steps:")
    print("  1. Review weights:")
    print(f"       cat {out_path.relative_to(PROJECT_ROOT)} | jq '.subfield_weights'")
    print("  2. Adopt v2 (replaces v1 in AttributionHead):")
    print(f"       mv {out_path} models/m13_attribution_head_v1.json")
    print("     OR keep both files and select via")
    print(f"       AttributionHead(weights_path=Path('{out_path.name}'))")
    print("  3. Verify regression suite still passes:")
    print("       ./scripts/macmini_user_test.sh --hybrid")
    return 0


def _cmd_burnin_compare_m13(args: argparse.Namespace) -> int:
    """Side-by-side v1 vs v2 evaluation on a fresh synthetic corpus."""
    from pathlib import Path

    from aegis.burnin.m13_eval import compare

    v1_path = (
        Path(args.v1) if args.v1
        else PROJECT_ROOT / "models" / "m13_attribution_head_v1.json"
    )
    v2_path = (
        Path(args.v2) if args.v2
        else PROJECT_ROOT / "models" / "m13_attribution_head_v2.json"
    )
    if not v1_path.exists():
        print(_red(f"v1 weights not found: {v1_path}"), file=sys.stderr)
        return 1
    if not v2_path.exists():
        print(
            _red(
                f"v2 weights not found: {v2_path}\n"
                "  run `aegis burnin train-m13` first to produce v2."
            ),
            file=sys.stderr,
        )
        return 1

    print(f"[compare-m13] v1: {v1_path.name}")
    print(f"[compare-m13] v2: {v2_path.name}")
    print(f"[compare-m13] eval corpus: {args.per_category} × 7 categories")
    result = compare(v1_path, v2_path, per_category=args.per_category)

    print()
    print("  Metric              v1          v2          Δ")
    print("─" * 60)
    print(
        f"  3-class accuracy    {result.v1.accuracy:.3f}       "
        f"{result.v2.accuracy:.3f}       {result.delta_accuracy:+.3f}"
    )
    print(
        f"  False negatives     {result.v1.fn_count:<6}      "
        f"{result.v2.fn_count:<6}      "
        f"{result.v2.fn_count - result.v1.fn_count:+d}"
    )
    print(
        f"  False positives     {result.v1.fp_count:<6}      "
        f"{result.v2.fp_count:<6}      "
        f"{result.v2.fp_count - result.v1.fp_count:+d}"
    )
    print(
        f"  Asym cost (5×FN+FP) {result.v1.cost:<6.1f}      "
        f"{result.v2.cost:<6.1f}      {-result.delta_cost:+.1f}"
    )
    print()
    badge = {"v1": "🅰️", "v2": "🅱️", "tie": "≈"}.get(result.winner, "?")
    print(f"  Winner: {result.winner.upper()}  {badge}")
    if result.notes:
        print()
        for n in result.notes:
            print(f"  note: {n}")
    return 0 if result.winner != "v1" else 0  # informational, never error


def _cmd_burnin_shadow_status(args: argparse.Namespace) -> int:
    """Summarise the Burn-in Shadow log (count + label distribution)."""
    from aegis.burnin.shadow import shadow_stats

    stats = shadow_stats(args.shadow_log)
    print(f"[shadow-status] records: {stats['n']}")
    if stats["n"] == 0:
        print("  (none yet — set AEGIS_BURNIN_SHADOW=1 in your hook env to enable)")
        return 0
    print("  label distribution:")
    for label, count in sorted(stats["by_label"].items()):
        print(f"    {label:<18} {count:>5}")
    if stats["n"] >= 30:
        print()
        print(
            _green(
                f"  ✓ enough samples for `aegis burnin train-m13 "
                f"--corpus {args.shadow_log or '~/.aegis/shadow.jsonl'}`"
            )
        )
    else:
        print(
            _yellow(
                f"  need ≥30 records to train (have {stats['n']}); "
                "let the hook collect for a while longer"
            )
        )
    return 0


def _cmd_burnin_export_baseline(args: argparse.Namespace) -> int:
    """Walk the local audit JSONL and write a per-tenant RAG baseline chunk.

    Default (``--rotate=False``) replaces
    ``policies/rag_corpus/baselines.jsonl`` with a single chunk for the
    named tenant — convenient for one-tenant local installs.

    With ``--rotate``, appends a new datestamped chunk and seals the
    previous open baseline for this tenant by stamping
    ``valid_until=now`` on it. Tool input *values* are never logged so
    the chunk cannot leak content. See PR ② of the temporal-RAG track.
    """
    from pathlib import Path as _Path

    from aegis.burnin.baseline_export import (
        export_to_corpus,
        render_export_report,
    )

    audit_path: _Path | None = None
    if args.audit:
        audit_path = _Path(args.audit).expanduser()
    out_path, summary = export_to_corpus(
        audit_path=audit_path,
        tenant=args.tenant,
        rotate=getattr(args, "rotate", False),
    )
    print(render_export_report(summary, out_path))
    if getattr(args, "rotate", False):
        print(
            "  rotate:            previous open baseline (if any) "
            "sealed with valid_until=<now>"
        )
    return 0 if summary.is_useful else 1


def cmd_case_memory(args: argparse.Namespace) -> int:
    """Build / inspect / import the step340 RAG case memory.

    Three actions, all read/write the npz at
    ``models/case_memory_v1.npz`` (override with ``--out``):

    * ``build``  — embed the synthetic M13 corpus through BGE-base-en
      and save. Default source for fresh installs.
    * ``import`` — embed the rows of a Burn-in Shadow JSONL and save.
      Use this once the user has accumulated real (label, text) pairs
      from production traffic.
    * ``status`` — count + label distribution + top-5 cosine pairs
      (sanity-check that the memory has semantic structure).

    All paths require BGE-local active
    (``aegis pull-model --model bge-base-en`` + ``--extra local-llm``).
    Without BGE, every cosine is meaningless and RAG would inject
    noise into the LLM prompt — we hard-error rather than silently
    embed garbage.
    """
    from pathlib import Path

    from aegis.atv.embeddings import BGELocalEmbedding
    from aegis.judge.case_memory import (
        DEFAULT_CASE_MEMORY_PATH,
        CaseMemory,
    )

    out_path = Path(args.out) if args.out else DEFAULT_CASE_MEMORY_PATH

    if args.action == "status":
        if not out_path.exists():
            print(_yellow(f"no memory at {out_path} — run `aegis case-memory build`"))
            return 0
        memory = CaseMemory.load(out_path)
        print(f"[case-memory] file:  {out_path}")
        print(f"[case-memory] n:     {memory.n}")
        print(f"[case-memory] dim:   {memory.dim}")
        print(f"[case-memory] meta:  {memory.meta}")
        if memory.n > 0:
            from collections import Counter
            counts = Counter(str(memory.labels[i]) for i in range(memory.n))
            print("  labels:")
            for label, c in sorted(counts.items()):
                print(f"    {label:<18} {c:>5}")
        return 0

    # build / import: need BGE.
    bge_path = os.environ.get("AEGIS_EMBEDDING_MODEL_PATH", "").strip()
    if not bge_path or not Path(bge_path).exists():
        print(_red(
            "BGE GGUF not configured. Run:\n"
            "  uv run aegis pull-model --model bge-base-en\n"
            "  echo 'AEGIS_EMBEDDING_MODEL_PATH=$(pwd)/models/"
            "bge-base-en-v1.5-q4_k_m.gguf' >> .env\n"
            "  uv sync --extra local-llm"
        ), file=sys.stderr)
        return 2

    provider = BGELocalEmbedding()

    if args.action == "build":
        from aegis.burnin.m13_data import generate
        corpus = generate(per_category=args.per_category, seed=args.seed)
        print(
            f"[case-memory build] embedding {len(corpus)} synthetic examples "
            f"through BGE-base-en…"
        )
        memory = CaseMemory.build_from_corpus(
            corpus, embed_provider=provider,
            meta={"source": "synthetic", "per_category": args.per_category,
                  "seed": args.seed},
        )
    elif args.action == "import":
        if not args.corpus:
            print(_red("--corpus is required for `import`"), file=sys.stderr)
            return 2
        import json as _json
        import time as _time

        from aegis.burnin.m13_data import LabeledExample
        from aegis.schema import ATVHeader, ATVInput, CostEfficiencyMetrics

        corpus_objs = []
        with Path(args.corpus).open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                rec = _json.loads(line)
                corpus_objs.append(LabeledExample(
                    category=rec.get("category", "shadow"),
                    label=rec["label"],
                    inp=ATVInput(
                        header=ATVHeader(
                            trace_id=rec.get("trace_id", "t"),
                            span_id=rec.get("span_id", "s"),
                            tenant_id=rec.get("tenant_id", "shadow"),
                            aid=rec.get("aid", "shadow"),
                            timestamp_ns=_time.time_ns(),
                        ),
                        agent_state_text=rec.get("agent_state_text", ""),
                        plan_text=rec.get("plan_text", ""),
                        tool_name=rec["tool_name"],
                        tool_args_json=rec["tool_args_json"],
                        safety_flags={},
                        memory_fingerprint="sha3:shadow",
                        cost_estimate=CostEfficiencyMetrics(
                            input_token_count=10, output_token_count=5,
                        ),
                    ),
                ))
        print(
            f"[case-memory import] embedding {len(corpus_objs)} shadow records "
            f"through BGE-base-en…"
        )
        memory = CaseMemory.build_from_corpus(
            corpus_objs, embed_provider=provider,
            meta={"source": "shadow", "input_path": str(args.corpus)},
        )
    else:
        print(_red(f"unknown action: {args.action}"), file=sys.stderr)
        return 2

    memory.save(out_path)
    print(_green(f"✓ wrote {out_path}"))
    print(f"  n={memory.n}, dim={memory.dim}")
    print()
    print("Next steps:")
    print(
        "  1. Verify the memory has semantic structure:\n"
        "       uv run aegis case-memory status"
    )
    print(
        "  2. Restart Claude Code so the hook picks up the new memory\n"
        "     (the LocalPhiJudge prompt builder loads it on first call).\n"
    )
    print(
        "  3. Run the dogfood check to confirm RAG fires:\n"
        "       ./scripts/dogfood_check.sh --hybrid"
    )
    return 0


def cmd_session(args: argparse.Namespace) -> int:
    """Inspect / clear the per-session behavioural-drift store.

    The local hook persists one JSON file per Claude Code session in
    ``~/.aegis/sessions/`` (override with ``$AEGIS_SESSION_DIR``).
    Each file holds the BGE anchor embedding + Welford running stats
    + the last 32 cosine drifts, so the encoder can fill the
    ``session_behavioral_drift`` ATV slot with a real signal instead
    of zeros.

    Three actions:

    * ``list`` — table of (session_id, n_calls, age, max_drift),
      sorted by recency.
    * ``show`` — full JSON for one session (``--id <session_id>``).
    * ``clear`` — delete all session files except the
      ``--keep N`` most-recent (default keep=0, i.e. delete all).
    """
    from datetime import datetime

    from aegis.atv import session_drift

    if args.action == "list":
        sessions = session_drift.list_sessions()
        if not sessions:
            print("[session] no sessions yet")
            print(
                f"  (sessions are persisted to "
                f"{session_drift.session_dir()}/ once the hook fires)"
            )
            return 0
        print(
            f"{'session_id':<20} {'n_calls':>8} {'age':>8} "
            f"{'max_drift':>10} {'started (UTC)':<20}"
        )
        print("─" * 78)
        now_ns = time.time_ns()
        for s in sessions[: args.limit]:
            age_s = max(0, (now_ns - int(s.get("last_seen_ns", 0))) / 1e9)
            age = f"{age_s/60:.0f}m" if age_s < 3600 else f"{age_s/3600:.1f}h"
            started = datetime.fromtimestamp(
                int(s.get("started_at_ns", 0)) / 1e9, tz=UTC,
            ).strftime("%Y-%m-%d %H:%M:%S")
            print(
                f"{str(s['session_id'])[:20]:<20} "
                f"{s['n_calls']:>8} {age:>8} "
                f"{s['max_drift']:>10.3f} {started:<20}"
            )
        return 0

    if args.action == "show":
        if not args.id:
            print(_red("--id required for `show`"), file=sys.stderr)
            return 2
        state = session_drift.load_session(args.id)
        if state is None:
            print(_yellow(f"no session found: {args.id}"))
            return 1
        # Print without the giant 768-D anchor vector.
        d = state.to_json()
        if d.get("anchor_embedding") is not None:
            d["anchor_embedding"] = (
                f"<768-D vector, first 4: "
                f"{[round(float(x), 3) for x in d['anchor_embedding'][:4]]}…>"
            )
        print(json.dumps(d, indent=2))
        return 0

    if args.action == "clear":
        n = session_drift.clear_sessions(keep_recent=args.keep)
        print(_green(f"✓ removed {n} session file(s) "
                     f"(kept {args.keep} most-recent)"))
        return 0

    print(_red(f"unknown action: {args.action}"), file=sys.stderr)
    return 2


def cmd_sidecar(args: argparse.Namespace) -> int:
    """Manage the local LLM-keep-alive daemon.

    The daemon (``aegis.judge.llm_daemon``) is a one-purpose process:
    it keeps a Llama-cpp GGUF resident in memory and serves
    ``evaluate`` requests over a Unix socket. This eliminates the
    per-PreToolUse cold load — Llama-1B drops from 2.1 s to ~150 ms,
    Phi-3.5-mini from 6.5 s to ~150 ms (the latter is what makes
    Phi-3.5 actually viable under Claude Code's 5 s hook timeout).

    Three actions:

    * ``start`` — spawn the daemon as a detached subprocess. Writes
      ``~/.aegis/llm_sidecar.pid`` (PID + model path + sock path)
      and creates ``~/.aegis/llm_sidecar.sock``. Polls the socket
      until the daemon is ready or the timeout expires.
    * ``stop``  — read the PID file, send SIGTERM, wait for the
      socket to disappear. Idempotent (no-op if already stopped).
    * ``status`` — table form: PID, uptime, model path + hash,
      requests served. Useful for verifying the daemon is alive
      AND serving the GGUF you expect.

    The daemon is **optional** — when not running, ``LocalPhiJudge``
    silently falls back to per-call in-process loading. That's the
    pre-PR-#30 behaviour, so existing installs keep working
    unchanged.
    """
    from aegis.judge.llm_daemon import (
        DaemonClient,
        is_pid_alive,
        read_pid_file,
    )

    if args.action == "status":
        info = read_pid_file()
        if info is None:
            print(_yellow("[sidecar] not running (no PID file)"))
            return 0
        pid = int(info.get("pid", 0))
        if not is_pid_alive(pid):
            print(_yellow(
                f"[sidecar] PID {pid} is not alive — stale PID file at "
                f"~/.aegis/llm_sidecar.pid (run `aegis sidecar stop` to clean)"
            ))
            return 1
        client = DaemonClient()
        ping = client.ping()
        if ping is None:
            print(_yellow(
                f"[sidecar] PID {pid} alive but socket unresponsive — "
                f"daemon may still be loading model. Try again in a few seconds."
            ))
            return 1
        print(_green("✓ sidecar running"))
        print(f"  pid:        {pid}")
        print(f"  model:      {info.get('model_path', '?')}")
        print(f"  model_hash: {ping.get('model_hash', '?')[:32]}…")
        print(f"  uptime:     {ping.get('uptime_s', 0):.1f} s")
        print(f"  served:     {ping.get('requests_served', 0)} request(s)")
        print(f"  socket:     {info.get('sock_path', '?')}")
        return 0

    if args.action == "stop":
        info = read_pid_file()
        if info is None:
            print(_yellow("[sidecar] not running (no PID file) — nothing to stop"))
            return 0
        pid = int(info.get("pid", 0))
        if not is_pid_alive(pid):
            # Stale PID file — clean it up.
            from aegis.judge.llm_daemon import _pid_path, _sock_path
            for p in (_pid_path(), _sock_path()):
                if p.exists():
                    p.unlink()
            print(_yellow(f"[sidecar] PID {pid} not alive; cleaned up stale state"))
            return 0
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError as e:
            print(_red(f"[sidecar] failed to SIGTERM PID {pid}: {e}"), file=sys.stderr)
            return 1
        # Wait up to 10 s for socket to disappear (clean shutdown).
        from aegis.judge.llm_daemon import _sock_path

        sp = _sock_path()
        deadline = time.time() + 10.0
        while time.time() < deadline:
            if not sp.exists() and not is_pid_alive(pid):
                print(_green(f"✓ sidecar stopped (PID {pid})"))
                return 0
            time.sleep(0.1)
        print(_yellow(
            f"[sidecar] SIGTERM sent but daemon hasn't exited after 10 s. "
            f"Try `kill -9 {pid}` if it's stuck."
        ))
        return 1

    if args.action == "start":
        existing = read_pid_file()
        if existing is not None and is_pid_alive(int(existing.get("pid", 0))):
            print(_yellow(
                f"[sidecar] already running (PID {existing.get('pid')}). "
                "Use `aegis sidecar stop` first to restart."
            ))
            return 0

        # Resolve the GGUF path: --model wins, else AEGIS_JUDGE_MODEL_PATH,
        # else fail with a helpful message.
        model_path = args.model or os.environ.get("AEGIS_JUDGE_MODEL_PATH", "")
        if not model_path or not Path(model_path).exists():
            print(_red(
                f"GGUF path not found: {model_path or '(unset)'}\n"
                f"  set AEGIS_JUDGE_MODEL_PATH or pass --model PATH\n"
                f"  download with: uv run aegis pull-model"
            ), file=sys.stderr)
            return 2

        # Spawn detached subprocess that runs `python -m aegis.judge.llm_daemon
        # serve <path>`. We use a child runner script in tools/ so the daemon
        # process tree is grokkable in `ps`.
        py = _hook_python_executable()
        log_path = Path.home() / ".aegis" / "llm_sidecar.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a") as logfh:
            import subprocess
            proc = subprocess.Popen(
                [py, "-c",
                 "import sys; from aegis.judge.llm_daemon import serve_forever; "
                 f"serve_forever({model_path!r})"],
                stdout=logfh, stderr=logfh, stdin=subprocess.DEVNULL,
                start_new_session=True,
                env={**os.environ, "PYTHONPATH": str(SRC_DIR)},
            )

        # Poll for readiness — model load can take 2-7 s depending on size.
        from aegis.judge.llm_daemon import DaemonClient
        client = DaemonClient()
        deadline = time.time() + 30.0
        while time.time() < deadline:
            if proc.poll() is not None:
                print(_red(
                    f"[sidecar] daemon exited rc={proc.returncode} during start. "
                    f"Check log: {log_path}"
                ), file=sys.stderr)
                return 1
            ping = client.ping(timeout_s=1.0)
            if ping is not None:
                model_name = Path(model_path).name
                print(_green("✓ sidecar started"))
                print(f"  pid:        {proc.pid}")
                print(f"  model:      {model_name}")
                print(f"  model_hash: {ping.get('model_hash', '?')[:32]}…")
                print("  socket:     ~/.aegis/llm_sidecar.sock")
                print(f"  log:        {log_path}")
                print()
                print("Hooks will now use the daemon for all step340 LLM calls.")
                print("Stop with: aegis sidecar stop")
                return 0
            time.sleep(0.5)
        print(_red(
            f"[sidecar] daemon failed to come up within 30 s. "
            f"Check log: {log_path}"
        ), file=sys.stderr)
        return 1

    print(_red(f"unknown action: {args.action}"), file=sys.stderr)
    return 2


def cmd_audit(args: argparse.Namespace) -> int:
    """Inspect / rotate / verify the local audit log.

    Three actions:

    * ``list``   — table of (active + rotated) audit files with size +
      record count, plus aggregate.
    * ``rotate`` — manually trigger size-based rotation (otherwise
      happens automatically when the active file exceeds
      ``AEGIS_AUDIT_MAX_BYTES``, default 50 MB).
    * ``verify`` — alias for :func:`cmd_verify_audit`. Walks the entire
      rotation set in append order and checks the SHA3 chain.

    The audit log lives at ``$AEGIS_LOCAL_AUDIT`` (or
    ``~/.aegis/audit.jsonl``). Rotated files share the same parent
    dir with names ``audit.jsonl.1``, ``audit.jsonl.2``, etc. — most-
    recent rotation has the lowest number.
    """
    from aegis.audit.rotation import (
        list_rotation_chain,
        max_bytes,
        max_rotations,
        total_size,
    )
    from aegis.audit.rotation import (
        rotate as do_rotate,
    )

    audit_path = (
        Path(args.audit) if args.audit
        else Path(os.environ.get("AEGIS_LOCAL_AUDIT", ""))
        if os.environ.get("AEGIS_LOCAL_AUDIT", "")
        else Path.home() / ".aegis" / "audit.jsonl"
    )

    if args.action == "list":
        files = list_rotation_chain(audit_path)
        if not files:
            print(f"[audit] no audit files at {audit_path}")
            return 0
        print(f"[audit] threshold={max_bytes() // 1024 // 1024} MB, "
              f"keep={max_rotations()} rotations, "
              f"total={total_size(audit_path) // 1024} KB")
        print()
        print(f"  {'file':<24}  {'size (KB)':>10}  {'records':>10}")
        print("  " + "─" * 50)
        for f in files:
            n_records = sum(1 for line in f.read_text(encoding="utf-8").splitlines() if line.strip())
            print(
                f"  {f.name:<24}  {f.stat().st_size // 1024:>10}  {n_records:>10}"
            )
        return 0

    if args.action == "rotate":
        from aegis.audit.rotation import slot_path

        if not audit_path.exists():
            print(_yellow(f"no active audit log at {audit_path}"))
            return 0
        size_before = audit_path.stat().st_size
        new_top = do_rotate(audit_path)
        if new_top == 0:
            print(_yellow(
                f"rotation skipped (max_rotations={max_rotations()}, "
                f"max_bytes={max_bytes()})"
            ))
            return 0
        # The rotated file may be `.1.gz` (post-PR) or `.1` (legacy /
        # gzip step failed). Report whichever is actually on disk so
        # the operator sees the real filename.
        new_path = slot_path(audit_path, new_top)
        new_name = new_path.name if new_path else f"{audit_path.name}.{new_top}"
        print(_green(
            f"✓ rotated {audit_path.name} "
            f"({size_before // 1024} KB) → "
            f"{new_name}"
        ))
        files = list_rotation_chain(audit_path)
        for f in files:
            print(f"    {f.name}  ({f.stat().st_size // 1024} KB)")
        return 0

    if args.action == "verify":
        # Delegate to the existing verify-audit command — that walker
        # was updated in this PR to traverse the rotation chain.
        ns = argparse.Namespace(audit=str(audit_path))
        return cmd_verify_audit(ns)

    if args.action == "status":
        return _cmd_audit_status(audit_path, json_out=getattr(args, "json", False))

    if args.action == "prune":
        return _cmd_audit_prune(audit_path, keep=int(args.keep))

    print(_red(f"unknown action: {args.action}"), file=sys.stderr)
    return 2


def _cmd_audit_status(audit_path: Path, *, json_out: bool) -> int:
    """`aegis audit status` — operator-friendly summary of disk usage.

    Renders a human table by default; ``--json`` emits the raw dict
    from :func:`aegis.audit.rotation.status` for jq / fleet-monitor
    consumption.
    """
    from aegis.audit.rotation import status as rotation_status

    s = rotation_status(audit_path)

    if json_out:
        print(json.dumps(s, indent=2, sort_keys=True, default=str))
        return 0

    active_kb = int(s["active_bytes"]) // 1024  # type: ignore[arg-type]
    threshold_mb = int(s["threshold_bytes"]) // (1024 * 1024)  # type: ignore[arg-type]
    total_kb = int(s["total_bytes"]) // 1024  # type: ignore[arg-type]
    keep = int(s["max_rotations"])  # type: ignore[arg-type]
    rotate_daily = bool(s["rotate_daily"])
    slots = s["rotation_slots"]
    assert isinstance(slots, list)

    print(f"audit log:        {s['active_path']}")
    if s["active_exists"]:
        print(f"active size:      {active_kb} KB")
    else:
        print(f"active size:      {_yellow('(file absent — never written, or just rotated)')}")
    print(f"rotation cap:     {threshold_mb} MB per slot, "
          f"keep last {keep} slot(s)")
    if rotate_daily:
        print(f"daily rotation:   {_green('on')} (UTC midnight trigger)")
    print(f"total disk used:  {total_kb} KB across active + rotations")
    print()

    if not slots:
        print(_yellow("  no rotated files yet"))
    else:
        print(f"  {'slot':<6}  {'format':<10}  {'size (KB)':>10}  path")
        print("  " + "─" * 70)
        for slot in slots:
            assert isinstance(slot, dict)
            n = slot["n"]
            fmt = "gzip" if slot["compressed"] else "plain (legacy)"
            size_kb = int(slot["bytes"]) // 1024  # type: ignore[arg-type]
            print(f"  .{n:<5}  {fmt:<10}  {size_kb:>10}  {slot['path']}")
    return 0


def _cmd_audit_prune(audit_path: Path, *, keep: int) -> int:
    """`aegis audit prune --keep N` — drop rotation slots above N.

    Operator surface for "free disk space immediately, even though
    we're not at the size threshold yet". Useful before a backup,
    before disk runs out, or to trim retention without lowering
    ``AEGIS_AUDIT_MAX_ROTATIONS`` permanently.

    Never touches the active file. ``keep=0`` removes every rotation.
    """
    from aegis.audit.rotation import prune as do_prune

    if keep < 0:
        print(_red(f"--keep must be >= 0, got {keep}"), file=sys.stderr)
        return 2

    removed = do_prune(audit_path, keep=keep)
    if not removed:
        print(_yellow(
            f"no rotated files above slot {keep} — nothing to prune"
        ))
        return 0
    print(_green(f"✓ pruned {len(removed)} rotated file(s):"))
    for p in removed:
        print(f"    {p.name}")
    return 0


def cmd_cost_record(args: argparse.Namespace) -> int:
    """Manually record token usage — deferred (D10).

    Sidecar mode persists cost via the M12 Cost Attestation Ledger
    (Ed25519-signed, separate keypair). The plugin-mode equivalent —
    cost.catalog + wal.writer — is part of the D10 milestone and
    not yet shipped.
    """
    print(
        _yellow(
            "[cost-record] manual cost recording is D10 deferred."
        )
    )
    print(
        f"              (requested: inv={args.invocation_id} "
        f"model={args.model} tokens={args.tokens_in}+{args.tokens_out})"
    )
    print(
        "              Sidecar mode persists this via the M12 ledger; "
        "see `aegis cost-import transcript` for plugin-mode backfill."
    )
    return 1


def cmd_soak(args: argparse.Namespace) -> int:
    """``aegis soak`` and ``aegis bench`` — load test harness.

    ``aegis soak`` is the long-form (default 24h) production sign-
    off command. ``aegis bench`` is the same harness with shorter
    defaults (5 min / 50 RPS / fail-fast on first chain break) for
    CI smoke + dev-loop validation.

    The harness lives at :mod:`aegis.soak`; this function is just
    the CLI glue (parse args → ``SoakConfig`` → ``run_soak`` →
    print).
    """
    import asyncio

    from aegis.soak import (
        SoakConfig,
        SoakThresholds,
        _format_result_human,
        run_soak,
        write_result_json,
    )

    duration_s = _parse_duration(args.duration)
    rate = _parse_rate(args.rate)

    thresholds = SoakThresholds(
        max_error_rate=float(args.max_error_rate),
        max_p99_latency_ms=float(args.max_p99_ms),
        require_clean_chain=not args.no_chain_check,
        min_throughput_rps=float(args.min_throughput),
    )
    config = SoakConfig(
        target_url=args.target,
        duration_s=duration_s,
        rate_per_s=rate,
        concurrency=int(args.concurrency),
        timeout_s=float(args.timeout),
        seed=int(args.seed),
        chain_verify_interval_s=float(args.chain_interval),
        thresholds=thresholds,
    )

    print(f"target:          {config.target_url}")
    print(f"duration:        {duration_s:.0f}s")
    print(f"rate:            {rate:.2f}/s")
    print(f"concurrency:     {config.concurrency}")
    print("thresholds:")
    print(f"  max error:     {thresholds.max_error_rate * 100:.2f}%")
    print(f"  max p99:       {thresholds.max_p99_latency_ms:.0f}ms")
    print(f"  chain check:   {'on' if thresholds.require_clean_chain else 'off'}")
    if thresholds.min_throughput_rps > 0:
        print(f"  min throughput:{thresholds.min_throughput_rps:.2f}/s")
    print()
    print("running ...")

    def _progress(r: object) -> None:
        # Lightweight live-progress printer.
        from aegis.soak import SoakResult
        if not isinstance(r, SoakResult):
            return
        elapsed = (time.time_ns() - r.started_at_ns) / 1e9
        print(
            f"  [{int(elapsed):>5}s] sent={r.n_requested} "
            f"errors={r.n_errors} "
            f"p99={r.latency.percentile(0.99):.1f}ms",
            flush=True,
        )

    try:
        result = asyncio.run(run_soak(
            config,
            log_progress_fn=_progress,
        ))
    except KeyboardInterrupt:
        print(_yellow("\nsoak interrupted"))
        return 130

    print()
    print(_format_result_human(result))

    if args.output:
        out_path = Path(args.output)
        write_result_json(result, out_path)
        print()
        print(f"  JSON written: {out_path}")

    return 0 if result.pass_overall else 1


def _parse_duration(raw: str) -> float:
    """Parse e.g. ``"24h"``, ``"30m"``, ``"3600"`` → seconds."""
    raw = raw.strip().lower()
    if raw.endswith("h"):
        return float(raw[:-1]) * 3600
    if raw.endswith("m"):
        return float(raw[:-1]) * 60
    if raw.endswith("s"):
        return float(raw[:-1])
    return float(raw)


def _parse_rate(raw: str) -> float:
    """Parse e.g. ``"10/s"``, ``"600/min"``, ``"50"`` → requests/sec."""
    raw = raw.strip().lower()
    if "/s" in raw:
        return float(raw.replace("/s", ""))
    if "/min" in raw or "/m" in raw:
        return float(raw.replace("/min", "").replace("/m", "")) / 60.0
    return float(raw)


def cmd_cost_import(args: argparse.Namespace) -> int:
    if args.source == "transcript":
        from aegis.cost.transcript import import_into_wal

        r = import_into_wal(Path(args.path))
        print(f"[cost-import transcript] {r}")
        return 0 if r.get("status") == "imported" else 1
    if args.source == "admin-api":
        # PR #4 — Anthropic Admin API integration.
        from aegis.cost.usage_api import (
            fetch,
            per_model_breakdown,
            total_billed,
        )

        admin_key = (
            getattr(args, "admin_key", None)
            or os.environ.get("ANTHROPIC_ADMIN_KEY")
        )
        if not admin_key:
            print(
                _red(
                    "[cost-import admin-api] ANTHROPIC_ADMIN_KEY env var "
                    "or --admin-key flag required"
                ),
                file=sys.stderr,
            )
            print(
                "                        Get the admin key at "
                "https://console.anthropic.com/settings/admin-keys "
                "(separate from your regular API key).",
                file=sys.stderr,
            )
            return 2
        result = fetch(
            admin_key=admin_key,
            since=args.since,
            group_by=["model"],
        )
        if result.error:
            print(_red(f"[cost-import admin-api] {result.error}"), file=sys.stderr)
            return 1
        print("[cost-import admin-api] fetched from Anthropic")
        print(f"  window:  {result.requested_starting_at} → "
              f"{result.requested_ending_at}")
        print(f"  pages:   {result.pages_fetched}")
        print(f"  records: {len(result.records)}")
        print(f"  total billed: ${total_billed(result.records):.4f}")
        print()
        breakdown = per_model_breakdown(result.records)
        if breakdown:
            print("  per-model breakdown:")
            for model, m in sorted(
                breakdown.items(),
                key=lambda kv: kv[1]["billed_dollars"], reverse=True,
            ):
                print(
                    f"    {model:<28}  "
                    f"in={int(m['input_tokens']):>10,}  "
                    f"out={int(m['output_tokens']):>10,}  "
                    f"cache_r={int(m['cache_read']):>11,}  "
                    f"cache_w={int(m['cache_creation']):>9,}  "
                    f"${m['billed_dollars']:>9.4f}"
                )
        return 0
    return 2


def cmd_policy(args: argparse.Namespace) -> int:
    """`aegis policy {diff,log,show}` — explore the RAG corpus history.

    * ``diff --since SPEC`` — chunks added / retired / superseded in
      the window. SPEC accepts ISO date, ISO datetime, relative
      (``7d``/``2w``/``3m``/``1y``), quarter (``2024-Q1``), or
      ``all``.
    * ``log [--limit N]`` — chronological timeline of mutations.
    * ``show CHUNK_ID`` — single-chunk detail with predecessor /
      successor links.
    """
    from aegis.judge.policy_diff import (
        diff_since,
        log_entries,
        parse_since,
        render_diff,
        render_log,
        render_show,
        show_chunk,
    )
    from aegis.judge.rag_corpus import load_default_corpus, reset_corpus_cache

    reset_corpus_cache()
    corpus = load_default_corpus()

    if args.action == "diff":
        try:
            since_ns = parse_since(args.since)
        except ValueError as exc:
            print(_red(str(exc)), file=sys.stderr)
            return 2
        diff = diff_since(corpus, since_ns)
        print(render_diff(diff))
        return 0
    if args.action == "log":
        entries = log_entries(corpus, limit=args.limit)
        print(render_log(entries))
        return 0
    if args.action == "show":
        shown = show_chunk(corpus, args.chunk_id)
        if shown is None:
            print(_red(f"[policy show] chunk {args.chunk_id!r} not found"),
                  file=sys.stderr)
            return 1
        print(render_show(shown))
        return 0
    return 2


def cmd_budget(args: argparse.Namespace) -> int:
    """Persistent per-tenant budget config (PR #5).

    * ``aegis budget show`` — list every persisted tenant + the
      default fallback.
    * ``aegis budget set --tenant T --daily X [--per-call Y]`` —
      upsert one tenant's budget. step335 reads from this on
      every PreToolUse.
    * ``aegis budget delete --tenant T`` — drop a row.
    """
    from aegis.cost.budget_store import (
        DEFAULT_DAILY_DOLLARS,
        BudgetStore,
    )

    store = BudgetStore()
    tenant = (
        getattr(args, "tenant", None) or "default"
    )

    if args.action == "show":
        budgets = store.list_all()
        if not budgets:
            print(_yellow(
                f"[budget show] no persisted budgets — default daily "
                f"ceiling is ${DEFAULT_DAILY_DOLLARS:.2f}"
            ))
            return 0
        print("[budget show] persisted budgets:")
        print(f"  {'tenant':<24}  {'daily $':>10}  {'per-call $':>12}  "
              f"{'updated':>20}")
        for b in budgets:
            from datetime import datetime
            updated = datetime.fromtimestamp(
                b.updated_at_ns / 1_000_000_000
            ).isoformat(timespec="seconds")
            pc = (
                f"${b.per_call_dollars:.4f}"
                if b.per_call_dollars is not None
                else "—"
            )
            print(
                f"  {b.tenant_id:<24}  ${b.daily_dollars:>9.4f}  "
                f"{pc:>12}  {updated:>20}"
            )
        return 0

    if args.action == "set":
        if args.daily is None:
            print(_red("[budget set] --daily X required"), file=sys.stderr)
            return 2
        try:
            b = store.set(
                tenant,
                daily_dollars=float(args.daily),
                per_call_dollars=(
                    float(args.per_call)
                    if args.per_call is not None else None
                ),
            )
        except ValueError as e:
            print(_red(f"[budget set] {e}"), file=sys.stderr)
            return 2
        print(_green(f"✓ budget set for tenant '{b.tenant_id}'"))
        print(f"  daily:    ${b.daily_dollars:.4f}")
        if b.per_call_dollars is not None:
            print(f"  per call: ${b.per_call_dollars:.4f}")
        return 0

    if args.action == "delete":
        deleted = store.delete(tenant)
        if deleted:
            print(_green(f"✓ deleted budget for tenant '{tenant}'"))
            return 0
        print(_yellow(f"[budget delete] no budget for tenant '{tenant}'"))
        return 1

    return 2


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="aegis")
    sub = ap.add_subparsers(dest="cmd", required=True)

    st = sub.add_parser(
        "status",
        help="📊 ATV Live — operational status + optional performance dashboard",
    )
    st.add_argument(
        "--performance",
        action="store_true",
        help=(
            "Append a performance dashboard: cumulative cache_hit_rate, "
            "billed dollars, inefficiency totals across all Stop-hook "
            "session retrospectives in the local audit chain"
        ),
    )
    st.add_argument(
        "--json",
        action="store_true",
        help=(
            "(--performance only) emit the PerformanceSummary as JSON "
            "to stdout, instead of the human-readable rendering"
        ),
    )
    st.add_argument(
        "--redact",
        action="store_true",
        help=(
            "(--performance only) redact sensitive fields: absolute "
            "billed_dollars become $-relative ratios, session ts becomes "
            "day-precision quantized, audit path hashed. Use this when "
            "sharing the dashboard in support tickets / public logs."
        ),
    )
    st.set_defaults(fn=cmd_status)
    va = sub.add_parser(
        "verify-audit",
        help="Verify the local audit chain integrity (Solo Free, v2.1.5)",
    )
    va.add_argument(
        "--audit",
        help="Path to audit JSONL (default: ~/.aegis/audit.jsonl, the local-mode log)",
    )
    va.set_defaults(fn=cmd_verify_audit)

    ak = sub.add_parser(
        "audit-key",
        help="(neutral) Manage the optional Ed25519 audit-signing key (v4.4)",
    )
    ak_sub = ak.add_subparsers(dest="action", required=False)
    ak_init = ak_sub.add_parser(
        "init",
        help=(
            "Generate a fresh Ed25519 keypair at "
            "~/.aegis/keys/audit.ed25519{,.pub}. Subsequent audit "
            "appends will sign records; `aegis verify-audit` will "
            "verify them."
        ),
    )
    ak_init.add_argument(
        "--force",
        action="store_true",
        help=(
            "Overwrite an existing key. WARNING: invalidates signatures "
            "in any prior audit chain — only do this when rotating."
        ),
    )
    ak_init.set_defaults(fn=cmd_audit_key)
    ak_show = ak_sub.add_parser(
        "show",
        help="Print the audit signing key fingerprint + paths",
    )
    ak_show.set_defaults(fn=cmd_audit_key)
    ak.set_defaults(fn=cmd_audit_key)

    rp = sub.add_parser("replay")
    rp.add_argument("n", type=int, nargs="?", default=20)
    rp.set_defaults(fn=cmd_replay)

    pr = sub.add_parser("policy-replay")
    pr.add_argument("--since", default="1970-01-01")
    pr.add_argument("--policy", default=None)
    pr.add_argument("--limit", type=int, default=10000)
    pr.set_defaults(fn=cmd_policy_replay)

    co = sub.add_parser(
        "cost",
        help="📊 ATV Live — cost rollup (`summary`) and 🏋️ ATV Coach what-if replay (`replay`).",
    )
    co_sub = co.add_subparsers(dest="action")
    co_sum = co_sub.add_parser(
        "summary",
        help="Aggregate ~/.aegis/audit.jsonl: max cumulative $, escalations, "
        "per-tool, per-session, spikes.",
    )
    co_sum.add_argument(
        "--audit",
        default=None,
        help="Audit JSONL path (default: ~/.aegis/audit.jsonl).",
    )
    co_sum.add_argument(
        "--spike-threshold",
        type=float,
        default=0.10,
        help="Min $ jump within a session that counts as a spike event "
        "(default: 0.10).",
    )
    co_sum.add_argument("--json", action="store_true", help="Emit JSON instead of a table.")
    co_sum.add_argument(
        "--by-aid",
        action="store_true",
        help=(
            "Show ONLY the per-aid (per-agent / per-session) cost "
            "breakdown, expanded to all aids and sorted descending. "
            "Designed for multi-agent ops (Telegram bot per channel, "
            "Claude Code subagents, OpenClaw multi-provider, etc.)."
        ),
    )
    co_sum.add_argument(
        "--top",
        type=int,
        default=10,
        help=(
            "(--by-aid) Show only the top N aids by max cumulative $. "
            "Default 10. Use --top 0 for all aids."
        ),
    )
    co_sum.set_defaults(fn=cmd_cost)
    co_rep = co_sub.add_parser(
        "replay",
        help="Replay a Claude Code transcript through the firewall offline. "
        "Useful for what-if budget / model / attack experiments.",
    )
    co_rep.add_argument(
        "transcript",
        help="Path to a Claude Code transcript .jsonl",
    )
    co_rep.add_argument(
        "--budget",
        type=float,
        default=1.0,
        help="Budget ceiling in dollars (default: 1.0 — same as DEFAULT_BUDGET).",
    )
    co_rep.add_argument(
        "--model",
        default="claude-haiku-4-5",
        help="Model name for the FLOPS table → cumulative_dollars conversion.",
    )
    co_rep.add_argument(
        "--hw-provider",
        choices=["none", "sim"],
        default="none",
        help="HW band source. Use `sim` to enable step337 + M12 cost-divergence.",
    )
    co_rep.add_argument(
        "--hw-attack",
        default="",
        help="Comma-separated subset of "
        "{token_flops_mismatch,hbm_exfil,cost_underreport,thermal_spike,"
        "network_exfil,iommu_violation} (only meaningful with --hw-provider sim).",
    )
    co_rep.add_argument(
        "--multiplier",
        type=float,
        default=3.0,
        help="M12 escalation multiplier × baseline (default: 3.0 — Claim 27).",
    )
    co_rep.add_argument("--json", action="store_true", help="Emit JSON instead of a table.")
    co_rep.set_defaults(fn=cmd_cost)

    co_mult = co_sub.add_parser(
        "multi-agent",
        help="Replay N transcripts as a fleet, fire warn/hard-stop "
        "thresholds on cumulative fleet cost. Useful for "
        "what-if multi-agent budget experiments.",
    )
    co_mult.add_argument(
        "--transcripts",
        required=True,
        help="Comma-separated list of transcript .jsonl paths (one per agent).",
    )
    co_mult.add_argument(
        "--per-agent-budget",
        type=float,
        default=1.0,
        help="step335 ceiling per agent (default: 1.0).",
    )
    co_mult.add_argument(
        "--threshold",
        type=float,
        default=None,
        help="Fleet $ at which to fire a WARN crossing (default: 5.0).",
    )
    co_mult.add_argument(
        "--hard-stop",
        type=float,
        default=None,
        help="Fleet $ at which to ABORT the replay (default: 20.0).",
    )
    co_mult.add_argument(
        "--interactive",
        action="store_true",
        help="Prompt y/N from stdin at each crossing — empty/no → abort.",
    )
    co_mult.add_argument(
        "--model",
        default="claude-haiku-4-5",
        help="Model name for the FLOPS table (per-agent).",
    )
    co_mult.add_argument(
        "--hw-provider",
        choices=["none", "sim"],
        default="none",
        help="HW band source (per-agent).",
    )
    co_mult.add_argument(
        "--hw-attack",
        default="",
        help="HW attack to inject (only with --hw-provider sim).",
    )
    co_mult.add_argument(
        "--multiplier",
        type=float,
        default=3.0,
        help="M12 escalation multiplier × baseline (default: 3.0).",
    )
    co_mult.add_argument("--json", action="store_true", help="Emit JSON instead of a table.")
    co_mult.set_defaults(fn=cmd_cost)

    co.set_defaults(fn=cmd_cost, action=None)

    fm = sub.add_parser(
        "fleet-monitor",
        help="📊 ATV Live — multi-session cost daemon. "
        "Tails ~/.aegis/audit.jsonl, fires notifier on threshold "
        "crossings.",
    )
    fm.add_argument(
        "action",
        choices=["start", "stop", "status"],
        help="Daemon lifecycle.",
    )
    fm.add_argument("--threshold", type=float, default=None,
                    help="Fleet $ for WARN notifications.")
    fm.add_argument("--hard-stop", type=float, default=None,
                    help="Fleet $ that writes a stop-flag for hook polling.")
    fm.add_argument("--slack-url-env", default=None,
                    help="ENV var name holding the Slack webhook URL.")
    fm.add_argument("--ntfy-topic-env", default=None,
                    help="ENV var name holding the ntfy.sh topic "
                    "(free phone push, no signup).")
    fm.add_argument("--ntfy-base-url", default="https://ntfy.sh",
                    help="ntfy server (default: https://ntfy.sh; "
                    "override for self-hosted).")
    fm.add_argument("--crossings-log", default=None,
                    help="Append every crossing to this JSONL file. "
                    "Use ~/.aegis/crossings.jsonl for the canonical "
                    "audit location.")
    fm.add_argument("--interactive", action="store_true",
                    help="Stderr notifier reads y/N from stdin.")
    fm.add_argument("--audit", default=None,
                    help="Audit JSONL path (default: ~/.aegis/audit.jsonl).")
    fm.add_argument("--poll-interval", type=float, default=1.0,
                    help="Seconds between polls (default: 1.0).")
    fm.set_defaults(fn=cmd_fleet_monitor)

    sub.add_parser(
        "health",
        help="🔧 ATV Doctor — Aegis self-health check (firewall hook, audit log, key, drift).",
    ).set_defaults(fn=cmd_health)

    mt = sub.add_parser(
        "metrics",
        help=(
            "📊 ATV Live (OpenClaw + Local OSS LLM track) — scrape "
            "self-hosted inference server /metrics and print KV cache "
            "/ throughput / queue-depth snapshot. "
            "Currently supports vLLM; Ollama / TGI follow."
        ),
    )
    mt.add_argument(
        "--vllm-url",
        default="http://localhost:8000",
        help=(
            "vLLM server base URL (default: http://localhost:8000). "
            "/metrics is appended automatically. Ignored when "
            "--all or --aid is set."
        ),
    )
    mt.add_argument(
        "--all",
        action="store_true",
        help=(
            "Scrape every endpoint in ~/.aegis/inference.toml "
            "concurrently. Cloud / disabled endpoints are listed "
            "but not scraped. Gap B (issue #145)."
        ),
    )
    mt.add_argument(
        "--aid",
        type=str,
        default=None,
        help=(
            "Scrape exactly one endpoint from the registry, "
            "selected by its [endpoints.<aid>] label."
        ),
    )
    mt.add_argument(
        "--timeout",
        type=float,
        default=2.0,
        help="HTTP timeout in seconds (default: 2.0).",
    )
    mt.add_argument(
        "--json",
        action="store_true",
        help=(
            "Emit a single JSON object instead of the human-readable "
            "table. Useful for piping into jq / fleet-monitor."
        ),
    )
    mt.set_defaults(fn=cmd_metrics)

    # ── `aegis license <action>` — Solo Pro / Team / Enterprise gate ──
    lic = sub.add_parser(
        "license",
        help=(
            "Activate / inspect / verify a Solo Pro / Team / Enterprise "
            "license. Solo Free needs no license — these commands are "
            "for paying tiers. See PRICING.md and docs/LICENSE_KEY.md."
        ),
    )
    lic_sub = lic.add_subparsers(dest="action", required=True)

    lic_activate = lic_sub.add_parser(
        "activate",
        help="Verify a license file and install it as the active license.",
    )
    lic_activate.add_argument(
        "path", type=str,
        help="Path to a JWS license token file (typically supplied by the issuer service).",
    )

    lic_sub.add_parser(
        "status",
        help=(
            "Print the active tier + license_id + expiry. "
            "Solo Free if no key is installed."
        ),
    )

    lic_sub.add_parser(
        "deactivate",
        help="Remove the active license file; revert to Solo Free.",
    )

    lic_verify = lic_sub.add_parser(
        "verify",
        help=(
            "Validate a license file *without* activating. "
            "Useful for CI / dry-run before commit."
        ),
    )
    lic_verify.add_argument(
        "path", type=str, help="Path to a JWS license token file.",
    )

    lic_sub.add_parser(
        "refresh",
        help=(
            "(Opt-in) Refresh the certificate-revocation list. "
            "Not yet active — issuer service pending. See "
            "docs/LICENSE_KEY.md §3."
        ),
    )

    lic.set_defaults(fn=cmd_license)

    rb = sub.add_parser(
        "rollback",
        help="🔧 ATV Doctor — revert a destructive tool call by trace / session / time window.",
    )
    rb.add_argument("invocation_id", nargs="?", default=None)
    rb.add_argument("--allow-git", action="store_true")
    rb.add_argument("--dry-run", action="store_true")
    rb.add_argument("--session", help="Restore all from session/agent id")
    rb.add_argument("--since", help="Restore all snapshots since ISO datetime")
    rb.set_defaults(fn=cmd_rollback)

    sn = sub.add_parser(
        "snapshots",
        help="🔧 ATV Doctor — list / prune ATMU rollback snapshots.",
    )
    sn.add_argument("action", nargs="?", default="list", choices=["list", "prune"])
    sn.add_argument("--limit", type=int, default=50)
    sn.add_argument(
        "--older-than", default="7d", help="prune snapshots older than: 7d / 24h"
    )
    sn.set_defaults(fn=cmd_snapshots)

    bn = sub.add_parser(
        "burnin",
        help="🏋️ ATV Coach — burn-in (5-layer × 4-phase) train / revert / shadow / export.",
    )
    bn.add_argument(
        "action",
        choices=[
            "retrain", "revert", "train-m13", "compare-m13",
            "shadow-status", "export-baseline",
        ],
        help=(
            "retrain: Burn-in Shadow phase (M11) iforest baseline. "
            "revert: roll back to previous baseline. "
            "train-m13: learn M13 attribution-head v2 weights. "
            "compare-m13: side-by-side v1 vs v2 evaluation. "
            "shadow-status: summarise the Burn-in Shadow log. "
            "export-baseline: write a per-tenant baseline RAG chunk "
            "to policies/rag_corpus/baselines.jsonl from the local "
            "audit log."
        ),
    )
    bn.add_argument(
        "--since", default="30d", help="time window: 30d / 24h / ISO-date"
    )
    bn.add_argument("--dry-run", action="store_true")
    # train-m13 specific (ignored by retrain/revert):
    bn.add_argument(
        "--corpus", default=None,
        help=(
            "(train-m13) JSONL path of (ATV, label) pairs from a "
            "Burn-in Shadow dump. Default: auto-generate synthetic."
        ),
    )
    bn.add_argument(
        "--per-category", type=int, default=35,
        help="(train-m13) synthetic examples per category (default: 35)",
    )
    bn.add_argument(
        "--seed", type=int, default=2026_05_03,
        help="(train-m13) RNG seed for synthetic generation (default: 2026_05_03)",
    )
    bn.add_argument(
        "--test-fraction", type=float, default=0.2,
        help="(train-m13) held-out fraction (default: 0.2)",
    )
    bn.add_argument(
        "--out", default=None,
        help="(train-m13) output JSON path (default: models/m13_attribution_head_v2.json)",
    )
    # compare-m13 specific:
    bn.add_argument(
        "--v1", default=None,
        help="(compare-m13) v1 weights JSON path "
             "(default: models/m13_attribution_head_v1.json)",
    )
    bn.add_argument(
        "--v2", default=None,
        help="(compare-m13) v2 weights JSON path "
             "(default: models/m13_attribution_head_v2.json)",
    )
    # shadow-status specific:
    bn.add_argument(
        "--shadow-log", default=None,
        help="(shadow-status) shadow JSONL path (default: $AEGIS_SHADOW_LOG or ~/.aegis/shadow.jsonl)",
    )
    # export-baseline specific:
    bn.add_argument(
        "--audit", default=None,
        help=(
            "(export-baseline) audit JSONL path "
            "(default: ~/.aegis/audit.jsonl)"
        ),
    )
    bn.add_argument(
        "--tenant", default="local",
        help=(
            "(export-baseline) tenant identifier embedded in the "
            "RAG baseline chunk (default: local)"
        ),
    )
    bn.add_argument(
        "--rotate", action="store_true",
        help=(
            "(export-baseline) append a new datestamped chunk and "
            "seal the previous open baseline for this tenant with "
            "valid_until=<now>. Default off → overwrite-mode."
        ),
    )
    bn.set_defaults(fn=cmd_burnin)

    # v2.7.2 Phase D — calibration feedback loop. Walks the local
    # audit.jsonl, reports per-signal accuracy from accumulated
    # PostToolUse retrospectives, and (with --apply) recomputes the
    # M13 / session-drift percentile thresholds for the gate.
    ac = sub.add_parser(
        "advisor-calibration",
        help=(
            "🏋️ ATV Coach — inspect / retrain the advisor-gate calibration "
            "(M13 confidence + session_drift percentile thresholds) "
            "from accumulated audit retrospectives (Phase D)."
        ),
    )
    ac.add_argument(
        "action",
        choices=["analyse", "recommend", "apply"],
        help=(
            "analyse: print per-signal accuracy from audit. "
            "recommend: also print proposed new thresholds (dry-run). "
            "apply: persist the recommended calibration to "
            "models/advisor_calibration_v1.json."
        ),
    )
    ac.add_argument(
        "--audit", default=None,
        help=(
            "audit.jsonl path "
            "(default: $AEGIS_LOCAL_AUDIT or ~/.aegis/audit.jsonl)"
        ),
    )
    ac.add_argument(
        "--output", default=None,
        help=(
            "(apply) where to write the new calibration JSON "
            "(default: models/advisor_calibration_v1.json)"
        ),
    )
    ac.set_defaults(fn=cmd_advisor_calibration)

    cm = sub.add_parser(
        "case-memory",
        help="🏋️ ATV Coach — build / inspect the step340 RAG case memory (nearest-neighbour index)",
    )
    cm.add_argument(
        "action",
        choices=["build", "import", "status"],
        help=(
            "build: embed synthetic M13 corpus into models/case_memory_v1.npz. "
            "import: embed a Burn-in Shadow JSONL (--corpus). "
            "status: show count + label distribution."
        ),
    )
    cm.add_argument(
        "--corpus", default=None,
        help="(import) shadow JSONL produced via AEGIS_BURNIN_SHADOW=1",
    )
    cm.add_argument(
        "--per-category", type=int, default=35,
        help="(build) synthetic examples per category (default: 35)",
    )
    cm.add_argument(
        "--seed", type=int, default=2026_05_03,
        help="(build) RNG seed (default: 2026_05_03)",
    )
    cm.add_argument(
        "--out", default=None,
        help="output npz path (default: models/case_memory_v1.npz)",
    )
    cm.set_defaults(fn=cmd_case_memory)

    se = sub.add_parser(
        "session",
        help="📊 ATV Live — inspect / clear the per-session behavioural-drift store",
    )
    se.add_argument(
        "action",
        choices=["list", "show", "clear"],
        help=(
            "list: table of recent sessions; "
            "show: full JSON for one session (--id); "
            "clear: delete session files (--keep N preserves most-recent)."
        ),
    )
    se.add_argument(
        "--id", default=None, help="(show) session_id to inspect",
    )
    se.add_argument(
        "--keep", type=int, default=0,
        help="(clear) number of most-recent sessions to preserve (default: 0)",
    )
    se.add_argument(
        "--limit", type=int, default=20,
        help="(list) max rows shown (default: 20)",
    )
    se.set_defaults(fn=cmd_session)

    au = sub.add_parser(
        "audit",
        help="Inspect / rotate / prune / verify the local audit log + rotations",
    )
    au.add_argument(
        "action",
        choices=["list", "rotate", "verify", "status", "prune"],
        help=(
            "list:   table of audit files with sizes + record counts; "
            "rotate: manually trigger size-based rotation; "
            "verify: walk the full rotation chain and check SHA3 integrity; "
            "status: operator summary of disk usage, compression, retention; "
            "prune:  drop rotation slots above --keep (free disk space)."
        ),
    )
    au.add_argument(
        "--audit", default=None,
        help="audit log path (default: $AEGIS_LOCAL_AUDIT or ~/.aegis/audit.jsonl)",
    )
    au.add_argument(
        "--keep", type=int, default=1,
        help="(prune) retain rotation slots .1..N, drop the rest (default: 1)",
    )
    au.add_argument(
        "--json", action="store_true",
        help="(status) emit a JSON object instead of the human table",
    )
    au.set_defaults(fn=cmd_audit)

    # ── `aegis soak` and `aegis bench` — load test harness ──────────
    # Two parsers sharing one handler. Defaults differ (soak is the
    # 24h sign-off; bench is the 5min CI smoke).
    def _add_soak_args(parser: argparse.ArgumentParser, *, smoke: bool) -> None:
        parser.add_argument(
            "--target", default="http://localhost:8000",
            help="Sidecar base URL (default: http://localhost:8000)",
        )
        parser.add_argument(
            "--duration", default="5m" if smoke else "24h",
            help=(
                "Total wall-clock duration (e.g. '24h', '30m', '300'). "
                f"Default: {'5m (bench)' if smoke else '24h (soak)'}"
            ),
        )
        parser.add_argument(
            "--rate", default="50/s" if smoke else "10/s",
            help=(
                "Request rate (e.g. '10/s', '600/min', '50'). "
                f"Default: {'50/s (bench)' if smoke else '10/s (soak)'}"
            ),
        )
        parser.add_argument(
            "--concurrency", type=int, default=8 if smoke else 16,
            help="Worker pool size (default: 8 bench / 16 soak)",
        )
        parser.add_argument(
            "--timeout", type=float, default=5.0,
            help="Per-request timeout in seconds (default: 5.0)",
        )
        parser.add_argument(
            "--seed", type=int, default=42,
            help="RNG seed for reproducible payload mix (default: 42)",
        )
        parser.add_argument(
            "--chain-interval", type=float, default=60.0 if smoke else 600.0,
            help=(
                "Audit-chain verify cadence in seconds "
                f"(default: {'60 (bench)' if smoke else '600 (soak)'})"
            ),
        )
        parser.add_argument(
            "--max-error-rate", type=float, default=0.01,
            help="Pass/fail: max allowed error rate (default: 0.01 = 1%%)",
        )
        parser.add_argument(
            "--max-p99-ms", type=float, default=500.0,
            help="Pass/fail: max p99 latency in ms (default: 500)",
        )
        parser.add_argument(
            "--min-throughput", type=float, default=0.0,
            help=(
                "Pass/fail: min completed RPS (0 = no floor, default). "
                "Catches backpressure that silently caps the harness."
            ),
        )
        parser.add_argument(
            "--no-chain-check", action="store_true",
            help=(
                "Skip the periodic audit-chain verification. "
                "Use only when targeting a sidecar without chain access."
            ),
        )
        parser.add_argument(
            "--output",
            help=(
                "Write the JSON result to this path (atomic). "
                "Useful for CI gates and fleet-monitor ingestion."
            ),
        )

    soak_p = sub.add_parser(
        "soak",
        help=(
            "Load test the sidecar — 24h-default duration, configurable "
            "rate. Periodic audit-chain verification mid-run. Pass/fail "
            "by error rate + p99 latency + chain integrity."
        ),
    )
    _add_soak_args(soak_p, smoke=False)
    soak_p.set_defaults(fn=cmd_soak)

    bench_p = sub.add_parser(
        "bench",
        help=(
            "Same harness as `aegis soak`, scaled down to 5min/50 RPS — "
            "intended for CI smoke + dev-loop validation."
        ),
    )
    _add_soak_args(bench_p, smoke=True)
    bench_p.set_defaults(fn=cmd_soak)

    sc = sub.add_parser(
        "sidecar",
        help="Manage the local LLM-keep-alive daemon (eliminates cold-load on every PreToolUse)",
    )
    sc.add_argument(
        "action",
        choices=["start", "stop", "status"],
        help=(
            "start: spawn the daemon (loads GGUF once, listens on Unix socket); "
            "stop: SIGTERM + clean up state; "
            "status: liveness + model hash + uptime."
        ),
    )
    sc.add_argument(
        "--model", default=None,
        help="(start) GGUF path (default: $AEGIS_JUDGE_MODEL_PATH)",
    )
    sc.set_defaults(fn=cmd_sidecar)

    pm = sub.add_parser(
        "pull-model",
        help="Download a Solo Free local-sLLM GGUF into ./models/",
    )
    from aegis.judge.model_registry import (
        DEFAULT_MODEL_NAME,
        list_aliases,
        list_models,
    )
    _model_choices = sorted(list_aliases().keys())
    pm.add_argument(
        "--model",
        choices=_model_choices,
        default=DEFAULT_MODEL_NAME,
        help=(
            f"GGUF to fetch (default: {DEFAULT_MODEL_NAME}). "
            f"Run `aegis pull-model --list` for full table or "
            f"`--recommend` for use-case guidance. Aliases (e.g. "
            f"`phi3-mini`) resolve to canonical names."
        ),
    )
    pm.add_argument("--list", action="store_true", help="show available models + exit")
    pm.add_argument(
        "--recommend",
        action="store_true",
        help="print judge-model recommendations by use case + exit",
    )
    pm.add_argument(
        "--force", action="store_true",
        help="re-download even if the file is already present",
    )
    _ = list_models  # imported for cmd_pull_model
    pm.set_defaults(fn=cmd_pull_model)

    cr = sub.add_parser(
        "cost-record", help="Manually record token usage for an invocation"
    )
    cr.add_argument("--inv", dest="invocation_id", required=True)
    cr.add_argument("--in", dest="tokens_in", type=int, required=True)
    cr.add_argument("--out", dest="tokens_out", type=int, required=True)
    cr.add_argument("--model", default="default")
    cr.add_argument("--cost", type=float, default=0.0, help="override estimate")
    cr.set_defaults(fn=cmd_cost_record)

    ci = sub.add_parser(
        "cost-import", help="Backfill cost from transcript or Admin API"
    )
    ci.add_argument("source", choices=["transcript", "admin-api"])
    ci.add_argument("--path", help="transcript .jsonl path")
    ci.add_argument("--since", default="30d",
                    help="time window (e.g. 30d / 24h / ISO-8601 datetime)")
    ci.add_argument("--admin-key", default=None,
                    help="Anthropic admin key (or set ANTHROPIC_ADMIN_KEY env)")
    ci.set_defaults(fn=cmd_cost_import)

    bg = sub.add_parser(
        "budget",
        help="Persistent per-tenant budget config (PR #5). step335 "
        "reads these on every PreToolUse.",
    )
    bg.add_argument("action", choices=["show", "set", "delete"])
    bg.add_argument("--tenant", default="default",
                    help="Tenant id (default: 'default').")
    bg.add_argument("--daily", type=float, default=None,
                    help="Daily ceiling in dollars (required for `set`).")
    bg.add_argument("--per-call", type=float, dest="per_call", default=None,
                    help="Optional per-call ceiling.")
    bg.set_defaults(fn=cmd_budget)

    pol = sub.add_parser(
        "policy",
        help=(
            "Explore the RAG corpus history derived from chunk "
            "timestamps (PR ③ of v3.1 temporal-RAG track)."
        ),
    )
    pol.add_argument(
        "action", choices=["diff", "log", "show"],
        help=(
            "diff: chunks added/retired/superseded in a window. "
            "log: chronological mutation timeline. "
            "show: single-chunk detail with supersession links."
        ),
    )
    pol.add_argument(
        "--since", default="30d",
        help=(
            "(diff) time window: ISO date (2024-08-01), ISO datetime, "
            "relative (7d / 2w / 3m / 1y), quarter (2024-Q1), or "
            "'all'. Default: 30d."
        ),
    )
    pol.add_argument(
        "--limit", type=int, default=20,
        help="(log) max entries to show (default: 20; 0 = unlimited)",
    )
    pol.add_argument(
        "chunk_id", nargs="?", default=None,
        help="(show) chunk id to inspect.",
    )
    pol.set_defaults(fn=cmd_policy)

    bl = sub.add_parser(
        "baseline",
        help="Manage the instruction baseline (CLAUDE.md / AGENTS.md / .mcp.json)",
    )
    bl.add_argument(
        "action",
        choices=["init", "status", "reattest"],
        help="init: snapshot files; status: diff vs baseline; reattest: overwrite",
    )
    bl.add_argument(
        "--root",
        help="Repo root to walk (default: current working directory)",
    )
    bl.add_argument(
        "--baseline",
        help="Manifest path (default: .aegis/instruction_baseline.json under cwd)",
    )
    bl.add_argument(
        "--force",
        action="store_true",
        help="(init) overwrite existing baseline manifest",
    )
    bl.add_argument(
        "--include-model-weights",
        action="store_true",
        help=(
            "PR-E (OpenClaw + Local OSS LLM track) — also baseline "
            "self-hosted LLM weight files (GGUF / safetensors / etc.) "
            "under common locations (models/*.gguf, models/*.safetensors). "
            "Step309 then BLOCKs every PreToolUse if any model file's "
            "SHA3 changes — catches supply-chain tampering AND silent "
            "quantization swaps. Use with `init` to capture, with "
            "`reattest` to update after a deliberate model upgrade."
        ),
    )
    bl.add_argument(
        "--model-weight-paths",
        nargs="+",
        default=None,
        help=(
            "PR-E — explicit list of glob patterns to baseline as "
            "model weights. Overrides --include-model-weights default "
            "patterns. Use for non-standard model layouts, e.g.: "
            "--model-weight-paths "
            "/opt/llama/*.gguf vendor/safetensors/*.bin"
        ),
    )
    bl.set_defaults(fn=cmd_baseline)

    rep = sub.add_parser(
        "report",
        help="📊 ATV Live — 5-line Agent Risk Report from the local audit log",
    )
    rep.add_argument(
        "--audit",
        help="Path to audit JSONL (default: ~/.aegis/audit.jsonl, the local-mode log)",
    )
    rep.add_argument(
        "--since",
        help="Time window: '24h', '7d', '3600' (seconds)",
    )
    rep.add_argument(
        "--verbose", "-v", action="store_true",
        help="Show top reasons table",
    )
    rep.add_argument(
        "--explain",
        default=None,
        metavar="TRACE_OR_LAST",
        help=(
            "Render a layer-by-layer explanation of one decision: which "
            "firewall steps fired, M13 attribution top contributors, "
            "RAG cases retrieved, session drift. Pass a trace_id prefix "
            "or 'LAST' / 'last' for the most-recent decision."
        ),
    )
    rep.add_argument(
        "--json",
        action="store_true",
        help=(
            "(--explain only) emit the audit record + explain block as a "
            "single line of JSON to stdout, instead of the human-readable "
            "rendering. Schema = the audit record itself, stable for "
            "jq / CI integration."
        ),
    )
    rep.add_argument(
        "--by-aid",
        action="store_true",
        help=(
            "Multi-agent breakdown: print one risk-summary block per aid "
            "(per-agent / per-session) instead of a single global aggregate. "
            "Shows the same emoji counts the default report has, but "
            "attributed to each agent. Sub-agent rows (Claude Code Task "
            "subagents) are flagged with [sidechain]."
        ),
    )
    rep.add_argument(
        "--by-channel",
        action="store_true",
        help=(
            "OpenClaw multi-channel breakdown (PR-D): print one risk-"
            "summary block per `channel` (telegram / discord / slack / "
            "cli / web / custom). Records without a channel field "
            "(Claude Code track) are bucketed under '(no-channel)'. "
            "Useful when one OpenClaw deployment serves multiple chat "
            "platforms and you want per-channel BLOCK / approval rates."
        ),
    )
    rep.add_argument(
        "--by-provider",
        action="store_true",
        help=(
            "OpenClaw multi-provider breakdown (PR-F): print one risk-"
            "summary block per LLM provider (anthropic-claude / "
            "openai-gpt-4o / google-gemini / local-llama / etc.). "
            "Records without a provider field bucket under "
            "'(no-provider)'. Surfaces provider-drift — when the same "
            "agent's verdict distribution diverges across providers, "
            "the provider-divergence advisor flags the gap as a "
            "vendor-switch risk signal."
        ),
    )
    rep.add_argument(
        "--by-aid-and-provider",
        action="store_true",
        help=(
            "Multi-agent + multi-LLM cross-grouping (Gap A): for each "
            "aid, print a sub-block per provider it used. Surfaces "
            "per-agent provider drift (e.g., 'Agent A was high-block "
            "rate on Claude but dropped sharply after migrating to "
            "GPT — investigate'). Useful for OpenClaw deployments "
            "where multiple agents each pick their own LLM (some "
            "local, some cloud). Also activated implicitly when both "
            "--by-aid and --by-provider are passed together."
        ),
    )
    rep.add_argument(
        "--with-live",
        action="store_true",
        help=(
            "(--by-aid-and-provider only) Cross-reference live "
            "inference metrics from ~/.aegis/inference.toml. For each "
            "aid, prints a 'live: KV=87%% queue=2/0 band=high' line "
            "below its provider sub-rows. Cloud / disabled / "
            "unreachable endpoints render 'live: skipped' or "
            "'live: unreachable'. Adds a per-aid network round-trip "
            "to /metrics during report generation. Gap B (issue #145)."
        ),
    )
    rep.set_defaults(fn=cmd_report)

    # ── PR-C: aegis forensic <selector> ────────────────────────────
    fr = sub.add_parser(
        "forensic",
        help=(
            "🔧 ATV Doctor — postmortem timeline for one session "
            "(decisions / reasons / step traces / advisor signals / "
            "per-step latency if AEGIS_STEP_TIMING_ENABLED). "
            "Read-only over ~/.aegis/audit.jsonl."
        ),
    )
    fr.add_argument(
        "selector",
        help=(
            "AID (session id) to filter by — exact match. "
            "Pass 'last' / 'LAST' for the most-recent AID. "
            "A trace_id prefix also works for cross-session lookup."
        ),
    )
    fr.add_argument(
        "--trace",
        default=None,
        help="(optional) narrow to a single trace_id within the session",
    )
    fr.add_argument(
        "--audit",
        default=None,
        help="audit log path (default: ~/.aegis/audit.jsonl)",
    )
    fr.add_argument(
        "--since",
        default=None,
        metavar="DURATION",
        help="only records within this window, e.g. 7d / 24h / 3600",
    )
    fr.add_argument(
        "--limit",
        type=int,
        default=0,
        help="show only the most-recent N matching records (0 = all)",
    )
    fr.add_argument(
        "--json",
        action="store_true",
        help=(
            "emit a single JSON object {selector, count, records[]} for "
            "downstream tooling (jq / spreadsheet / blog post)"
        ),
    )
    fr.add_argument(
        "--logits",
        action="store_true",
        help=(
            "OpenClaw + Local OSS LLM (PR-G): show per-record logit "
            "metrics (mean / min logprob, low-confidence token count + "
            "sample) when present in the audit record's "
            "explain.logit_metrics block. Cloud LLM tracks have no "
            "logit data — this flag is a no-op for them. Wire vLLM's "
            "--return-logprobs into your sidecar to populate the data."
        ),
    )
    fr.set_defaults(fn=cmd_forensic)

    # ── PR-F: aegis advise [selector] ──────────────────────────────
    ad = sub.add_parser(
        "advise",
        help=(
            "🔧 ATV Doctor — advisor recommendations from recent audit "
            "records, grouped by domain (cost / performance / security). "
            "Reads explain.action_advice written by --profile pro/cloud."
        ),
    )
    ad.add_argument(
        "selector", nargs="?", default="last",
        help=(
            "AID for cross-session aggregation; 'last' (default) for "
            "the most-recent AID's recommendations; 'all' for every "
            "session in the window."
        ),
    )
    ad.add_argument(
        "--audit", default=None,
        help="audit log path (default: ~/.aegis/audit.jsonl)",
    )
    ad.add_argument(
        "--since", default="7d", metavar="DURATION",
        help="time window (e.g. 7d / 24h / 3600). default: 7d",
    )
    ad.add_argument(
        "--category",
        choices=["cost", "performance", "security", "all"],
        default="all",
        help="filter to one advisor domain (default: all)",
    )
    ad.add_argument(
        "--priority",
        choices=["high", "medium", "low", "all"],
        default="all",
        help="filter by recommendation priority (default: all)",
    )
    ad.add_argument(
        "--limit", type=int, default=20,
        help="max recommendations to render (default: 20)",
    )
    ad.add_argument(
        "--json", action="store_true",
        help=(
            "emit a single JSON object with recommendations[] + "
            "category_counts for downstream tooling"
        ),
    )
    ad.set_defaults(fn=cmd_advise)

    cl = sub.add_parser(
        "cache-lint",
        help=(
            "Diagnose Anthropic prompt-cache breakage in a Claude Code "
            "transcript and/or a system-prompt template"
        ),
    )
    cl.add_argument(
        "--transcript",
        help="Claude Code transcript .jsonl to scan for cache breaks",
    )
    cl.add_argument(
        "--system-prompt",
        dest="system_prompt",
        help=(
            "Path to a system-prompt / tool-catalog file to static-lint "
            "for cache-breaking anti-patterns (dates, UUIDs, etc.)"
        ),
    )
    cl.add_argument(
        "--break-threshold",
        dest="break_threshold",
        type=float,
        default=30.0,
        help=(
            "Percentage-point drop required to flag a cache break "
            "(default: 30.0)"
        ),
    )
    cl.add_argument(
        "--compare-with",
        dest="compare_with",
        help=(
            "Closed-loop verification: treat --transcript as the AFTER "
            "(post-fix) session and this path as the BEFORE baseline. "
            "Diffs the two cache_lint reports + reports the realisation rate."
        ),
    )
    cl.add_argument(
        "--json",
        action="store_true",
        help=(
            "Emit the full CacheLintReport (or comparison) as JSON to stdout"
        ),
    )
    cl.set_defaults(fn=cmd_cache_lint)

    inst = sub.add_parser("install", help="Install hooks into ~/.claude/settings.json")
    inst.add_argument(
        "--target",
        choices=["claude-code", "openclaw-local", "openclaw-cloud"],
        default="claude-code",
        help=(
            "Release track to install. "
            "claude-code (GA): patches ~/.claude/settings.json with the "
            "Aegis PreToolUse hook + slash commands (current default). "
            "openclaw-local (Preview): OpenClaw + self-hosted LLM "
            "(vLLM/Ollama) for air-gapped operation. "
            "openclaw-cloud (Preview): OpenClaw + cloud LLM (Anthropic/"
            "OpenAI/Google) for multi-channel agent ops. "
            "Preview tracks print a roadmap message — see "
            "docs/releases/README.md for details. "
            "default: claude-code"
        ),
    )
    inst.add_argument(
        "--mode",
        choices=["sidecar", "local"],
        default="sidecar",
        help=(
            "sidecar: hook POSTs to localhost:8000/evaluate (requires "
            "`docker compose up -d`). local: hook runs the firewall "
            "in-process (Solo Free, no service needed). default: sidecar"
        ),
    )
    inst.add_argument(
        "--force", action="store_true", help="add hook even if already installed"
    )
    inst.add_argument(
        "--profile",
        choices=list(VALID_LOCAL_PROFILES),
        default="free",
        help=(
            "(--mode local only) intelligence-tier preset. "
            "free: dummy embedding + dummy judge + advisor OFF (today's "
            "default; 0 cloud calls, 0 model files). "
            "pro: bge-local + hybrid (M13 attribution head + local-phi) + "
            "advisor heuristic; auto-pulls ~700 MB GGUF; still 0 cloud "
            "calls. "
            "cloud: same as pro plus haiku judge for grey-zone calls; "
            "requires ANTHROPIC_API_KEY. "
            "Explicit --judge / --embedding override the profile baseline. "
            "default: free"
        ),
    )
    inst.add_argument(
        "--judge",
        choices=list(VALID_LOCAL_JUDGES),
        default=None,
        help=(
            "(--mode local only) sLLM judge stack — overrides --profile. "
            "dummy: keyword-only (fastest, may miss AWS-secret + loop "
            "scenarios). hybrid: heuristic + keyword + M13 attribution "
            "head (recommended for real coding-AI traffic, still offline). "
            "default: per --profile (free=dummy, pro/cloud=hybrid)"
        ),
    )
    inst.add_argument(
        "--embedding",
        choices=list(VALID_LOCAL_EMBEDDINGS),
        default=None,
        help=(
            "(--mode local only) embedding provider for ATV agent_state "
            "and action_history slots — overrides --profile. dummy: "
            "deterministic SHA3 noise (no semantic similarity, no install). "
            "bge-local: real BGE-base-en-v1.5 GGUF via llama-cpp (~100 MB). "
            "default: per --profile (free=dummy, pro/cloud=bge-local)"
        ),
    )
    inst.add_argument(
        "--rescue", action="store_true",
        help=(
            "Restore ~/.claude/settings.json from the most-recent "
            "settings.json.bak.<ts> backup. Use when a previous install "
            "left the user locked out (cost gate, missing flags, etc.)."
        ),
    )
    inst.add_argument(
        "--no-commands", action="store_true",
        help=(
            "Skip installing Claude Code custom slash commands "
            "(/aegis-report, /aegis-verify, /aegis-advise, "
            "/aegis-forensic, /aegis-help) into ~/.claude/commands/. "
            "Default: install them so users can invoke Aegis without "
            "leaving Claude Code."
        ),
    )
    inst.set_defaults(fn=cmd_install)

    un = sub.add_parser(
        "uninstall",
        help="Remove Aegis-owned hooks from ~/.claude/settings.json",
    )
    un.add_argument(
        "--dry-run", action="store_true",
        help="show which hook entries would be removed without writing settings.json",
    )
    un.add_argument(
        "--no-backup", action="store_true",
        help="skip the settings.json.bak.<ts> safety copy (default: backup before write)",
    )
    un.set_defaults(fn=cmd_uninstall)

    return ap


def main() -> int:
    args = build_parser().parse_args()
    return int(args.fn(args))


if __name__ == "__main__":
    raise SystemExit(main())
