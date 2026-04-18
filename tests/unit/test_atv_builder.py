"""Unit tests for the ATV builder and encoders (PLAN 6.1-6.3)."""

from __future__ import annotations

import time

import numpy as np
import pytest

from aegis.atv.builder import (
    SAFETY_FLAG_KEYS,
    build_atv,
    encode_header,
    encode_memory_fp,
    encode_safety_flags,
)
from aegis.atv.embeddings import DummyEmbedding, get_provider
from aegis.schema import (
    ATV_DIM,
    SLICE_AGENT_STATE,
    SLICE_COST_EFFICIENCY,
    SLICE_HEADER,
    SLICE_IO_PROFILE,
    SLICE_LINKAGE,
    SLICE_MEMORY_FP,
    SLICE_PLAN,
    SLICE_SAFETY_FLAGS,
    SLICE_TOOL_CALL,
    ATVHeader,
    ATVInput,
    CostEfficiency,
)


def _input(**overrides: object) -> ATVInput:
    base = {
        "header": ATVHeader(
            trace_id="t-1",
            span_id="s-1",
            tenant_id="demo-tenant",
            aid="agent-x",
            timestamp_ns=time.time_ns(),
        ),
        "agent_state_text": "user asked to read a file",
        "plan_text": "plan: read then summarize",
        "tool_name": "read_file",
        "tool_args_json": '{"path":"./data/x.txt"}',
        "safety_flags": {"prompt_injection": 0.05},
        "memory_fingerprint": "sha3:abcdef",
        "cost_estimate": CostEfficiency(exp_bytes_write=1024, exp_dollars=0.0001),
    }
    base.update(overrides)
    return ATVInput(**base)  # type: ignore[arg-type]


class TestSlices:
    def test_slices_are_contiguous_and_total_2080(self) -> None:
        ranges = [
            SLICE_HEADER,
            SLICE_AGENT_STATE,
            SLICE_PLAN,
            SLICE_TOOL_CALL,
            SLICE_SAFETY_FLAGS,
            SLICE_MEMORY_FP,
            SLICE_COST_EFFICIENCY,
            SLICE_IO_PROFILE,
        ]
        for a, b in zip(ranges, ranges[1:], strict=False):
            assert a.stop == b.start, f"gap between {a} and {b}"
        assert SLICE_LINKAGE.stop == ATV_DIM


class TestEncodeHeader:
    def test_deterministic(self) -> None:
        h = ATVHeader(
            trace_id="t",
            span_id="s",
            tenant_id="x",
            aid="a",
            timestamp_ns=1,
        )
        assert np.array_equal(encode_header(h), encode_header(h))

    def test_dim_64_and_in_range(self) -> None:
        h = ATVHeader(
            trace_id="t",
            span_id="s",
            tenant_id="x",
            aid="a",
            timestamp_ns=1,
        )
        v = encode_header(h)
        assert v.shape == (64,)
        assert v.dtype == np.float32
        assert v.min() >= -1.0 and v.max() <= 1.0


class TestEncodeSafetyFlags:
    def test_zero_when_empty(self) -> None:
        v = encode_safety_flags({})
        assert v.shape == (256,)
        assert (v == 0).all()

    def test_known_key_lands_in_known_slot(self) -> None:
        v = encode_safety_flags({"prompt_injection": 0.91})
        idx = SAFETY_FLAG_KEYS.index("prompt_injection")
        assert v[idx] == pytest.approx(0.91)

    def test_unknown_key_ignored(self) -> None:
        v = encode_safety_flags({"never_heard_of_it": 1.0})
        assert (v == 0).all()


class TestEncodeMemoryFP:
    def test_none_is_zero(self) -> None:
        assert (encode_memory_fp(None) == 0).all()

    def test_dim_136(self) -> None:
        assert encode_memory_fp("hello").shape == (136,)


class TestDummyEmbedding:
    def test_deterministic_and_normalized(self) -> None:
        e = DummyEmbedding()
        a = e.embed("hello world", 384)
        b = e.embed("hello world", 384)
        assert np.array_equal(a, b)
        assert a.shape == (384,)
        assert float(np.linalg.norm(a)) == pytest.approx(1.0, abs=1e-5)

    def test_different_text_different_vec(self) -> None:
        e = DummyEmbedding()
        assert not np.array_equal(e.embed("a", 64), e.embed("b", 64))

    def test_get_provider_returns_dummy_in_test(self) -> None:
        assert isinstance(get_provider(), DummyEmbedding)


class TestBuildATV:
    def test_dimension_is_2080(self) -> None:
        atv = build_atv(_input())
        assert atv.shape == (ATV_DIM,)
        assert atv.dtype == np.float32

    def test_hardware_band_zero(self) -> None:
        atv = build_atv(_input())
        assert (atv[SLICE_IO_PROFILE] == 0).all()
        assert (atv[SLICE_LINKAGE] == 0).all()
        assert (atv[1880:2080] == 0).all()

    def test_software_bands_populated(self) -> None:
        atv = build_atv(_input())
        # at least one non-zero element in each non-trivial software band
        assert np.any(atv[SLICE_HEADER] != 0)
        assert np.any(atv[SLICE_AGENT_STATE] != 0)
        assert np.any(atv[SLICE_PLAN] != 0)
        assert np.any(atv[SLICE_TOOL_CALL] != 0)
        assert np.any(atv[SLICE_SAFETY_FLAGS] != 0)
        assert np.any(atv[SLICE_MEMORY_FP] != 0)
        assert np.any(atv[SLICE_COST_EFFICIENCY] != 0)

    def test_cost_estimate_lands_in_slice(self) -> None:
        ce = CostEfficiency(exp_dollars=0.5, confidence=0.7)
        atv = build_atv(_input(cost_estimate=ce))
        ce_arr = atv[SLICE_COST_EFFICIENCY]
        # exp_dollars is at index 8, confidence at index 9 (per CostEfficiency.to_array order)
        assert ce_arr[8] == pytest.approx(0.5)
        assert ce_arr[9] == pytest.approx(0.7)

    def test_deterministic_for_same_input(self) -> None:
        inp = _input()
        a = build_atv(inp)
        b = build_atv(inp)
        assert np.array_equal(a, b)
