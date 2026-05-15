"""Comprehensive test matrix for the v2.8 action_steps surface
(PR-ζ).

Three groups of tests:

1. Per-verb defensive parser sweep — for each of the 11 verbs in the
   closed catalog, valid input round-trips; common malformed shapes
   (missing required keys, wrong types, out-of-range values) drop.

2. Per-advisor cross-domain combinations — multi-domain scenarios
   (cost+cache+security firing together) emit the expected verb mix.

3. Hook end-to-end — drive the in-process hooks with realistic
   scenarios and confirm action_steps survive into the audit JSONL
   record.
"""

from __future__ import annotations

import io
import json
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from aegis.atv.temporal import ATVSnapshot, TemporalContext
from aegis.burnin.anomaly import AnomalyTag
from aegis.judge.action_advice import (
    advice_from_dict,
    advice_to_dict,
    compose_advice_heuristic,
)

# ──────────────────────────────────────────────────────────────────────
# Group 1 — per-verb parser sweep (parametrised)
# ──────────────────────────────────────────────────────────────────────


VERB_VALID_PARAMS: dict[str, dict[str, Any]] = {
    "prune-turns": {
        "turn_indices_rel": [-3, -2, -1],
        "saved_tokens_estimate": 4500,
        "saved_dollars_estimate": 0.42,
    },
    "summarize-window": {
        "turn_range": [-5, -2],
        "retain_categories": ["tool_calls", "decisions"],
    },
    "swap-model": {
        "from_model": "claude-opus-4-7",
        "to_model": "claude-haiku-4-5",
        "ratio_savings": 3.0,
    },
    "end-session": {"reason": "budget exceeded"},
    "swap-tool": {
        "from_tool": "Read",
        "to_tool": "Grep",
        "similarity_score": 0.76,
    },
    "narrow-scope": {
        "original_args": "src/**",
        "suggested_args": "src/aegis/judge/",
    },
    "clarify-intent": {
        "clarifying_question": "Did you intend the 1.x or 2.x branch?",
    },
    "run-diagnostic": {
        "diagnostic_command": "pytest -x tests/unit/",
        "expected_signal": "all green",
    },
    "verify-state": {
        "check": "file exists",
        "expected": "True",
    },
    "notify-operator": {
        "channel": "#aegis-cost",
        "urgency": "high",
        "summary": "M12 cost-divergence escalation",
    },
    "require-approval": {
        "approver_role": "security-reviewer",
        "reason": "destructive operation",
        "artifacts": ["rule:git_destructive", "blast=high"],
    },
}


VERB_INVALID_VARIANTS: dict[str, list[dict[str, Any]]] = {
    "prune-turns": [
        # Missing required key
        {"saved_tokens_estimate": 100},
        # Positive turn index
        {"turn_indices_rel": [1, 2]},
        # Empty list
        {"turn_indices_rel": []},
        # Negative savings
        {"turn_indices_rel": [-1], "saved_tokens_estimate": -100},
    ],
    "summarize-window": [
        # Missing turn_range
        {},
        # Inverted range
        {"turn_range": [-1, -5]},
        # Wrong arity
        {"turn_range": [-1]},
        # Positive end
        {"turn_range": [-3, 1]},
    ],
    "swap-model": [
        {"from_model": "opus"},  # missing to_model
        {"from_model": "", "to_model": "haiku"},  # empty
        {"from_model": "opus", "to_model": "haiku",
         "ratio_savings": -1.0},
    ],
    "swap-tool": [
        {"from_tool": "Read"},  # missing to_tool
        {"from_tool": "", "to_tool": "Grep"},
    ],
    "narrow-scope": [
        {"original_args": "x"},  # missing suggested_args
        {"original_args": "", "suggested_args": "y"},
    ],
    "clarify-intent": [
        {},  # missing
        {"clarifying_question": "   "},  # whitespace-only
    ],
    "run-diagnostic": [
        {},
        {"diagnostic_command": ""},
    ],
    "verify-state": [
        {},
        {"check": ""},
    ],
    "notify-operator": [
        {"channel": "#x"},  # missing summary
        {"channel": "", "summary": "y"},
    ],
    "require-approval": [
        {},
        {"reason": ""},
    ],
    "end-session": [],  # no required keys, so no required-failure cases
}


