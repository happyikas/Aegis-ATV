"""TestCase / TestResult dataclasses + the expectation-checker.

A ``TestCase`` is a pure data record describing one scenario. The
runner turns it into either a unit call (``compose_advice_heuristic``)
or an end-to-end hook drive, then compares the observed advisor /
verb / decision against the case's declared expectations.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any


@dataclass
class TestCase:
    cid: str
    category: str
    title: str
    scenario: str
    test_type: str

    expected_advisor: str | None = None
    expected_verb: str | None = None
    expected_no_fire: bool = False
    expected_no_fire_for: str | None = None
    expected_multi: tuple[str, ...] = ()
    expected_verbs_any: tuple[str, ...] = ()
    expected_decision: str | None = None

    pre_event: Mapping[str, Any] | None = None
    loop_priming: int = 0

    ctx_factory: Callable[[], Any] | None = None
    current_tool: str = ""
    current_model: str | None = None
    base_decision: str = "ALLOW"
    cost_signals: Mapping[str, Any] | None = None
    cache_signals: Mapping[str, Any] | None = None
    security_signals: Mapping[str, Any] | None = None
    step_traces: Mapping[str, str] | None = None
    anomaly_metric: str | None = None

    # RAG retrieval cases (test_type="rag")
    rag_query: str = ""
    rag_top_k: int = 3
    rag_enabled: bool = True
    rag_expected_min_count: int = 0
    rag_expected_max_count: int = 0
    rag_expected_categories: tuple[str, ...] = ()
    rag_expected_chunk_ids: tuple[str, ...] = ()

    execution_summary: str = ""

    def __post_init__(self) -> None:
        if self.test_type not in ("unit", "e2e", "rag"):
            raise ValueError(
                f"{self.cid}: test_type must be 'unit' / 'e2e' / 'rag', "
                f"got {self.test_type!r}"
            )
        if self.test_type == "e2e" and self.pre_event is None:
            raise ValueError(f"{self.cid}: e2e cases must set pre_event")
        if self.test_type == "rag" and not self.rag_query and self.rag_enabled:
            raise ValueError(
                f"{self.cid}: rag cases must set rag_query (or "
                "rag_enabled=False to test the off path)"
            )


@dataclass
class TestResult:
    cid: str
    category: str
    title: str
    passed: bool
    decision: str | None
    advisors: list[dict[str, Any]]
    misses: list[str] = field(default_factory=list)
    duration_ms: float = 0.0
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def status(self) -> str:
        return "PASS" if self.passed else "FAIL"


def check(case: TestCase, observed: Mapping[str, Any]) -> tuple[bool, list[str]]:
    """Compare observed advisor/verb / retrieval output against case expectations."""
    if case.test_type == "rag":
        return _check_rag(case, observed)

    advisors = {a["advisor"] for a in observed.get("advisors", [])}
    verbs_per_advisor = {
        a["advisor"]: a["verbs"] for a in observed.get("advisors", [])
    }
    miss: list[str] = []

    if case.expected_no_fire and advisors:
        miss.append(f"unexpected fire: {sorted(advisors)}")

    if case.expected_no_fire_for and case.expected_no_fire_for in advisors:
        miss.append(f"{case.expected_no_fire_for} unexpectedly fired")

    if case.expected_advisor:
        if case.expected_advisor not in advisors:
            miss.append(
                f"{case.expected_advisor} not in {sorted(advisors)}"
            )
        elif case.expected_verb:
            verbs = verbs_per_advisor.get(case.expected_advisor, [])
            if case.expected_verb not in verbs:
                miss.append(
                    f"{case.expected_advisor} missing verb "
                    f"{case.expected_verb} (has {verbs})"
                )

    for a in case.expected_multi:
        if a not in advisors:
            miss.append(f"multi miss: {a} not in {sorted(advisors)}")

    if case.expected_verbs_any:
        all_verbs = [
            v for a in observed.get("advisors", []) for v in a["verbs"]
        ]
        for v in case.expected_verbs_any:
            if v not in all_verbs:
                miss.append(f"verb {v} missing from {all_verbs}")

    if case.expected_decision:
        actual = observed.get("decision")
        if actual != case.expected_decision:
            miss.append(
                f"decision {actual!r} != {case.expected_decision!r}"
            )

    return (len(miss) == 0, miss)


def _check_rag(case: TestCase, observed: Mapping[str, Any]) -> tuple[bool, list[str]]:
    """RAG retrieval expectations.

    Observation shape (set by ``runner._run_rag``):
      {
        "n_retrieved": int,
        "chunk_ids": list[str],
        "categories": list[str],
        "rendered": str,
      }
    """
    miss: list[str] = []
    n = int(observed.get("n_retrieved", 0))

    if case.rag_expected_min_count and n < case.rag_expected_min_count:
        miss.append(
            f"retrieved {n} < expected min {case.rag_expected_min_count}"
        )
    if case.rag_expected_max_count and n > case.rag_expected_max_count:
        miss.append(
            f"retrieved {n} > expected max {case.rag_expected_max_count}"
        )

    chunk_ids = list(observed.get("chunk_ids", []))
    categories = list(observed.get("categories", []))

    if case.rag_expected_categories:
        cats_set = set(categories)
        for cat in case.rag_expected_categories:
            if cat not in cats_set:
                miss.append(
                    f"missing category {cat!r} in retrieved "
                    f"{sorted(cats_set)}"
                )
    if case.rag_expected_chunk_ids:
        ids_set = set(chunk_ids)
        for cid in case.rag_expected_chunk_ids:
            if cid not in ids_set:
                miss.append(
                    f"expected chunk {cid!r} not retrieved "
                    f"(got {chunk_ids})"
                )

    return (len(miss) == 0, miss)
