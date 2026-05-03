"""Unit tests for src/aegis/judge/local_phi.py (v2.6)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from aegis.atv.builder import build_atv
from aegis.cost.model_flops import DEFAULT_DOLLAR_PER_FLOP, expected_flops
from aegis.judge.local_phi import (
    LocalPhiJudge,
    _stub_model_hash,
    reset_model_hash_cache,
)
from aegis.schema import ATVHeader, ATVInput, CostEfficiencyMetrics


@pytest.fixture(autouse=True)
def _clear_caches(monkeypatch: pytest.MonkeyPatch) -> None:
    reset_model_hash_cache()
    # Each test starts with a clean env
    for k in (
        "AEGIS_JUDGE_MODEL_PATH",
        "AEGIS_JUDGE_LOCAL_PHI_STUB",
    ):
        monkeypatch.delenv(k, raising=False)


def _atv_input(
    tool: str = "Bash",
    args: dict[str, Any] | None = None,
    *,
    in_tokens: float = 1000.0,
    out_tokens: float = 500.0,
    model: str = "claude-haiku-4-5",
) -> ATVInput:
    args = args or {"command": "ls"}
    cum_dollars = expected_flops(model, in_tokens, out_tokens) * DEFAULT_DOLLAR_PER_FLOP
    return ATVInput(
        header=ATVHeader(
            trace_id="t" * 32,
            span_id="s" * 16,
            tenant_id="demo",
            aid="agent-test",
            timestamp_ns=0,
            model_hash=model,
        ),
        tool_name=tool,
        tool_args_json=json.dumps(args),
        cost_estimate=CostEfficiencyMetrics(
            input_token_count=in_tokens,
            output_token_count=out_tokens,
            cumulative_tokens=in_tokens + out_tokens,
            cumulative_dollars=cum_dollars,
        ),
    )


# ---- Mode detection ----------------------------------------------------


def test_default_mode_is_stub_when_no_env() -> None:
    judge = LocalPhiJudge()
    mode, info = judge._decide_mode()
    assert mode == "stub"
    assert info is None


def test_explicit_stub_forced(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AEGIS_JUDGE_LOCAL_PHI_STUB", "1")
    judge = LocalPhiJudge()
    mode, _ = judge._decide_mode()
    assert mode == "stub"


def test_disabled_when_model_path_does_not_exist(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("AEGIS_JUDGE_MODEL_PATH", str(tmp_path / "absent.gguf"))
    judge = LocalPhiJudge()
    mode, info = judge._decide_mode()
    assert mode == "disabled"
    assert "does not exist" in (info or "")


def test_disabled_when_llama_cpp_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A real-looking GGUF path but llama-cpp-python missing → disabled,
    not crash."""
    fake_gguf = tmp_path / "fake.gguf"
    fake_gguf.write_bytes(b"not a real GGUF file")
    monkeypatch.setenv("AEGIS_JUDGE_MODEL_PATH", str(fake_gguf))
    judge = LocalPhiJudge()
    mode, info = judge._decide_mode()
    # Either "disabled" (llama-cpp not installed in CI) or "real" (if
    # llama-cpp is installed but loads the bogus file as None) — both
    # acceptable. The test asserts no crash.
    assert mode in {"disabled", "real"}
    if mode == "disabled":
        assert info  # has a reason


# ---- model_hash ---------------------------------------------------------


def test_stub_model_hash_is_deterministic() -> None:
    a = LocalPhiJudge().model_hash
    b = LocalPhiJudge().model_hash
    assert a == b
    assert a == _stub_model_hash()


