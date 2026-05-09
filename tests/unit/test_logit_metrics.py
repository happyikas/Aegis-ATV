"""Unit tests for PR-G: logit-level forensic surface (Local OSS LLM).

Three surfaces under test:

1. ``LogitMetrics`` dataclass — to_dict / from_dict round-trip,
   confidence band heuristic, hallucination_risk predicate.
2. ``parse_vllm_logprobs`` — handles vLLM native + OpenAI-compat
   logprobs response shapes.
3. ``aegis forensic --logits`` — renders the metrics block when
   present in the audit record's explain.logit_metrics field.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import pytest

from aegis.inference.logit_metrics import (
    DEFAULT_LOW_CONFIDENCE_THRESHOLD,
    LogitMetrics,
    parse_vllm_logprobs,
)
from tools import aegis_cli

# ── parse_vllm_logprobs — vLLM native shape ────────────────────────


def test_parse_native_shape_basic() -> None:
    payload = [
        {"token": "Hello", "logprob": -0.012},
        {"token": " world", "logprob": -0.003},
        {"token": "!", "logprob": -0.001},
    ]
    m = parse_vllm_logprobs(payload)
    assert m is not None
    assert m.n_tokens == 3
    assert m.mean_logprob == pytest.approx(
        (-0.012 + -0.003 + -0.001) / 3, rel=1e-6,
    )
    assert m.min_logprob == pytest.approx(-0.012)
    assert m.n_low_confidence_tokens == 0


def test_parse_with_low_confidence_tokens() -> None:
    payload = [
        {"token": "The", "logprob": -0.05},
        {"token": " ", "logprob": -0.001},
        {"token": "obscure", "logprob": -3.5},   # below default threshold
        {"token": " ", "logprob": -0.001},
        {"token": "thing", "logprob": -2.8},     # below threshold
    ]
    m = parse_vllm_logprobs(payload)
    assert m is not None
    assert m.n_low_confidence_tokens == 2
    assert ("obscure", -3.5) in m.sample_low_confidence_tokens
    assert ("thing", -2.8) in m.sample_low_confidence_tokens
    # Sample is sorted by ascending logprob (most-uncertain first)
    assert m.sample_low_confidence_tokens[0][1] < m.sample_low_confidence_tokens[1][1]


def test_parse_custom_threshold() -> None:
    """Operator can dial the threshold per use case."""
    payload = [
        {"token": "a", "logprob": -1.0},
        {"token": "b", "logprob": -1.5},
    ]
    m = parse_vllm_logprobs(
        payload,
        low_confidence_threshold=-0.5,
    )
    assert m is not None
    # Both tokens are below the stricter threshold.
    assert m.n_low_confidence_tokens == 2


def test_parse_skips_nan_inf() -> None:
    """vLLM emits -inf for numerically-unstable kernels — these would
    skew the mean if included. Parser drops them."""
    payload = [
        {"token": "a", "logprob": -0.1},
        {"token": "b", "logprob": -math.inf},
        {"token": "c", "logprob": -0.2},
        {"token": "d", "logprob": math.nan},
    ]
    m = parse_vllm_logprobs(payload)
    assert m is not None
    assert m.n_tokens == 2  # the inf + nan dropped
    assert m.mean_logprob == pytest.approx(-0.15)


def test_parse_empty_payload_returns_none() -> None:
    assert parse_vllm_logprobs([]) is None
    assert parse_vllm_logprobs(None) is None


def test_parse_skips_malformed_entries() -> None:
    """Entries missing logprob or with non-numeric logprob are skipped,
    but valid entries in the same payload still parse."""
    payload = [
        {"token": "a", "logprob": -0.1},
        {"token": "b"},  # missing logprob
        "not a dict",
        {"token": "c", "logprob": "not a number"},
        {"token": "d", "logprob": -0.2},
    ]
    m = parse_vllm_logprobs(payload)
    assert m is not None
    assert m.n_tokens == 2  # only a + d


def test_parse_invalid_payload_raises() -> None:
    with pytest.raises(ValueError, match="logprobs payload"):
        parse_vllm_logprobs("not a list")  # type: ignore[arg-type]


# ── parse_vllm_logprobs — OpenAI-compat shape ──────────────────────


def test_parse_openai_compat_shape() -> None:
    """vLLM with the OpenAI-compatible API returns parallel
    ``tokens`` + ``token_logprobs`` arrays nested under a single
    object instead of one entry per token."""
    payload = [
        {
            "tokens": ["Hello", " world", "!"],
            "token_logprobs": [-0.012, -0.003, -0.001],
        },
    ]
    m = parse_vllm_logprobs(payload)
    assert m is not None
    assert m.n_tokens == 3
    assert m.min_logprob == pytest.approx(-0.012)


def test_parse_openai_compat_skips_null_first_token() -> None:
    """OpenAI logprobs convention puts None as the logprob of the
    first token (no preceding context). The parser drops it."""
    payload = [
        {
            "tokens": ["Hello", " world"],
            "token_logprobs": [None, -0.003],
        },
    ]
    m = parse_vllm_logprobs(payload)
    assert m is not None
    assert m.n_tokens == 1
    assert m.min_logprob == pytest.approx(-0.003)


# ── LogitMetrics — confidence band + hallucination risk ────────────


@pytest.mark.parametrize(
    "n_tokens,n_low,band",
    [
        (100, 4, "high"),
        (100, 9, "moderate"),
        (100, 25, "low"),
        (100, 50, "critical"),
        (0, 0, "unknown"),
    ],
)
def test_confidence_band(
    n_tokens: int, n_low: int, band: str,
) -> None:
    m = LogitMetrics(
        n_tokens=n_tokens,
        mean_logprob=0.0,
        min_logprob=0.0,
        n_low_confidence_tokens=n_low,
    )
    assert m.confidence_band() == band


def test_hallucination_risk_below_low() -> None:
    m = LogitMetrics(
        n_tokens=100, mean_logprob=0.0, min_logprob=0.0,
        n_low_confidence_tokens=10,  # 10% → moderate
    )
    assert m.hallucination_risk() is False


def test_hallucination_risk_at_low() -> None:
    m = LogitMetrics(
        n_tokens=100, mean_logprob=0.0, min_logprob=0.0,
        n_low_confidence_tokens=20,  # 20% → low
    )
    assert m.hallucination_risk() is True


def test_hallucination_risk_at_critical() -> None:
    m = LogitMetrics(
        n_tokens=100, mean_logprob=0.0, min_logprob=0.0,
        n_low_confidence_tokens=40,  # 40% → critical
    )
    assert m.hallucination_risk() is True


# ── to_dict / from_dict round-trip ─────────────────────────────────


def test_logit_metrics_dict_round_trip() -> None:
    m = LogitMetrics(
        n_tokens=10,
        mean_logprob=-0.5,
        min_logprob=-3.2,
        n_low_confidence_tokens=2,
        low_confidence_threshold=-2.3,
        sample_low_confidence_tokens=[("foo", -3.2), ("bar", -2.5)],
    )
    out = m.to_dict()
    m2 = LogitMetrics.from_dict(out)
    assert m2.n_tokens == m.n_tokens
    assert m2.mean_logprob == pytest.approx(m.mean_logprob)
    assert m2.min_logprob == pytest.approx(m.min_logprob)
    assert m2.sample_low_confidence_tokens == m.sample_low_confidence_tokens


def test_logit_metrics_from_dict_handles_legacy_tuple_sample() -> None:
    """Older audit records may have stored sample tokens as
    [token, logprob] lists instead of {token: ..., logprob: ...}
    objects. Parser tolerates both."""
    legacy = {
        "n_tokens": 5,
        "mean_logprob": -0.1,
        "min_logprob": -2.0,
        "n_low_confidence_tokens": 0,
        "sample_low_confidence_tokens": [["foo", -2.0], ["bar", -1.5]],
    }
    m = LogitMetrics.from_dict(legacy)
    assert ("foo", -2.0) in m.sample_low_confidence_tokens


def test_logit_metrics_from_dict_uses_default_threshold() -> None:
    """Audit records that predate the threshold field still parse —
    the default is recorded."""
    data = {
        "n_tokens": 1,
        "mean_logprob": -0.1,
        "min_logprob": -0.1,
        "n_low_confidence_tokens": 0,
    }
    m = LogitMetrics.from_dict(data)
    assert m.low_confidence_threshold == DEFAULT_LOW_CONFIDENCE_THRESHOLD


# ── aegis forensic --logits CLI integration ────────────────────────


def _make_audit_record_with_logits(
    tmp_path: Path, *, with_logits: bool = True,
) -> Path:
    audit_path = tmp_path / "audit.jsonl"
    rec: dict = {
        "ts_ns": 1_700_000_000_000_000_000,
        "tool": "Bash",
        "aid": "sess-test",
        "decision": "ALLOW",
        "reason": "",
        "trace_id": "abc12345",
        "latency_ms": 4.2,
        "explain": {},
    }
    if with_logits:
        rec["explain"]["logit_metrics"] = {
            "n_tokens": 50,
            "mean_logprob": -1.2,
            "min_logprob": -4.5,
            "n_low_confidence_tokens": 18,  # 36% → critical
            "low_confidence_threshold": -2.3,
            "sample_low_confidence_tokens": [
                {"token": "obscure", "logprob": -4.5},
                {"token": "rare", "logprob": -3.8},
                {"token": "ambiguous", "logprob": -3.2},
            ],
        }
    audit_path.write_text(json.dumps(rec) + "\n")
    return audit_path


def test_forensic_logits_flag_renders_metrics(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    audit_path = _make_audit_record_with_logits(tmp_path)
    args = argparse.Namespace(
        audit=str(audit_path),
        selector="sess-test",
        trace=None,
        since=None,
        limit=0,
        json=False,
        logits=True,
    )
    rc = aegis_cli.cmd_forensic(args)
    assert rc == 0
    out = capsys.readouterr().out
    assert "logits" in out
    assert "[critical]" in out  # 36% low-confidence → critical band
    assert "obscure" in out  # sample token rendered
    assert "low-confidence: 18/50" in out


def test_forensic_logits_flag_silent_when_no_data(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Cloud LLM track records have no logit_metrics — flag should
    be a clean no-op (no error, no banner)."""
    audit_path = _make_audit_record_with_logits(
        tmp_path, with_logits=False,
    )
    args = argparse.Namespace(
        audit=str(audit_path),
        selector="sess-test",
        trace=None,
        since=None,
        limit=0,
        json=False,
        logits=True,
    )
    rc = aegis_cli.cmd_forensic(args)
    assert rc == 0
    out = capsys.readouterr().out
    # No "logits:" line in the rendered timeline.
    assert "└─ logits:" not in out


def test_forensic_without_logits_flag_skips_block(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Default forensic doesn't render logits even when the audit
    record has them — flag is opt-in to keep the timeline compact."""
    audit_path = _make_audit_record_with_logits(tmp_path)
    args = argparse.Namespace(
        audit=str(audit_path),
        selector="sess-test",
        trace=None,
        since=None,
        limit=0,
        json=False,
        logits=False,  # ← opt-in disabled
    )
    rc = aegis_cli.cmd_forensic(args)
    assert rc == 0
    out = capsys.readouterr().out
    assert "└─ logits:" not in out


def test_forensic_logits_flag_parses() -> None:
    parser = aegis_cli.build_parser()
    args = parser.parse_args(["forensic", "x", "--logits"])
    assert args.logits is True
    args = parser.parse_args(["forensic", "x"])
    assert args.logits is False
