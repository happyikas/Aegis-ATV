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
import json
import shutil
import sqlite3
import sys
import time
from pathlib import Path

DB = Path(".aegis/wal.db")
HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent
HOOK_SCRIPT = HERE / "aegis_hook.py"               # sidecar mode (POST /evaluate)
LOCAL_HOOK_SCRIPT = HERE / "aegis_local_hook.py"   # local mode (in-process)
STOP_HOOK_SCRIPT = HERE / "hooks" / "session_end.py"
PLUGIN_MANIFEST = PROJECT_ROOT / ".claude-plugin" / "plugin.json"
POLICIES_DIR = PROJECT_ROOT / "policies"
SRC_DIR = PROJECT_ROOT / "src"
SETTINGS_PATH = Path.home() / ".claude" / "settings.json"


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


def cmd_status(_: argparse.Namespace) -> int:
    from cost.tracker import daily_spend_today, total_usd  # type: ignore[import-not-found]
    from crypto.anchor import list_anchors  # type: ignore[import-not-found]
    from monitor.malfunction import overall  # type: ignore[import-not-found]
    from sllm.router import cache_stats  # type: ignore[import-not-found]

    c = _conn()
    n_intents = c.execute("SELECT COUNT(*) FROM intents").fetchone()[0]
    n_blocks = c.execute(
        "SELECT COUNT(*) FROM intents WHERE verdict LIKE '%block%'"
    ).fetchone()[0]
    n_outcomes = c.execute("SELECT COUNT(*) FROM outcomes").fetchone()[0]
    anchors = list_anchors(limit=5)
    cache = cache_stats()
    h = overall()
    print("AegisData status")
    print("================")
    print(f"  intents:    {n_intents:>8,}  ({n_blocks} blocked)")
    print(f"  outcomes:   {n_outcomes:>8,}")
    print(f"  cache:      {cache['size']}/{cache['max']}")
    print(f"  spend today:${daily_spend_today():>7.2f}  total ${total_usd():.2f}")
    print(
        f"  health:     {h['signal']:>8}  "
        f"(err={h['error_rate']:.2f}  loop={h['atv_loop']:.2f}  "
        f"drift={h['schema_drift']:.2f})"
    )
    last_anchor = anchors[-1]["root"][:16] if anchors else "-"
    print(f"  anchors:    {len(anchors)}  (last: {last_anchor}…)")
    return 0


def cmd_verify_audit(_: argparse.Namespace) -> int:
    from crypto.signing import verify_intent  # type: ignore[import-not-found]

    c = _conn()
    rows = c.execute(
        "SELECT aid, atv_hash, verdict, signature FROM intents ORDER BY id"
    ).fetchall()
    ok = bad = 0
    for aid, atv_hash, verdict_json, sig in rows:
        verdict = json.loads(verdict_json)
        if verify_intent(atv_hash, verdict, aid, sig):
            ok += 1
        else:
            bad += 1
    print(f"[verify-audit] {ok}/{ok + bad} signatures valid")
    return 0 if bad == 0 else 1


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
    from cost.tracker import daily_breakdown, per_agent, total_usd  # type: ignore[import-not-found]

    print(f"[cost] total = ${total_usd():.2f}")
    print()
    print("  Daily breakdown (last", args.days, "days):")
    for day, usd, n in daily_breakdown(days=args.days):
        print(f"    {day}  ${usd:>9.2f}  ({n:>5} calls)")
    print()
    print("  Top agents by spend:")
    for aid, usd, n in per_agent()[:10]:
        print(f"    {aid:<24}  ${usd:>9.2f}  ({n:>5} calls)")
    return 0


def cmd_health(_: argparse.Namespace) -> int:
    from monitor.malfunction import overall  # type: ignore[import-not-found]

    h = overall()
    print(f"[health] signal = {h['signal'].upper()}  (overall={h['score']})")
    print(f"  error_rate:  {h['error_rate']:.3f}")
    print(f"  atv_loop:    {h['atv_loop']:.3f}")
    print(f"  schema_drift:{h['schema_drift']:.3f}")
    print(f"  window:      last {h['window']} events")
    return 0 if h["signal"] != "critical" else 1


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


def _build_pretool_command(mode: str) -> str:
    """Compose the shell command embedded into the PreToolUse hook.

    Sidecar mode uses the existing ``tools/aegis_hook.py`` (POST /evaluate);
    local mode uses ``tools/aegis_local_hook.py`` (in-process firewall) and
    pre-pends:

    * ``AEGIS_EMBEDDING_PROVIDER=dummy``  — Solo Free runs without an
      OpenAI key by default; users who explicitly want real embeddings
      can edit settings.json by hand.
    * ``AEGIS_JUDGE_PROVIDER=dummy``      — same rationale, dummy
      regex judge instead of Claude Haiku.
    * ``AEGIS_POLICY_DIR``                — absolute path to policies/
      so step310 can find sensitive_paths.json from any cwd.
    * ``PYTHONPATH``                      — absolute path to src/ so the
      spawned subprocess resolves the ``aegis`` package without the
      user having to ``uv sync`` first.
    """
    if mode == "local":
        return (
            f"AEGIS_EMBEDDING_PROVIDER=dummy "
            f"AEGIS_JUDGE_PROVIDER=dummy "
            f"AEGIS_POLICY_DIR={POLICIES_DIR} "
            f"PYTHONPATH={SRC_DIR} "
            f"python3 {LOCAL_HOOK_SCRIPT}"
        )
    return f"python3 {HOOK_SCRIPT}"