def _make_advice_dict_with_step(verb: str, params: dict) -> dict:
    return {
        "decision": "ALLOW", "reason": "x", "confidence": 0.5,
        "recommended_advisors": [{
            "advisor": "cost-optimizer", "priority": "high",
            "action": "test",
            "action_steps": [{"verb": verb, "parameters": params}],
        }],
    }


@pytest.mark.parametrize("verb", list(VERB_VALID_PARAMS.keys()))
def test_each_verb_valid_params_round_trip(verb: str) -> None:
    """Every verb in the catalog accepts a valid params dict and
    round-trips through advice_to_dict / advice_from_dict."""
    d = _make_advice_dict_with_step(verb, VERB_VALID_PARAMS[verb])
    advice = advice_from_dict(d)
    assert len(advice.recommended_advisors) == 1
    steps = advice.recommended_advisors[0].action_steps
    assert len(steps) == 1, f"{verb} dropped despite valid params"
    assert steps[0].verb == verb

    # JSON serialisable.
    json.dumps(advice_to_dict(advice))


def _verb_invalid_cases() -> Iterator[tuple[str, dict]]:
    for verb, variants in VERB_INVALID_VARIANTS.items():
        for params in variants:
            yield verb, params


@pytest.mark.parametrize("verb,params", list(_verb_invalid_cases()))
def test_each_verb_invalid_params_dropped(verb: str, params: dict) -> None:
    """Every malformed variant for every verb must be silently dropped
    by the defensive parser. The recommendation survives but with
    an empty action_steps tuple."""
    d = _make_advice_dict_with_step(verb, params)
    advice = advice_from_dict(d)
    assert len(advice.recommended_advisors) == 1
    assert advice.recommended_advisors[0].action_steps == (), (
        f"{verb} with {params} should have been dropped"
    )


# ──────────────────────────────────────────────────────────────────────
# Group 2 — multi-domain combinations
# ──────────────────────────────────────────────────────────────────────


def _ctx_5turns_expensive() -> TemporalContext:
    """Build a 5-turn ctx where last 4 turns are expensive (5000
    tokens each) and cache_hit drops at index -3."""
    snaps = []
    for i in range(5):
        rel = i - 4
        tokens = 200 if rel < -3 else 5000
        cache = 0.85 if rel < -3 else 0.30
        snaps.append(ATVSnapshot(
            turn_index_rel=rel, ts_ns=0, tool_name="Bash",
            args_excerpt="", decision="ALLOW", outcome="success",
            input_tokens=tokens // 2, output_tokens=tokens // 2,
            cache_hit_rate=cache,
        ))
    return TemporalContext(
        history=tuple(snaps), window_size=5,
        cumulative_token_trajectory=tuple(0 for _ in range(5)),
        cache_hit_rate_trajectory=tuple(s.cache_hit_rate for s in snaps),
        n_backtracks=0, n_redundant=0, n_errors=0, n_failures=0,
        cache_hit_rate_max_drop_pp=55.0,
        token_velocity_per_turn=2500.0,
        is_progress_stalled=False,
        distinct_tools_in_window=("Bash",),
    )


