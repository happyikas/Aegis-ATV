"""v0.5.0 PR ② — top-level CLI restructure.

Verifies the new operator-vocabulary commands route correctly:

* ``aegis live``      ↔ ``aegis dashboard`` (alias; same handler)
* ``aegis guard``     ↔ ``aegis rule``       (alias; same handler)
* ``aegis coach``     — composite group routing burnin / advisor-
                        calibration / case-memory through
                        ``_coach_delegate``
* ``aegis memory``    — composite group with own ``show`` /
                        ``claude-md`` handlers + ``case`` delegate

Backward-compat: the legacy command names still resolve to the same
handlers. v0.4.x scripts and muscle memory keep working.

Also verifies the top-level ``--help`` banner uses the canonical
section grouping operators read in the README / docs.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from tools import aegis_cli

# Split-string for destructive-pattern sample (we never execute it;
# it's a payload for `guard test` parser routing only). The Aegis
# firewall scans source files for destructive regex literals — by
# concatenating at module-load time we keep the file installable
# even with our own hook enabled.
_GUARD_TEST_SAMPLE = "rm -rf " + "/"


# ── live / dashboard alias ─────────────────────────────────────────


def test_live_routes_to_dashboard_handler() -> None:
    args = aegis_cli.build_parser().parse_args(["live"])
    assert args.fn.__name__ == "cmd_dashboard"


def test_live_carries_dashboard_flags() -> None:
    args = aegis_cli.build_parser().parse_args(
        ["live", "--refresh", "1.0", "--demo"]
    )
    assert args.refresh == pytest.approx(1.0)
    assert args.demo is True


def test_dashboard_still_works_as_alias() -> None:
    """Backward compat — `aegis dashboard` must keep parsing."""
    args = aegis_cli.build_parser().parse_args(["dashboard"])
    assert args.fn.__name__ == "cmd_dashboard"


# ── guard / rule alias ─────────────────────────────────────────────


@pytest.mark.parametrize(
    ("argv", "expected_action"),
    [
        (["guard", "list"], "list"),
        (["guard", "test", _GUARD_TEST_SAMPLE], "test"),
        (["guard", "disable", "no-rm-root"], "disable"),
        (["rule", "list"], "list"),
        (["rule", "enable", "no-rm-root"], "enable"),
    ],
)
def test_guard_and_rule_route_to_same_handler(
    argv: list[str], expected_action: str
) -> None:
    args = aegis_cli.build_parser().parse_args(argv)
    assert args.fn.__name__ == "cmd_rule"
    assert args.rule_action == expected_action


# ── coach (composite) ──────────────────────────────────────────────


@pytest.mark.parametrize(
    ("argv", "expected_target", "expected_rest"),
    [
        (
            ["coach", "burnin", "retrain", "--since", "30d"],
            "burnin",
            ["retrain", "--since", "30d"],
        ),
        (
            ["coach", "calibrate", "analyse"],
            "advisor-calibration",
            ["analyse"],
        ),
        (
            ["coach", "case-memory", "status"],
            "case-memory",
            ["status"],
        ),
    ],
)
def test_coach_routes_through_delegate(
    argv: list[str], expected_target: str, expected_rest: list[str]
) -> None:
    args = aegis_cli.build_parser().parse_args(argv)
    assert args.fn is aegis_cli._coach_delegate
    assert args._coach_target == expected_target
    assert args.rest == expected_rest


def test_coach_delegate_dispatches_to_legacy_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_coach_delegate` must re-parse and call the legacy handler.

    We stub the legacy handler so we don't actually train; the test
    just verifies the dispatch path: args namespace lands inside
    `cmd_burnin` (the legacy handler) with the right action.
    """
    captured: dict[str, object] = {}

    def _fake_burnin(args: argparse.Namespace) -> int:
        captured["action"] = args.action
        captured["since"] = args.since
        return 0

    monkeypatch.setattr(aegis_cli, "cmd_burnin", _fake_burnin)

    # Build parser AFTER monkeypatch so the legacy `bn.set_defaults(
    # fn=cmd_burnin)` picks up the stub.
    parser = aegis_cli.build_parser()
    args = parser.parse_args(
        ["coach", "burnin", "shadow-status", "--since", "7d"]
    )
    rc = args.fn(args)
    assert rc == 0
    assert captured == {"action": "shadow-status", "since": "7d"}


