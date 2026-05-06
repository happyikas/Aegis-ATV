"""Orchestrator: drives TestCases against the Aegis stack and produces TestResults.

Responsibilities:

1.  ``setup_environment()`` — pin all providers to ``dummy``, redirect
    audit JSONL to a temp path, reset the loop detector. Idempotent.
2.  ``run_case()`` — dispatches a single TestCase to the unit or e2e
    driver and returns a populated ``TestResult``.
3.  ``run(category)`` — builds the case list, runs each, returns the
    list of results.
"""
from __future__ import annotations

import io
import json
import os
import sys
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Literal, cast

from .case import TestCase, TestResult, check

_REPO = Path(__file__).resolve().parents[2]
AUDIT_PATH = Path("/tmp/macmini-validation-audit.jsonl")


def setup_environment() -> None:
    """Pin providers to dummy and redirect audit. Safe to call repeatedly."""
    sys.path.insert(0, str(_REPO / "tools"))
    sys.path.insert(0, str(_REPO / "tools" / "hooks"))
    sys.path.insert(0, str(_REPO))

    if AUDIT_PATH.exists():
        AUDIT_PATH.unlink()

    os.environ["AEGIS_LOCAL_AUDIT"] = str(AUDIT_PATH)
    os.environ["AEGIS_ADVISOR_ENABLED"] = "1"
    os.environ.setdefault("AEGIS_ADVISOR_PROVIDER", "dummy")
    os.environ.setdefault("AEGIS_EMBEDDING_PROVIDER", "dummy")
    os.environ.setdefault("AEGIS_JUDGE_PROVIDER", "dummy")
    os.environ["AEGIS_APPROVE_AS_BLOCK"] = "1"
    os.environ["AEGIS_ATMU_DISABLE"] = "1"
    os.environ.setdefault("AEGIS_HW_PROVIDER", "sim")

    import aegis_local_hook  # type: ignore[import-not-found]
    import post_tool  # type: ignore[import-not-found]

    aegis_local_hook.LOCAL_AUDIT_PATH = AUDIT_PATH
    post_tool.LOCAL_AUDIT_PATH = AUDIT_PATH
    aegis_local_hook.ADVISOR_ENABLED = True
    aegis_local_hook.ADVISOR_ALWAYS = False
    aegis_local_hook.APPROVE_AS_BLOCK = True
    aegis_local_hook.ATMU_DISABLED = True
    post_tool.ATMU_DISABLED = True
    aegis_local_hook._CALIBRATION_SINGLETON = None

    try:
        from aegis.monitor.loop_detector import get_default_detector
        get_default_detector().reset()
    except Exception:  # noqa: BLE001
        pass


def _drive_pretool(event: dict[str, Any]) -> None:
    import aegis_local_hook

    pre_in = io.StringIO(json.dumps(event))
    pre_out = io.StringIO()
    saved = sys.stderr
    sys.stderr = io.StringIO()
    try:
        aegis_local_hook.handle_pretool(pre_in, pre_out)
    finally:
        sys.stderr = saved


def _read_audit_for_invocation(invocation_id: str) -> dict[str, Any]:
    if not AUDIT_PATH.is_file():
        return {"decision": "(none)", "advisors": []}
    for raw in reversed(AUDIT_PATH.read_text(encoding="utf-8").splitlines()):
        line = raw.strip()
        if not line:
            continue
        rec = json.loads(line)
        if (rec.get("invocation_id") == invocation_id
                and rec.get("hook") != "PostToolUse"):
            explain = rec.get("explain") or {}
            advice = explain.get("action_advice") or {}
            advisors = advice.get("recommended_advisors") or []
            return {
                "decision": rec.get("decision"),
                "reason": rec.get("reason", ""),
                "advisors": [
                    _summarize_advisor(r) for r in advisors
                    if isinstance(r, dict)
                ],
            }
    return {"decision": "(none)", "advisors": []}


def _summarize_advisor(rec: dict[str, Any]) -> dict[str, Any]:
    steps = rec.get("action_steps") or []
    return {
        "advisor": rec.get("advisor"),
        "priority": rec.get("priority"),
        "verbs": [
            s.get("verb") for s in steps if isinstance(s, dict)
        ],
        "steps": steps,
    }


def _run_e2e(case: TestCase) -> dict[str, Any]:
    pre = dict(case.pre_event or {})
    session = pre["session_id"]

    for k in range(case.loop_priming):
        _drive_pretool({
            "hook_event_name": "PreToolUse",
            "session_id": session,
            "invocation_id": f"prime-{case.cid}-{k}",
            "tool_name": pre["tool_name"],
            "tool_input": pre["tool_input"],
        })
    _drive_pretool(pre)

    return _read_audit_for_invocation(pre["invocation_id"])


