"""Unit tests for aegis.monitor.loop_detector + step336 (v2.1.3, Day-1 #6)."""

from __future__ import annotations

import json
import time
from typing import Any

import numpy as np
import pytest

from aegis.firewall import step336_loop
from aegis.firewall.core import FirewallContext
from aegis.monitor.loop_detector import (
    LoopDetector,
    _canonical_hash,
    get_default_detector,
    reset_default_detector,
)
from aegis.schema import ATVHeader, ATVInput


@pytest.fixture(autouse=True)
def _fresh_default() -> None:
    reset_default_detector()


# ---- _canonical_hash ----------------------------------------------------


def test_canonical_hash_dict_order_stable() -> None:
    a = _canonical_hash("Bash", {"a": 1, "b": 2})
    b = _canonical_hash("Bash", {"b": 2, "a": 1})
    assert a == b


def test_canonical_hash_diff_tool_diff_hash() -> None:
    a = _canonical_hash("Bash", {"command": "ls"})
    b = _canonical_hash("Read", {"command": "ls"})
    assert a != b


def test_canonical_hash_accepts_serialized_string() -> None:
    a = _canonical_hash("Bash", '{"command": "ls"}')
    b = _canonical_hash("Bash", {"command": "ls"})
    assert a == b


def test_canonical_hash_handles_malformed_string() -> None:
    """Falls back to using the raw string as-is."""
    h1 = _canonical_hash("Bash", "not json {{")
    h2 = _canonical_hash("Bash", "not json {{")
    assert h1 == h2  # deterministic at least


# ---- LoopDetector.observe -----------------------------------------------


def test_first_two_calls_are_fresh() -> None:
    det = LoopDetector(loop_threshold=3)
    v1 = det.observe("sess", "Bash", {"command": "ls"})
    v2 = det.observe("sess", "Bash", {"command": "ls"})
    assert v1.kind is None
    assert v2.kind is None  # second read-only Bash isn't redundant (Bash not in READ_ONLY_TOOLS)
    assert v1.count == 1
    assert v2.count == 2


def test_third_repeat_triggers_loop() -> None:
    det = LoopDetector(loop_threshold=3)
    for _ in range(2):
        det.observe("sess", "Bash", {"command": "ls"})
    v3 = det.observe("sess", "Bash", {"command": "ls"})
    assert v3.kind == "loop"
    assert v3.count == 3
    assert "3 times" in v3.reason


def test_loop_threshold_is_configurable() -> None:
    det = LoopDetector(loop_threshold=5)
    for _ in range(4):
        det.observe("sess", "Bash", {"command": "ls"})
    v5 = det.observe("sess", "Bash", {"command": "ls"})
    assert v5.kind == "loop"
    assert v5.count == 5


def test_redundant_read_only_call_flagged() -> None:
    det = LoopDetector(loop_threshold=10)
    det.observe("sess", "Read", {"file_path": "/x"})
    v2 = det.observe("sess", "Read", {"file_path": "/x"})
    assert v2.kind == "redundant"


def test_redundant_inactive_for_non_read_only_tools() -> None:
    det = LoopDetector(loop_threshold=10)
    det.observe("sess", "Bash", {"command": "ls"})
    v2 = det.observe("sess", "Bash", {"command": "ls"})
    assert v2.kind is None


def test_redundant_disabled_outside_window() -> None:
    det = LoopDetector(loop_threshold=10, dedup_window_secs=0.01)
    det.observe("sess", "Read", {"file_path": "/x"})
    time.sleep(0.05)
    v2 = det.observe("sess", "Read", {"file_path": "/x"})
    assert v2.kind is None


def test_loop_isolated_per_session() -> None:
    det = LoopDetector(loop_threshold=3)
    for _ in range(3):
        det.observe("sess-A", "Bash", {"command": "ls"})
    v_b1 = det.observe("sess-B", "Bash", {"command": "ls"})
    assert v_b1.kind is None
    assert v_b1.count == 1


def test_different_args_dont_collide() -> None:
    det = LoopDetector(loop_threshold=3)
    det.observe("sess", "Bash", {"command": "ls"})
    det.observe("sess", "Bash", {"command": "ls"})
    v_other = det.observe("sess", "Bash", {"command": "pwd"})
    assert v_other.kind is None
    assert v_other.count == 1