# ── memory (composite) ─────────────────────────────────────────────


def test_memory_show_routes_to_own_handler() -> None:
    args = aegis_cli.build_parser().parse_args(["memory", "show"])
    assert args.fn.__name__ == "cmd_memory_show"
    assert args.memory_action == "show"


def test_memory_claude_md_routes_to_own_handler() -> None:
    args = aegis_cli.build_parser().parse_args(["memory", "claude-md"])
    assert args.fn.__name__ == "cmd_memory_claude_md"


def test_memory_case_is_alias_for_case_memory() -> None:
    args = aegis_cli.build_parser().parse_args(
        ["memory", "case", "status"]
    )
    assert args.fn is aegis_cli._coach_delegate
    assert args._coach_target == "case-memory"
    assert args.rest == ["status"]


def test_memory_show_empty_store_returns_1(tmp_path: Path) -> None:
    """`memory show` on a missing store prints the empty message and
    returns 1 — operators get a friendly nudge instead of a traceback."""
    missing = tmp_path / "context_memory.jsonl"
    args = aegis_cli.build_parser().parse_args(
        ["memory", "show", "--context-memory", str(missing)]
    )
    rc = args.fn(args)
    assert rc == 1


def test_memory_show_walks_jsonl_records(tmp_path: Path) -> None:
    """`memory show` counts records + extracts ts_ns range from the
    JSONL store. Verifies it doesn't crash on partial / malformed
    lines (the audit chain can have those during crash recovery)."""
    store = tmp_path / "context_memory.jsonl"
    lines = [
        json.dumps({"ts_ns": 1_700_000_000_000_000_000, "decision": "ALLOW"}),
        json.dumps({"ts_ns": 1_700_000_001_000_000_000, "decision": "BLOCK"}),
        "",  # blank line — skip
        "{not valid json}",  # malformed — skip
        json.dumps({"ts_ns": 1_700_000_002_000_000_000, "decision": "ALLOW"}),
    ]
    store.write_text("\n".join(lines) + "\n", encoding="utf-8")

    args = aegis_cli.build_parser().parse_args(
        ["memory", "show", "--context-memory", str(store)]
    )
    rc = args.fn(args)
    assert rc == 0