def _pretool_hook_marker(mode: str) -> str:
    """Substring searched in existing settings to detect a prior install."""
    return str(LOCAL_HOOK_SCRIPT) if mode == "local" else str(HOOK_SCRIPT)


def cmd_report(args: argparse.Namespace) -> int:
    """Print a 5-line Agent Risk Report for the most recent session.

    Reads ``~/.aegis/audit.jsonl`` (local mode) or the path passed via
    ``--audit``. Aggregates by decision + reason + redundant flag and
    prints an emoji-led summary mirroring the report shape from the
    must-install strategy doc:

        ✅ N safe tool calls auto-approved
        ⚠️ K high-risk actions required approval
        ⛔ B destructive commands blocked
        ⛔ P poisoned-instruction sources detected
        💸 D redundant calls deduplicated
        🔁 L potential loops aborted
        🧾 Full signed local audit: <path>
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

    cutoff_ns = int(time.time() - since_secs) * 1_000_000_000 if since_secs else 0

    n_total = 0
    n_safe = 0
    n_approval = 0
    n_block_destructive = 0
    n_block_poisoned = 0
    n_loop_aborted = 0
    n_redundant = 0
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
            ts = int(rec.get("ts_ns") or 0)
            if cutoff_ns and ts and ts < cutoff_ns:
                continue
            decision = (rec.get("decision") or "").upper()
            reason = (rec.get("reason") or "").lower()
            n_total += 1

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

            tag = reason[:60] if reason else f"{decision} (no reason)"
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

    if args.verbose and by_reason:
        print()
        print("  Top reasons (count × tag):")
        top = sorted(by_reason.items(), key=lambda kv: -kv[1])[:10]
        for tag, c in top:
            print(f"    {c:>4} × {tag}")

    return 0


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
    * Stop hook — both modes also register
      ``tools/hooks/session_end.py`` so transcript cost data is
      back-filled when each Claude Code session ends (D6).
    * Plugin manifest validation — refuses to install if
      ``.claude-plugin/plugin.json`` is missing or malformed.
    """
    mode = args.mode

    ok, info = _validate_plugin_manifest()
    if not ok:
        print(_red(info), file=sys.stderr)
        return 1
    print(f"[install] plugin v{info}, mode={mode}")

    pretool_script = LOCAL_HOOK_SCRIPT if mode == "local" else HOOK_SCRIPT
    if not pretool_script.exists():
        print(_red(f"hook script not found: {pretool_script}"), file=sys.stderr)
        return 1
    if not pretool_script.stat().st_mode & 0o100:
        print(_yellow(f"making {pretool_script.name} executable"))
        pretool_script.chmod(pretool_script.stat().st_mode | 0o111)

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

    pretool_cmd = _build_pretool_command(mode)
    pretool_marker = _pretool_hook_marker(mode)
    pretool_entry = {
        "matcher": "*",
        "hooks": [{"type": "command", "command": pretool_cmd}],
    }

    hooks_section = existing.setdefault("hooks", {})
    pretooluse = hooks_section.setdefault("PreToolUse", [])

    for entry in pretooluse:
        for h in entry.get("hooks", []):
            if pretool_marker in h.get("command", "") and not args.force:
                print(_green(f"already installed — {h['command']!r}"))
                print("(re-run with --force to add anyway)")
                return 0

    pretooluse.append(pretool_entry)

    # Always register the Stop hook (D6 cost auto-import); idempotent.
    stop_hooks = hooks_section.setdefault("Stop", [])
    stop_already = any(
        str(STOP_HOOK_SCRIPT) in h.get("command", "")
        for entry in stop_hooks
        for h in entry.get("hooks", [])
    )
    if not stop_already:
        stop_cmd = f"python3 {STOP_HOOK_SCRIPT}"
        stop_hooks.append({"hooks": [{"type": "command", "command": stop_cmd}]})

    SETTINGS_PATH.write_text(json.dumps(existing, indent=2) + "\n")

    print(_green(f"\u2713 installed Aegis hooks → {SETTINGS_PATH}"))
    print(f"  PreToolUse:  {pretool_cmd}")
    if not stop_already:
        print(f"  Stop:        python3 {STOP_HOOK_SCRIPT}")
    print('  matcher: "*" (every tool — narrow this in settings.json if too noisy)')
    print()
    if mode == "sidecar":
        print("Sidecar mode: start the Aegis service with `docker compose up -d`")
        print("  (the hook POSTs to localhost:8000/evaluate)")
    else:
        print("Local mode: in-process firewall — no service needed.")
    print()
    print("Restart Claude Code for the hooks to take effect.")

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


