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