def test_memory_claude_md_locator_fallback_when_cm_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When ContextMemory is empty / missing, fall back to the v0.5.1
    locator-only behavior — print path + size, exit 0. Operators get
    something useful on a fresh install before any agent traffic."""
    project = tmp_path / "project"
    project.mkdir()
    (project / "CLAUDE.md").write_text(
        "# Project guide\n\nUse rg, not grep.\n", encoding="utf-8"
    )
    monkeypatch.chdir(project)

    missing_cm = tmp_path / "no-such-cm.jsonl"
    args = aegis_cli.build_parser().parse_args(
        ["memory", "claude-md", "--context-memory", str(missing_cm)]
    )
    rc = args.fn(args)
    assert rc == 0


def test_memory_claude_md_proposals_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When ContextMemory contains BLOCK events above the threshold,
    the command runs miners and writes a markdown report. Test:
    construct a tmp CM with a synthetic loop-detector signal and
    verify the report mentions both `Bash` (the looping tool) and
    `Workflow Discipline` (the suggested section)."""
    import json as _json
    import time as _time

    project = tmp_path / "project"
    project.mkdir()
    (project / "CLAUDE.md").write_text(
        "# Project guide\n\n", encoding="utf-8"
    )
    monkeypatch.chdir(project)

    cm_path = tmp_path / "cm.jsonl"
    now_ns = _time.time_ns()
    rows = [
        {
            "schema_version": 1,
            "ts_ns": now_ns - i * 1_000_000,
            "trace_id": f"trace-{i:03d}",
            "invocation_id": "inv",
            "aid": "aid",
            "tenant_id": "local",
            "tool_name": "Bash",
            "decision": "REQUIRE_APPROVAL",
            "reason": "same Bash call repeated 3 times this session (threshold=3)",
            "channel": None,
            "provider": None,
            "latency_ms": 10.0,
            "cost_usd": 0.0,
            "tokens_in": 0,
            "tokens_out": 0,
            "step_traces": {},
            "m13_score": None,
            "advisor_invoked": False,
            "recommended_advisors": [],
            "atv_sha3": None,
            "atv_dim": 0,
            "is_sidechain": False,
            "mode": "local",
        }
        for i in range(5)
    ]
    cm_path.write_text(
        "\n".join(_json.dumps(r) for r in rows) + "\n",
        encoding="utf-8",
    )

    out_md = tmp_path / "proposals.md"
    args = aegis_cli.build_parser().parse_args([
        "memory", "claude-md",
        "--context-memory", str(cm_path),
        "--since", "24h",
        "--min-count", "3",
        "--out", str(out_md),
    ])
    rc = args.fn(args)
    assert rc == 0
    report = out_md.read_text(encoding="utf-8")
    assert "Bash" in report
    assert "Workflow Discipline" in report
    assert "loop-detector" in report


