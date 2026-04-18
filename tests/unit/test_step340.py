"""Tests for Step 340 — policy match + sLLM judge fallback."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from aegis.firewall.core import FirewallContext
from aegis.firewall.step340_policy import (
    atv_summary_for_llm,
    match_rule,
    reset_policy_cache,
    run,
)
from aegis.judge.base import Judge, JudgeVerdict
from tests.unit._firewall_helpers import ZERO_ATV, make_input


@pytest.fixture
def policy_dir(tmp_path: Path) -> Path:
    rules: dict[str, list[dict[str, Any]]] = {
        "deny": [
            {"name": "no-shadow", "arg_pattern": "/etc/shadow"},
            {
                "name": "no-drop-table",
                "tool_name": "db_query",
                "arg_pattern": r"DROP\s+TABLE",
            },
        ],
        "allow": [
            {"name": "safe-read", "tool_name": "read_file", "arg_pattern": r"^\{\"path\":\"\./data/"},
        ],
    }
    (tmp_path / "default.json").write_text(json.dumps(rules))
    reset_policy_cache()
    with patch("aegis.firewall.step340_policy.settings") as s:
        s.aegis_policy_dir = str(tmp_path)
        yield tmp_path
    reset_policy_cache()


class _StubJudge(Judge):
    def __init__(self, verdict: JudgeVerdict) -> None:
        self.verdict = verdict
        self.calls = 0

    def evaluate(self, summary: str) -> JudgeVerdict:
        self.calls += 1
        return self.verdict


class TestMatchRule:
    def test_tool_name_filter(self) -> None:
        inp = make_input(tool_name="read_file")
        assert match_rule({"tool_name": "read_file"}, inp)
        assert not match_rule({"tool_name": "db_query"}, inp)

    def test_tenant_filter(self) -> None:
        inp = make_input(tenant_id="acme")
        assert match_rule({"tenant_id": "acme"}, inp)
        assert not match_rule({"tenant_id": "other"}, inp)

    def test_arg_pattern(self) -> None:
        inp = make_input(tool_args_json='{"path":"/etc/shadow"}')
        assert match_rule({"arg_pattern": "/etc/shadow"}, inp)
        assert not match_rule({"arg_pattern": "no-match"}, inp)

    def test_combined(self) -> None:
        inp = make_input(tool_name="db_query", tool_args_json="DROP TABLE x")
        rule = {"tool_name": "db_query", "arg_pattern": r"DROP\s+TABLE"}
        assert match_rule(rule, inp)


class TestStep340:
    def test_deny_rule_blocks(self, policy_dir: Path) -> None:
        inp = make_input(tool_args_json='{"path":"/etc/shadow"}')
        r = run(ZERO_ATV, inp, FirewallContext())
        assert r.verdict == "BLOCK"
        assert "no-shadow" in r.reason

    def test_allow_rule_short_circuits_judge(self, policy_dir: Path) -> None:
        judge = _StubJudge(JudgeVerdict("BLOCK", 1.0, "should never run"))
        with patch("aegis.firewall.step340_policy.get_judge", return_value=judge):
            inp = make_input(
                tool_name="read_file",
                tool_args_json='{"path":"./data/x.txt"}',
            )
            r = run(ZERO_ATV, inp, FirewallContext())
        assert r.verdict is None
        assert judge.calls == 0

    def test_unmatched_falls_through_to_judge_block(self, policy_dir: Path) -> None:
        judge = _StubJudge(JudgeVerdict("BLOCK", 0.9, "judge says no"))
        with patch("aegis.firewall.step340_policy.get_judge", return_value=judge):
            inp = make_input(
                tool_name="call_external_api",
                tool_args_json='{"url":"https://example.com"}',
            )
            r = run(ZERO_ATV, inp, FirewallContext())
        assert r.verdict == "BLOCK"
        assert "judge says no" in r.reason
        assert judge.calls == 1

    def test_unmatched_falls_through_to_judge_approval(self, policy_dir: Path) -> None:
        judge = _StubJudge(JudgeVerdict("REQUIRE_APPROVAL", 0.5, "needs human"))
        with patch("aegis.firewall.step340_policy.get_judge", return_value=judge):
            r = run(
                ZERO_ATV,
                make_input(tool_name="call_external_api", tool_args_json="{}"),
                FirewallContext(),
            )
        assert r.verdict == "REQUIRE_APPROVAL"

    def test_unmatched_falls_through_to_judge_allow(self, policy_dir: Path) -> None:
        judge = _StubJudge(JudgeVerdict("ALLOW", 0.95, "looks fine"))
        with patch("aegis.firewall.step340_policy.get_judge", return_value=judge):
            r = run(
                ZERO_ATV,
                make_input(tool_name="call_external_api", tool_args_json="{}"),
                FirewallContext(),
            )
        assert r.verdict is None


def test_atv_summary_includes_key_fields() -> None:
    inp = make_input(tool_name="db_query", tool_args_json='{"sql":"SELECT 1"}')
    s = atv_summary_for_llm(inp)
    assert "Tool: db_query" in s
    assert "SELECT 1" in s
    assert "Tenant: demo-tenant" in s