class TestCrossDomainCombinations:
    def test_cost_plus_cache_plus_security(self) -> None:
        """Canonical 3-domain combination from the user's example —
        cost +30%, cache hit collapsed, destructive backup deletion.
        Should produce 3 advisor recommendations, each with its own
        action_steps[]."""
        ctx = _ctx_5turns_expensive()
        advice = compose_advice_heuristic(
            base_decision="REQUIRE_APPROVAL",
            current_tool="Bash",
            current_model="claude-opus-4-7",
            temporal_ctx=ctx,
            cost_signals={
                "hw_vs_sw_divergence_ratio": 3.0,
                "budget_used_ratio": 1.5,
            },
            cache_signals={
                "cache_hit_rate_max_drop_pp": 55.0,
                "prefix_re_keys_in_window": 4,
            },
            security_signals={
                "verdict_decision": "REQUIRE_APPROVAL",
                "destructive_path_match": True,
                "policy_rule": "rule:backup_path_destructive",
                "blast_radius": "high",
            },
        )
        names = {r.advisor for r in advice.recommended_advisors}
        # All three domain advisors fire.
        assert {
            "cost-optimizer", "kv-cache-optimizer", "security-reviewer",
        } <= names

        # Each carries action_steps.
        all_verbs = []
        for r in advice.recommended_advisors:
            assert r.action_steps, (
                f"{r.advisor} has empty action_steps in 3-domain combo"
            )
            all_verbs.extend(s.verb for s in r.action_steps)

        # Expected verb mix (heuristic Layer A):
        #   security-reviewer  -> require-approval
        #   cost-optimizer     -> prune-turns / swap-model / end-session
        #                          / notify-operator (M12)
        #   kv-cache-optimizer -> prune-turns
        assert "require-approval" in all_verbs
        assert "prune-turns" in all_verbs
        assert "swap-model" in all_verbs

    def test_loop_plus_errors(self) -> None:
        """Performance-domain combination — repeated calls + errors
        should emit BOTH loop-breaker (swap-tool) AND test-runner
        (run-diagnostic)."""
        anomalies = [
            AnomalyTag(
                metric="session_error_rate", severity="warning",
                observed=10, baseline_mean=1, baseline_std=1,
                z_score=3.0, description="errors elevated",
            ),
        ]
        # Use step336 trace path for loop-breaker (since the heuristic's
        # "redundant" anomaly check matches metric names containing the
        # literal substring "redundant" — none of the canonical
        # burn-in metrics use that exact word).
        advice = compose_advice_heuristic(
            base_decision="REQUIRE_APPROVAL",
            current_tool="Read",
            anomalies=anomalies,
            step_traces={
                "aegis.firewall.step336_loop.run":
                    "step336: loop (3× seen) Read",
            },
        )
        names = {r.advisor for r in advice.recommended_advisors}
        assert "loop-breaker" in names
        assert "test-runner" in names

        # Verbs from each.
        all_verbs = [
            s.verb for r in advice.recommended_advisors
            for s in r.action_steps
        ]
        assert "swap-tool" in all_verbs
        assert "run-diagnostic" in all_verbs

    def test_backtrack_plus_velocity(self) -> None:
        """Confused-agent combination — backtrack + token velocity
        spike should emit human-clarifier + context-compactor."""
        # 5-turn ctx so context-compactor's "summarize early window
        # but keep last 2 verbatim" rule has a non-empty range.
        snaps = []
        for i in range(5):
            rel = i - 4  # -4 to 0
            snaps.append(ATVSnapshot(
                turn_index_rel=rel, ts_ns=0, tool_name="Edit",
                args_excerpt="", decision="ALLOW", outcome="success",
                backtrack=(rel == -2),  # one backtrack at -2
                input_tokens=200, output_tokens=200, cache_hit_rate=0.5,
            ))
        ctx = TemporalContext(
            history=tuple(snaps), window_size=5,
            cumulative_token_trajectory=tuple(
                400 * (i + 1) for i in range(5)
            ),
            cache_hit_rate_trajectory=tuple(0.5 for _ in range(5)),
            n_backtracks=2, n_redundant=0, n_errors=0, n_failures=0,
            cache_hit_rate_max_drop_pp=0.0,
            token_velocity_per_turn=400.0,
            is_progress_stalled=False,
            distinct_tools_in_window=("Edit",),
        )
        advice = compose_advice_heuristic(
            temporal_ctx=ctx,
            anomalies=[
                AnomalyTag(
                    metric="window_token_velocity_per_turn",
                    severity="warning",
                    observed=10, baseline_mean=1, baseline_std=1,
                    z_score=3.0, description="velocity high",
                ),
            ],
        )
        names = {r.advisor for r in advice.recommended_advisors}
        assert "human-clarifier" in names
        assert "context-compactor" in names

        all_verbs = [
            s.verb for r in advice.recommended_advisors
            for s in r.action_steps
        ]
        assert "clarify-intent" in all_verbs
        assert "summarize-window" in all_verbs


# ──────────────────────────────────────────────────────────────────────
# Group 3 — hook end-to-end (action_steps survive into audit JSONL)
# ──────────────────────────────────────────────────────────────────────