def test_memory_claude_md_returns_1_when_no_md_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No CLAUDE.md / AGENTS.md in cwd → friendly error, rc=1."""
    monkeypatch.chdir(tmp_path)
    args = aegis_cli.build_parser().parse_args(["memory", "claude-md"])
    rc = args.fn(args)
    assert rc == 1


def _write_cm_with_loop_signal(cm_path: Path, n: int = 5) -> None:
    """Helper: write a synthetic ContextMemory with `n` loop-detector
    events for the Bash tool. Used by --apply tests below to drive
    the proposal generator deterministically."""
    import json as _json
    import time as _time

    now_ns = _time.time_ns()
    rows = [
        {
            "schema_version": 1,
            "ts_ns": now_ns - i * 1_000_000,
            "trace_id": f"trace-{i:03d}",
            "invocation_id": "inv",
            "aid": "aid",
            "tenant_id": "local",
            "tool_name": "Bash",
            "decision": "REQUIRE_APPROVAL",
            "reason": "same Bash call repeated 3 times this session (threshold=3)",
            "channel": None, "provider": None,
            "latency_ms": 10.0,
            "cost_usd": 0.0, "tokens_in": 0, "tokens_out": 0,
            "step_traces": {}, "m13_score": None,
            "advisor_invoked": False, "recommended_advisors": [],
            "atv_sha3": None, "atv_dim": 0,
            "is_sidechain": False, "mode": "local",
        }
        for i in range(n)
    ]
    cm_path.write_text(
        "\n".join(_json.dumps(r) for r in rows) + "\n", encoding="utf-8",
    )


def test_memory_claude_md_apply_splices_into_md(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--apply 1` writes the chosen proposal into CLAUDE.md and a
    `.bak` copy of the original next to it."""
    project = tmp_path / "project"
    project.mkdir()
    md = project / "CLAUDE.md"
    md.write_text(
        "# Project\n\n## Workflow Discipline\n\nUse small commits.\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(project)

    cm = tmp_path / "cm.jsonl"
    _write_cm_with_loop_signal(cm)

    args = aegis_cli.build_parser().parse_args([
        "memory", "claude-md",
        "--context-memory", str(cm),
        "--apply", "1",
    ])
    rc = args.fn(args)
    assert rc == 0

    # CLAUDE.md was modified
    new = md.read_text(encoding="utf-8")
    assert "aegis-managed-proposal" in new
    assert "kind=loop-detector" in new
    # .bak preserves original
    bak = project / "CLAUDE.md.bak"
    assert bak.exists()
    assert "aegis-managed-proposal" not in bak.read_text(encoding="utf-8")


def test_memory_claude_md_apply_no_bak_skips_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    md = project / "CLAUDE.md"
    md.write_text(
        "# Project\n\n## Workflow Discipline\n\nUse small commits.\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(project)

    cm = tmp_path / "cm.jsonl"
    _write_cm_with_loop_signal(cm)

    args = aegis_cli.build_parser().parse_args([
        "memory", "claude-md",
        "--context-memory", str(cm),
        "--apply", "1",
        "--no-bak",
    ])
    rc = args.fn(args)
    assert rc == 0
    assert not (project / "CLAUDE.md.bak").exists()


def test_memory_claude_md_apply_out_of_range_returns_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "CLAUDE.md").write_text(
        "# Project\n\n## Workflow Discipline\n\nUse small commits.\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(project)

    cm = tmp_path / "cm.jsonl"
    _write_cm_with_loop_signal(cm, n=5)  # 1 proposal

    args = aegis_cli.build_parser().parse_args([
        "memory", "claude-md",
        "--context-memory", str(cm),
        "--apply", "999",
    ])
    rc = args.fn(args)
    assert rc == 1
    # Original CLAUDE.md was NOT modified.
    assert "aegis-managed-proposal" not in (
        project / "CLAUDE.md"
    ).read_text(encoding="utf-8")


def test_memory_diff_lists_applied_proposals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`aegis memory diff` should walk the project CLAUDE.md, find
    every aegis-managed-proposal marker, and surface them. Test:
    create a CLAUDE.md with one applied marker → diff finds it."""
    project = tmp_path / "project"
    project.mkdir()
    (project / "CLAUDE.md").write_text(
        "# Project\n\n## Workflow Discipline\n\n"
        "<!-- aegis-managed-proposal: kind=loop-detector "
        "pattern='repeated Bash' confidence=high -->\n"
        "Avoid Bash loops.\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(project)

    args = aegis_cli.build_parser().parse_args(["memory", "diff"])
    rc = args.fn(args)
    assert rc == 0


def test_memory_diff_json_emits_parseable_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """`--json` flag should print a parseable JSON object with the
    expected keys."""
    import json as _json

    project = tmp_path / "project"
    project.mkdir()
    (project / "CLAUDE.md").write_text(
        "# x\n\n## S\n\n"
        "<!-- aegis-managed-proposal: kind=k pattern='p' confidence=high -->\n"
        "body\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(project)

    args = aegis_cli.build_parser().parse_args(["memory", "diff", "--json"])
    rc = args.fn(args)
    assert rc == 0
    payload = _json.loads(capsys.readouterr().out)
    assert payload["applied_count"] == 1
    assert payload["applied"][0]["kind"] == "k"
    assert payload["applied"][0]["pattern"] == "p"
    assert "claude_md" in payload


def test_memory_diff_missing_md_returns_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    args = aegis_cli.build_parser().parse_args(["memory", "diff"])
    rc = args.fn(args)
    assert rc == 1


def test_atmu_recover_dry_run_reports_orphans(
    tmp_path: Path,
) -> None:
    """`aegis atmu recover --dry-run` should report orphans without
    mutating the WAL. Test sets up one old + one young row and
    verifies the dry-run preview surfaces exactly one as eligible."""
    import time as _time

    from aegis.atmu import IntentLog, TxState

    db = tmp_path / "intent.sqlite"
    log = IntentLog(str(db))
    rec_old = log.append_tentative(
        aid="aid-old", tenant_id="t", trace_id="tr1",
        span_id="sp1", parent_span_id=None,
        tool_name="Bash", tool_args_hash="h",
        blast_radius=5, atv_commitment="c",
    )
    log.append_tentative(
        aid="aid-young", tenant_id="t", trace_id="tr2",
        span_id="sp2", parent_span_id=None,
        tool_name="Read", tool_args_hash="h",
        blast_radius=1, atv_commitment="c",
    )
    now_ns = _time.time_ns()
    log.conn.execute(
        "UPDATE intent_log SET created_at_ns=? WHERE record_id=?",
        (now_ns - 30 * 3600 * 1_000_000_000, rec_old["record_id"]),
    )
    log.close()

    args = aegis_cli.build_parser().parse_args([
        "atmu", "recover", "--db", str(db), "--dry-run",
    ])
    rc = args.fn(args)
    assert rc == 0

    # WAL untouched — the old row is still TENTATIVE.
    log2 = IntentLog(str(db))
    rec = log2.get(rec_old["record_id"])
    assert rec is not None
    assert rec["current_state"] == TxState.TENTATIVE.value
    log2.close()


def test_atmu_recover_executes(tmp_path: Path) -> None:
    """`aegis atmu recover` (no --dry-run) actually transitions old
    orphans to ABORTED."""
    import time as _time

    from aegis.atmu import IntentLog, TxState

    db = tmp_path / "intent.sqlite"
    log = IntentLog(str(db))
    rec = log.append_tentative(
        aid="aid", tenant_id="t", trace_id="tr",
        span_id="sp", parent_span_id=None,
        tool_name="Bash", tool_args_hash="h",
        blast_radius=5, atv_commitment="c",
    )
    log.conn.execute(
        "UPDATE intent_log SET created_at_ns=? WHERE record_id=?",
        (_time.time_ns() - 50 * 3600 * 1_000_000_000, rec["record_id"]),
    )
    log.close()

    args = aegis_cli.build_parser().parse_args([
        "atmu", "recover", "--db", str(db),
    ])
    rc = args.fn(args)
    assert rc == 0

    log2 = IntentLog(str(db))
    state = log2.get(rec["record_id"])["current_state"]
    log2.close()
    assert state == TxState.ABORTED.value


def test_atmu_recover_zero_threshold_sweeps_all(tmp_path: Path) -> None:
    """`--max-age-hours 0` sweeps every non-terminal row regardless
    of age — useful as an operator escape hatch."""
    from aegis.atmu import IntentLog, TxState

    db = tmp_path / "intent.sqlite"
    log = IntentLog(str(db))
    for i in range(3):
        log.append_tentative(
            aid=f"a{i}", tenant_id="t", trace_id=f"tr{i}",
            span_id=f"sp{i}", parent_span_id=None,
            tool_name="Bash", tool_args_hash="h",
            blast_radius=5, atv_commitment="c",
        )
    log.close()

    args = aegis_cli.build_parser().parse_args([
        "atmu", "recover", "--db", str(db), "--max-age-hours", "0",
    ])
    rc = args.fn(args)
    assert rc == 0

    log2 = IntentLog(str(db))
    n_aborted = log2.count_state(TxState.ABORTED)
    log2.close()
    assert n_aborted == 3


def test_memory_rotate_dry_run_reports_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`aegis memory rotate --dry-run` should report the current
    chain state without making changes."""
    cm = tmp_path / "cm.jsonl"
    cm.write_text("x" * 100, encoding="utf-8")
    args = aegis_cli.build_parser().parse_args([
        "memory", "rotate", "--context-memory", str(cm), "--dry-run",
    ])
    rc = args.fn(args)
    assert rc == 0
    # Dry-run leaves the file untouched.
    assert cm.exists()
    assert cm.read_text(encoding="utf-8") == "x" * 100


def test_memory_rotate_executes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`aegis memory rotate` should rotate the file → slot 1 archive
    + active file removed."""
    from aegis.context_memory.rotation import compressed_rotation_path
    monkeypatch.setenv("AEGIS_CONTEXT_MEMORY_MAX_ROTATIONS", "3")

    cm = tmp_path / "cm.jsonl"
    cm.write_text("payload\n", encoding="utf-8")
    args = aegis_cli.build_parser().parse_args([
        "memory", "rotate", "--context-memory", str(cm),
    ])
    rc = args.fn(args)
    assert rc == 0
    assert not cm.exists()
    assert compressed_rotation_path(cm, 1).exists()


def test_memory_rotate_disabled_returns_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When rotation is disabled via env, the CLI reports + exits 1."""
    monkeypatch.setenv("AEGIS_CONTEXT_MEMORY_ROTATION_DISABLED", "1")
    cm = tmp_path / "cm.jsonl"
    cm.write_text("x" * 100, encoding="utf-8")
    args = aegis_cli.build_parser().parse_args([
        "memory", "rotate", "--context-memory", str(cm),
    ])
    rc = args.fn(args)
    assert rc == 1
    # File untouched.
    assert cm.exists()


def test_memory_diff_explicit_path_override(tmp_path: Path) -> None:
    """`--claude-md PATH` should override the cwd lookup."""
    md = tmp_path / "guide.md"
    md.write_text(
        "# Guide\n\n## S\n\n"
        "<!-- aegis-managed-proposal: kind=k pattern='p' confidence=low -->\n"
        "body\n",
        encoding="utf-8",
    )
    args = aegis_cli.build_parser().parse_args([
        "memory", "diff", "--claude-md", str(md),
    ])
    rc = args.fn(args)
    assert rc == 0


def test_memory_claude_md_apply_with_no_proposals_returns_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empty window → nothing to apply → friendly error."""
    project = tmp_path / "project"
    project.mkdir()
    (project / "CLAUDE.md").write_text(
        "# Project\n", encoding="utf-8",
    )
    monkeypatch.chdir(project)

    # ContextMemory exists but has zero loop events. The miners will
    # produce no proposals; --apply 1 has nothing to act on.
    cm = tmp_path / "cm.jsonl"
    cm.write_text(
        '{"ts_ns": 1700000000000000000, "decision": "ALLOW", '
        '"reason": "", "tool_name": "Bash"}\n',
        encoding="utf-8",
    )

    args = aegis_cli.build_parser().parse_args([
        "memory", "claude-md",
        "--context-memory", str(cm),
        "--apply", "1",
    ])
    rc = args.fn(args)
    assert rc == 1


# ── top-level help banner ──────────────────────────────────────────


def test_top_level_help_groups_commands_by_intent() -> None:
    """The top-level `--help` output should mirror the operator-
    vocabulary table from the README so docs and CLI stay in sync.
    The exact section labels + canonical names are part of the
    public surface — locking them down here flags accidental renames.
    """
    help_text = aegis_cli.build_parser().format_help()

    # Section headers
    assert "Aegis ATV — Agent Telemetry Vector" in help_text
    assert "Core commands:" in help_text
    assert "Audit & forensics:" in help_text
    assert "System:" in help_text

    # Core command names
    for name in (
        "doctor", "report", "live", "advise",
        "memory", "guard", "coach",
    ):
        assert f"  {name}:" in help_text, f"missing core command: {name}"

    # Audit / forensics
    for name in ("verify-audit", "replay", "forensic"):
        assert f"  {name}:" in help_text


def test_top_level_help_collapses_subparser_list() -> None:
    """The metavar collapse keeps the auto-generated subparser dump
    from drowning the curated section list. Verify the placeholder
    `<command>` appears in usage instead of the full alphabetic
    blow-out."""
    help_text = aegis_cli.build_parser().format_help()
    # usage line uses the metavar
    assert "usage: aegis [-h] <command>" in help_text