def cmd_burnin(args: argparse.Namespace) -> int:
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
    return 2


def cmd_cost_record(args: argparse.Namespace) -> int:
    from cost.catalog import estimate_usd  # type: ignore[import-not-found]
    from wal.writer import _connect  # type: ignore[import-not-found]

    c = _connect()
    usd = args.cost or estimate_usd(args.model, args.tokens_in, args.tokens_out)
    c.execute(
        """
        INSERT OR REPLACE INTO outcomes
          (ts_ns, invocation_id, status, result_hash,
           tokens_in, tokens_out, model, cost_usd, snapshot_ref)
        VALUES (strftime('%s','now')*1000000000, ?, 'manual', '', ?, ?, ?, ?, '')
        """,
        (args.invocation_id, args.tokens_in, args.tokens_out, args.model, usd),
    )
    print(
        f"[cost record] inv={args.invocation_id}  ${usd:.4f}  "
        f"({args.tokens_in}+{args.tokens_out} tokens, {args.model})"
    )
    return 0


def cmd_cost_import(args: argparse.Namespace) -> int:
    if args.source == "transcript":
        from aegis.cost.transcript import import_into_wal

        r = import_into_wal(Path(args.path))
        print(f"[cost-import transcript] {r}")
        return 0 if r.get("status") == "imported" else 1
    if args.source == "admin-api":
        from cost.usage_api import (  # type: ignore[import-not-found]
            fetch,
            import_into_wal,
        )

        rows = fetch(since=args.since)
        r = import_into_wal(rows)
        print(f"[cost-import admin-api] {r}")
        return 0 if r.get("status") == "imported" else 1
    return 2


def cmd_budget(args: argparse.Namespace) -> int:
    from cost.budget import load, set_daily, set_per_call  # type: ignore[import-not-found]

    if args.action == "show":
        print(f"[budget] {load()}")
        return 0
    if args.action == "set":
        if args.daily is not None:
            set_daily(args.daily)
        if args.per_call is not None:
            set_per_call(args.per_call)
        print(f"[budget set] {load()}")
        return 0
    return 2


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="aegis")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status").set_defaults(fn=cmd_status)
    sub.add_parser("verify-audit").set_defaults(fn=cmd_verify_audit)

    rp = sub.add_parser("replay")
    rp.add_argument("n", type=int, nargs="?", default=20)
    rp.set_defaults(fn=cmd_replay)

    pr = sub.add_parser("policy-replay")
    pr.add_argument("--since", default="1970-01-01")
    pr.add_argument("--policy", default=None)
    pr.add_argument("--limit", type=int, default=10000)
    pr.set_defaults(fn=cmd_policy_replay)

    co = sub.add_parser("cost")
    co.add_argument("--days", type=int, default=7)
    co.set_defaults(fn=cmd_cost)

    sub.add_parser("health").set_defaults(fn=cmd_health)

    rb = sub.add_parser("rollback")
    rb.add_argument("invocation_id", nargs="?", default=None)
    rb.add_argument("--allow-git", action="store_true")
    rb.add_argument("--dry-run", action="store_true")
    rb.add_argument("--session", help="Restore all from session/agent id")
    rb.add_argument("--since", help="Restore all snapshots since ISO datetime")
    rb.set_defaults(fn=cmd_rollback)

    sn = sub.add_parser("snapshots")
    sn.add_argument("action", nargs="?", default="list", choices=["list", "prune"])
    sn.add_argument("--limit", type=int, default=50)
    sn.add_argument(
        "--older-than", default="7d", help="prune snapshots older than: 7d / 24h"
    )
    sn.set_defaults(fn=cmd_snapshots)

    bn = sub.add_parser("burnin")
    bn.add_argument("action", choices=["retrain", "revert"])
    bn.add_argument(
        "--since", default="30d", help="time window: 30d / 24h / ISO-date"
    )
    bn.add_argument("--dry-run", action="store_true")
    bn.set_defaults(fn=cmd_burnin)

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
    ci.add_argument("--since", default="30d")
    ci.set_defaults(fn=cmd_cost_import)

    bg = sub.add_parser("budget", help="Show or set budget limits")
    bg.add_argument("action", choices=["show", "set"])
    bg.add_argument("--daily", type=float)
    bg.add_argument("--per-call", type=float, dest="per_call")
    bg.set_defaults(fn=cmd_budget)

    rep = sub.add_parser(
        "report",
        help="5-line Agent Risk Report from the local audit log",
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
    rep.set_defaults(fn=cmd_report)

    inst = sub.add_parser("install", help="Install hooks into ~/.claude/settings.json")
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
    inst.set_defaults(fn=cmd_install)

    return ap


def main() -> int:
    args = build_parser().parse_args()
    return int(args.fn(args))


if __name__ == "__main__":
    raise SystemExit(main())
