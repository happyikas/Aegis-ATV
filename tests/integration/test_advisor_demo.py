"""Smoke test for ``demo/advisor_demo.py`` (Task 3, Phase C).

Verifies the full demo session produces a report and audit chain that
match the public contracts of the advisor-stack PRs (#65-#70). Skips
the regression by isolating the demo's audit + report into a tmp_path
so each run is hermetic.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "demo"))


def test_demo_session_produces_report_and_audit(
    tmp_path: Path, monkeypatch: object,
) -> None:
    audit = tmp_path / "audit.jsonl"
    report = tmp_path / "report.md"

    import advisor_demo  # noqa: E402

    advisor_demo.run_session(audit)
    assert audit.is_file()
    records = advisor_demo._stream_records(audit)
    # 22 PreToolUse + 22 PostToolUse expected from the deterministic
    # session.
    assert len(records) == 44
    pre = [r for r in records if r.get("hook") != "PostToolUse"]
    post = [r for r in records if r.get("hook") == "PostToolUse"]
    assert len(pre) == 22
    assert len(post) == 22

    stats = advisor_demo.analyse(records)
    advisor_demo.write_report(report, audit, stats)
    text = report.read_text(encoding="utf-8")

    # Required sections of the report.
    for needle in (
        "Aegis Advisor Demo Session",
        "Decision distribution",
        "Tool distribution",
        "Advisor-gate (PR-ψ-gating)",
        "ActionAdvice surface",
        "Multi-domain advisor recommendations",
        "Retrospective accuracy",
        "Findings",
    ):
        assert needle in text, f"missing: {needle}"


def test_demo_gate_skip_majority(tmp_path: Path) -> None:
    """The demo's gate-skip ratio should reflect "advisor only on
    critical moments" — most routine calls skip."""
    audit = tmp_path / "audit.jsonl"
    import advisor_demo
    advisor_demo.run_session(audit)
    stats = advisor_demo.analyse(advisor_demo._stream_records(audit))
    skip_ratio = stats["gate_skipped"] / max(stats["n_pre"], 1)
    assert skip_ratio >= 0.5, (
        f"gate skip ratio too low: {skip_ratio:.2f} "
        "(advisor would fire on most calls — re-check gating)"
    )


def test_demo_block_path_emits_security_reviewer(
    tmp_path: Path,
) -> None:
    """At least one BLOCK call must produce a `security-reviewer`
    recommendation — the canonical multi-domain output."""
    audit = tmp_path / "audit.jsonl"
    import advisor_demo
    advisor_demo.run_session(audit)
    records = advisor_demo._stream_records(audit)

    # Find a BLOCK record and verify advisor recommendation present.
    found_security = False
    for r in records:
        if r.get("decision") != "BLOCK":
            continue
        explain = r.get("explain") or {}
        advice = explain.get("action_advice") or {}
        recs = advice.get("recommended_advisors") or []
        for rec in recs:
            if isinstance(rec, dict) and rec.get("advisor") == "security-reviewer":
                found_security = True
                break
        if found_security:
            break
    assert found_security, (
        "expected at least one BLOCK record with "
        "security-reviewer recommendation"
    )


def test_demo_retrospective_link_works(tmp_path: Path) -> None:
    """At least the BLOCK / REQUIRE_APPROVAL records should produce a
    populated retrospective block (not all `not_applicable`)."""
    audit = tmp_path / "audit.jsonl"
    import advisor_demo
    advisor_demo.run_session(audit)
    records = advisor_demo._stream_records(audit)
    accuracies = []
    for r in records:
        if r.get("hook") != "PostToolUse":
            continue
        retro = (r.get("explain") or {}).get("retrospective_advice")
        if isinstance(retro, dict):
            accuracies.append(retro.get("accuracy"))
    assert "accurate" in accuracies, (
        f"expected at least one 'accurate' retrospective; got "
        f"{set(accuracies)}"
    )


def test_demo_audit_lines_are_valid_json(tmp_path: Path) -> None:
    audit = tmp_path / "audit.jsonl"
    import advisor_demo
    advisor_demo.run_session(audit)
    for line in audit.read_text(encoding="utf-8").splitlines():
        if line.strip():
            json.loads(line)  # raises if invalid