def _run_unit(case: TestCase) -> dict[str, Any]:
    from aegis.burnin.anomaly import AnomalyTag
    from aegis.judge.action_advice import compose_advice_heuristic

    ctx = case.ctx_factory() if callable(case.ctx_factory) else None
    anomalies: list[Any] = []
    if case.anomaly_metric:
        anomalies.append(AnomalyTag(
            metric=case.anomaly_metric, severity="warning",
            observed=10, baseline_mean=1, baseline_std=1,
            z_score=3.0,
            description=f"{case.anomaly_metric} elevated",
        ))

    base_decision = cast(
        "Literal['ALLOW', 'BLOCK', 'REQUIRE_APPROVAL', 'DEFER']",
        case.base_decision,
    )
    advice = compose_advice_heuristic(
        temporal_ctx=ctx,
        anomalies=anomalies,
        base_decision=base_decision,
        current_tool=case.current_tool,
        current_model=case.current_model,
        cost_signals=dict(case.cost_signals or {}) or None,
        cache_signals=dict(case.cache_signals or {}) or None,
        security_signals=dict(case.security_signals or {}) or None,
        step_traces=dict(case.step_traces or {}) or None,
    )

    return {
        "decision": advice.decision,
        "reason": advice.reason,
        "advisors": [
            {
                "advisor": r.advisor,
                "priority": r.priority,
                "verbs": [s.verb for s in r.action_steps],
                "steps": [
                    {
                        "verb": s.verb,
                        "parameters": dict(s.parameters),
                        "expected_impact": s.expected_impact,
                        "confidence": s.confidence,
                    }
                    for s in r.action_steps
                ],
            }
            for r in advice.recommended_advisors
        ],
    }


def _run_rag(case: TestCase) -> dict[str, Any]:
    """Drive a RAG retrieval case. Honours case.rag_enabled to test
    the off-path. Always uses dummy embedding for determinism — the
    cases assert structural invariants, not semantic ranking."""
    from aegis.atv.embeddings import DummyEmbedding
    from aegis.config import settings
    from aegis.judge.rag_corpus import reset_corpus_cache
    from aegis.judge.rag_retrieval import (
        build_default_index,
        reset_index_cache,
        retrieve,
    )

    object.__setattr__(settings, "aegis_rag_enabled", case.rag_enabled)
    reset_corpus_cache()
    reset_index_cache()

    if not case.rag_enabled:
        return {
            "n_retrieved": 0,
            "chunk_ids": [],
            "categories": [],
            "rendered": "",
            "decision": None,
            "advisors": [],
        }

    index = build_default_index()
    hits = retrieve(
        case.rag_query, k=case.rag_top_k, index=index,
        provider=DummyEmbedding(),
    )
    chunks = [c for c, _ in hits]
    return {
        "n_retrieved": len(chunks),
        "chunk_ids": [c.id for c in chunks],
        "categories": [c.category for c in chunks],
        "rendered": "\n\n".join(c.render_for_prompt() for c in chunks),
        "decision": None,
        "advisors": [],
    }


def run_case(case: TestCase) -> TestResult:
    t0 = time.perf_counter()
    if case.test_type == "rag":
        observed = _run_rag(case)
    elif case.test_type == "e2e":
        observed = _run_e2e(case)
    else:
        observed = _run_unit(case)
    duration_ms = (time.perf_counter() - t0) * 1000.0
    passed, miss = check(case, observed)
    return TestResult(
        cid=case.cid,
        category=case.category,
        title=case.title,
        passed=passed,
        decision=observed.get("decision"),
        advisors=observed.get("advisors", []),
        misses=miss,
        duration_ms=duration_ms,
        raw=observed,
    )


def _build_cases(category: str) -> list[TestCase]:
    if category == "cost":
        from .cost import cases
        return cases()
    if category == "performance":
        from .performance import cases
        return cases()
    if category == "security":
        from .security import cases
        return cases()
    if category == "rag":
        from .rag import cases
        return cases()
    if category == "all":
        from .cost import cases as cc
        from .performance import cases as pc
        from .rag import cases as rc
        from .security import cases as sc
        return [*cc(), *pc(), *sc(), *rc()]
    raise ValueError(
        f"unknown category {category!r}; expected "
        "cost / performance / security / rag / all"
    )


def run(category: str = "all") -> list[TestResult]:
    setup_environment()
    cases = _build_cases(category)
    return [run_case(c) for c in cases]


def filter_by_category(
    results: Iterable[TestResult], category: str
) -> list[TestResult]:
    return [r for r in results if r.category == category]