def test_stats_summary() -> None:
    det = LoopDetector(loop_threshold=3)
    det.observe("sess", "Bash", {"command": "ls"})
    det.observe("sess", "Bash", {"command": "ls"})
    det.observe("sess", "Bash", {"command": "ls"})
    det.observe("sess", "Bash", {"command": "pwd"})
    s = det.stats("sess")
    assert s["calls"] == 4
    assert s["unique_calls"] == 2
    assert s["looping_keys"] == 1
    assert s["redundant_calls"] == 2  # ls seen 3 times → 2 redundant


def test_reset_clears_session() -> None:
    det = LoopDetector(loop_threshold=3)
    det.observe("sess", "Bash", {"command": "ls"})
    det.reset("sess")
    v_again = det.observe("sess", "Bash", {"command": "ls"})
    assert v_again.count == 1


def test_reset_all() -> None:
    det = LoopDetector(loop_threshold=3)
    det.observe("a", "Bash", {"command": "ls"})
    det.observe("b", "Bash", {"command": "ls"})
    det.reset()
    assert det.stats("a")["calls"] == 0
    assert det.stats("b")["calls"] == 0


def test_gc_keeps_only_retained_entries() -> None:
    det = LoopDetector(loop_threshold=3, retain_per_session=3)
    for i in range(10):
        det.observe("sess", "Bash", {"command": f"cmd-{i}"})
    s = det.stats("sess")
    assert s["unique_calls"] <= 3


# ---- get_default_detector singleton -------------------------------------


def test_default_detector_is_singleton() -> None:
    a = get_default_detector()
    b = get_default_detector()
    assert a is b


def test_reset_default_detector_creates_new_instance() -> None:
    a = get_default_detector()
    reset_default_detector()
    b = get_default_detector()
    assert a is not b


# ---- step336 integration ------------------------------------------------


def _atv_input(tool: str, args: dict[str, Any], aid: str = "test") -> ATVInput:
    return ATVInput(
        header=ATVHeader(
            trace_id="t" * 32,
            span_id="s" * 16,
            tenant_id="t",
            aid=aid,
            timestamp_ns=0,
        ),
        tool_name=tool,
        tool_args_json=json.dumps(args),
    )


def test_step336_first_call_passes() -> None:
    inp = _atv_input("Bash", {"command": "ls"})
    ctx = FirewallContext()
    res = step336_loop.run(np.zeros(2080, dtype=np.float32), inp, ctx)
    assert res.verdict is None
    assert ctx.extras["loop_count"] == 1
    assert "fresh call" in res.trace


def test_step336_third_repeat_returns_require_approval() -> None:
    det = get_default_detector()
    det.loop_threshold = 3
    inp = _atv_input("Bash", {"command": "ls"})
    ctx = FirewallContext()
    for _ in range(2):
        step336_loop.run(np.zeros(2080, dtype=np.float32), inp, ctx)
    ctx_final = FirewallContext()
    res = step336_loop.run(np.zeros(2080, dtype=np.float32), inp, ctx_final)
    assert res.verdict == "REQUIRE_APPROVAL"
    assert "loop" in res.trace
    assert ctx_final.extras["loop_count"] == 3


def test_step336_redundant_read_only_does_not_block() -> None:
    inp = _atv_input("Read", {"file_path": "/x"})
    ctx1 = FirewallContext()
    step336_loop.run(np.zeros(2080, dtype=np.float32), inp, ctx1)
    ctx2 = FirewallContext()
    res = step336_loop.run(np.zeros(2080, dtype=np.float32), inp, ctx2)
    assert res.verdict is None
    assert ctx2.extras.get("redundant") is True
    assert "redundant" in res.trace


def test_step336_isolates_by_aid() -> None:
    det = get_default_detector()
    det.loop_threshold = 3
    inp_a = _atv_input("Bash", {"command": "ls"}, aid="agent-A")
    inp_b = _atv_input("Bash", {"command": "ls"}, aid="agent-B")
    for _ in range(3):
        step336_loop.run(np.zeros(2080, dtype=np.float32), inp_a, FirewallContext())
    ctx_b = FirewallContext()
    res = step336_loop.run(np.zeros(2080, dtype=np.float32), inp_b, ctx_b)
    assert res.verdict is None
    assert ctx_b.extras["loop_count"] == 1