def test_real_model_hash_is_file_sha3(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When the GGUF file exists, model_hash is the SHA3 of the file
    contents — not the stub hash."""
    import hashlib
    fake_gguf = tmp_path / "fake.gguf"
    payload = b"v2.6 test model contents " * 1000
    fake_gguf.write_bytes(payload)
    monkeypatch.setenv("AEGIS_JUDGE_MODEL_PATH", str(fake_gguf))
    monkeypatch.delenv("AEGIS_JUDGE_LOCAL_PHI_STUB", raising=False)

    expected = hashlib.sha3_256(payload).hexdigest()
    j = LocalPhiJudge()
    if j._decide_mode()[0] == "real":
        # llama-cpp is installed and loaded — model_hash should be
        # the file SHA3.
        assert j.model_hash == expected
    else:
        # llama-cpp missing → mode is disabled; model_hash is stub.
        # We accept either path; the important contract is "real path
        # → file SHA3".
        pytest.skip("llama-cpp not installed; cannot verify real path")


# ---- Stub evaluate path ------------------------------------------------


def test_stub_evaluate_returns_judge_verdict_with_model_hash() -> None:
    judge = LocalPhiJudge()
    inp = _atv_input(tool="Bash", args={"command": "rm -rf /tmp/foo"})
    atv = build_atv(inp)
    v = judge.evaluate_full("", atv=atv, inp=inp)
    assert v.decision in {"ALLOW", "BLOCK", "REQUIRE_APPROVAL"}
    assert v.model_hash == _stub_model_hash()
    assert v.latency_ms is not None and v.latency_ms < 100.0


def test_stub_block_for_destructive_args() -> None:
    judge = LocalPhiJudge()
    inp = _atv_input(tool="Bash", args={"command": "rm -rf /tmp/foo"})
    atv = build_atv(inp)
    v = judge.evaluate_full("", atv=atv, inp=inp)
    # Stub uses M13 attribution which fires on destructive_verb.
    assert v.decision in {"BLOCK", "REQUIRE_APPROVAL"}
    assert "local-phi (stub)" in v.reason


def test_stub_allow_for_innocent_read() -> None:
    judge = LocalPhiJudge()
    inp = _atv_input(
        tool="read_file",
        args={"file_path": "/tmp/x.txt"},
        in_tokens=10.0,
        out_tokens=5.0,
    )
    atv = build_atv(inp)
    v = judge.evaluate_full("", atv=atv, inp=inp)
    assert v.decision == "ALLOW"
    assert "local-phi (stub)" in v.reason


def test_stub_text_only_path() -> None:
    """evaluate(summary) without ATV — falls through to attribution head's
    text fallback."""
    judge = LocalPhiJudge()
    v = judge.evaluate("tool: sql\nargs: drop table users")
    assert v.decision == "BLOCK"
    assert v.model_hash == _stub_model_hash()


def test_stub_deterministic_same_input() -> None:
    judge = LocalPhiJudge()
    inp = _atv_input()
    atv = build_atv(inp)
    v1 = judge.evaluate_full("", atv=atv, inp=inp)
    v2 = judge.evaluate_full("", atv=atv, inp=inp)
    assert v1.decision == v2.decision
    assert v1.model_hash == v2.model_hash
    assert v1.reason == v2.reason


# ---- Disabled path -----------------------------------------------------


def test_disabled_returns_low_confidence_allow(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When the configured model is unreachable, return a clear
    low-confidence ALLOW with an explanatory reason. The HybridJudge
    interprets confidence==0.0 as 'fall through to next layer'."""
    monkeypatch.setenv("AEGIS_JUDGE_MODEL_PATH", str(tmp_path / "absent.gguf"))
    judge = LocalPhiJudge()
    inp = _atv_input()
    atv = build_atv(inp)
    v = judge.evaluate_full("", atv=atv, inp=inp)
    assert v.decision == "ALLOW"
    assert v.confidence == 0.0
    assert "local-phi disabled" in v.reason


# ---- _parse_real_decode (logic-only, no real model) -------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('{"decision": "BLOCK", "reason": "destructive"}', "BLOCK"),
        ('{"decision": "ALLOW", "reason": "ok"}', "ALLOW"),
        ('{"decision": "REQUIRE_APPROVAL", "reason": "high blast"}', "REQUIRE_APPROVAL"),
        ("Verdict: BLOCK because rm -rf", "BLOCK"),
        ("ALLOW — nominal", "ALLOW"),
        ("REQUIRE_APPROVAL — please confirm", "REQUIRE_APPROVAL"),
    ],
)
def test_parse_real_decode(text: str, expected: str) -> None:
    from aegis.judge.local_phi import _parse_real_decode

    decision, conf, _ = _parse_real_decode(text)
    assert decision == expected
    assert 0.0 <= conf <= 1.0


def test_parse_real_decode_unparseable_falls_back_to_allow() -> None:
    from aegis.judge.local_phi import _parse_real_decode

    decision, _, _ = _parse_real_decode("blah blah no decision here")
    assert decision == "ALLOW"


# ---- Phi-3.5 markdown-fenced output (tests the strip + parse path) -----


@pytest.mark.parametrize(
    ("text", "expected_decision", "expected_in_reason"),
    [
        # Phi-3.5-mini canonical greedy output: \n\n```json\n{...}\n```
        (
            '\n\n```json\n{"decision": "BLOCK", "reason": "credentials in args"}\n```',
            "BLOCK",
            "credentials",
        ),
        # Without the json tag (some models emit just ```)
        (
            '\n```\n{"decision": "ALLOW", "reason": "read-only"}\n```',
            "ALLOW",
            "read-only",
        ),
        # Trailing prose after the fence — common Phi-3.5 pattern
        (
            '\n```json\n{"decision": "REQUIRE_APPROVAL", "reason": "high impact"}\n```\n\n'
            '### Answer\n\n```json\n{"decision": "BLOCK", ...',
            "REQUIRE_APPROVAL",
            "high impact",
        ),
        # Llama-1B raw style still works (no fence)
        (
            '{"decision": "BLOCK", "reason": "destructive verb"}',
            "BLOCK",
            "destructive",
        ),
    ],
)
def test_parse_real_decode_markdown_fence_phi35(
    text: str, expected_decision: str, expected_in_reason: str,
) -> None:
    from aegis.judge.local_phi import _parse_real_decode

    decision, conf, reason = _parse_real_decode(text)
    assert decision == expected_decision, (
        f"failed to extract {expected_decision} from:\n{text}\n→ got {decision!r} ({reason!r})"
    )
    assert expected_in_reason.lower() in reason.lower()
    assert conf > 0.0


def test_strip_markdown_fence_returns_inner_json() -> None:
    """Direct unit test of the helper — must not crash on any input."""
    from aegis.judge.local_phi import _strip_markdown_fence

    # With fence
    assert "decision" in _strip_markdown_fence(
        '\n\n```json\n{"decision": "BLOCK"}\n```'
    )
    # Without fence — passes through trim
    assert _strip_markdown_fence("  hello  ") == "hello"
    # Empty
    assert _strip_markdown_fence("") == ""
    # Garbage
    assert isinstance(_strip_markdown_fence("```json\nbroken"), str)


# ---- get_judge() integration ------------------------------------------


def test_get_judge_returns_local_phi(monkeypatch: pytest.MonkeyPatch) -> None:
    from aegis.config import settings
    from aegis.judge import get_judge

    monkeypatch.setattr(settings, "aegis_judge_provider", "local-phi")
    j = get_judge()
    assert isinstance(j, LocalPhiJudge)