@pytest.fixture
def _hook_audit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    audit = tmp_path / "audit.jsonl"
    monkeypatch.setenv("AEGIS_LOCAL_AUDIT", str(audit))
    monkeypatch.setenv("AEGIS_ADVISOR_ENABLED", "1")
    monkeypatch.setenv("AEGIS_ADVISOR_PROVIDER", "dummy")
    monkeypatch.setenv("AEGIS_EMBEDDING_PROVIDER", "dummy")
    monkeypatch.setenv("AEGIS_JUDGE_PROVIDER", "dummy")
    monkeypatch.setenv("AEGIS_ATMU_DISABLE", "1")

    # Hook is in tools/, not on the regular pythonpath
    repo = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(repo / "tools"))
    sys.path.insert(0, str(repo / "tools" / "hooks"))
    import aegis_local_hook
    aegis_local_hook.LOCAL_AUDIT_PATH = audit
    aegis_local_hook.ADVISOR_ENABLED = True
    aegis_local_hook.ATMU_DISABLED = True
    aegis_local_hook._CALIBRATION_SINGLETON = None

    # Activate a synthetic Pro license so the advisor gate
    # (LICENSE_KEY.md §9 step 6) opens. The boot-once sentinel is
    # flipped so the gate's lazy disk-init doesn't wipe this in-memory
    # claim. Same pattern as ``demo/advisor_demo.py``.
    import time as _t

    from aegis.license import set_active_license
    from aegis.license.verify import LicenseClaims

    set_active_license(LicenseClaims(
        tier="pro",
        iss="https://license.test.example",
        sub="action-steps-matrix-test",
        aud="aegis-atv",
        iat=int(_t.time()) - 60,
        exp=int(_t.time()) + 60 * 60 * 24 * 365,
        license_id="lic_TEST_ACTION_STEPS",
        seats=1,
        features=(),
        burnin_bind=None,
        kid="aegis-license-TEST",
    ))
    monkeypatch.setattr(aegis_local_hook, "_license_booted", True)
    return audit


def _run_hook(event: dict) -> None:
    import aegis_local_hook
    pre_in = io.StringIO(json.dumps(event))
    pre_out = io.StringIO()
    saved = sys.stderr
    sys.stderr = io.StringIO()
    try:
        aegis_local_hook.handle_pretool(pre_in, pre_out)
    finally:
        sys.stderr = saved


def test_destructive_call_audit_carries_action_steps(
    _hook_audit: Path,
) -> None:
    """End-to-end: destructive bash command → BLOCK → audit JSONL
    line carries ``explain.action_advice.recommended_advisors[].action_steps``
    with at least one valid step (require-approval)."""
    _run_hook({
        "hook_event_name": "PreToolUse",
        "session_id": "matrix-e2e-1",
        "invocation_id": "matrix-e2e-1",
        "tool_name": "Bash",
        "tool_input": {"command": "git push --force origin main"},
    })
    line = _hook_audit.read_text(encoding="utf-8").strip().splitlines()[-1]
    rec = json.loads(line)
    advice = (rec.get("explain") or {}).get("action_advice") or {}
    advisors = advice.get("recommended_advisors") or []
    assert advisors, "no recommended_advisors in audit"
    sec = next(
        (a for a in advisors if a.get("advisor") == "security-reviewer"),
        None,
    )
    assert sec is not None, "security-reviewer not in audit"
    steps = sec.get("action_steps") or []
    assert steps, "action_steps missing in audit"
    verbs = [s.get("verb") for s in steps]
    assert "require-approval" in verbs


# ──────────────────────────────────────────────────────────────────────
# Group 4 — schema invariants across the catalog
# ──────────────────────────────────────────────────────────────────────


class TestSchemaInvariants:
    def test_step_count_equals_catalog(self) -> None:
        """Sanity: VERB_VALID_PARAMS covers all 11 catalog entries.
        If we add a verb to the catalog without adding a valid-params
        sample here, this test breaks immediately."""
        from aegis.judge.action_advice import _ALLOWED_VERBS
        assert set(VERB_VALID_PARAMS.keys()) == _ALLOWED_VERBS

    def test_invalid_variants_cover_catalog(self) -> None:
        """Sanity: every catalog verb has invalid variant cases
        (except end-session which has no required params)."""
        from aegis.judge.action_advice import _ALLOWED_VERBS
        # end-session legitimately has no required keys -> no required-
        # failure variants; others must have ≥ 1.
        no_required = {"end-session"}
        for verb in _ALLOWED_VERBS - no_required:
            assert VERB_INVALID_VARIANTS.get(verb), (
                f"{verb} missing invalid variants"
            )
