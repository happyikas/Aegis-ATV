"""Unit tests for `aegis report --explain` and the audit-record enrichment.

The hook-side enrichment lives in ``tools/aegis_local_hook.py``; the
renderer lives in ``tools/aegis_cli.py``. This file exercises both.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

# Make tools/ importable for the local-hook module.
_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "tools"))

import aegis_cli  # noqa: E402,I001
import aegis_local_hook  # noqa: E402,I001


# ─────────────────────────────────────────────────────────────────────
# Hook-side: _build_explain_block
# ─────────────────────────────────────────────────────────────────────


def _atv_input(tool: str = "Bash", args: dict | None = None,
               state: str = "") -> object:
    """Build a real ATVInput suitable for build_atv()."""
    import time

    from aegis.schema import ATVHeader, ATVInput, CostEfficiencyMetrics
    return ATVInput(
        header=ATVHeader(
            trace_id="t-explain", span_id="s-explain",
            tenant_id="t", aid="a",
            timestamp_ns=time.time_ns(),
        ),
        agent_state_text=state,
        plan_text="",
        tool_name=tool,
        tool_args_json=json.dumps(args or {}, sort_keys=True),
        safety_flags={},
        memory_fingerprint="sha3:t",
        cost_estimate=CostEfficiencyMetrics(
            input_token_count=1, output_token_count=1,
        ),
    )


class TestBuildExplainBlock:
    def test_returns_dict_with_atv_fingerprint(self) -> None:
        from aegis.atv.builder import build_atv
        from aegis.firewall.core import run_firewall

        inp = _atv_input("Bash", {"command": "ls"}, "list dir")
        atv = build_atv(inp)
        v = run_firewall(atv, inp, atv_id=inp.header.span_id)

        block = aegis_local_hook._build_explain_block(atv, inp, v)
        assert isinstance(block, dict)
        assert block.get("atv_dim") == int(atv.shape[0])
        # SHA3 hex is 64 chars.
        assert len(str(block.get("atv_sha3", ""))) == 64

    def test_step_traces_filtered_to_non_trivial(self) -> None:
        from aegis.atv.builder import build_atv
        from aegis.firewall.core import run_firewall

        inp = _atv_input("Bash", {"command": "ls"}, "")
        atv = build_atv(inp)
        v = run_firewall(atv, inp, atv_id=inp.header.span_id)

        block = aegis_local_hook._build_explain_block(atv, inp, v)
        traces = block.get("step_traces", {})
        # Even on a benign call we expect the BLOCK-keyword filter to
        # keep numeric / hybrid / drift entries — but never trivial
        # ones like "ok" alone.
        for v_str in traces.values():
            assert isinstance(v_str, str)

    def test_m13_top_present_for_normal_atv(self) -> None:
        from aegis.atv.builder import build_atv
        from aegis.firewall.core import run_firewall

        inp = _atv_input(
            "Bash", {"command": "rm -rf /"}, "destructive call",
        )
        atv = build_atv(inp)
        v = run_firewall(atv, inp, atv_id=inp.header.span_id)

        block = aegis_local_hook._build_explain_block(atv, inp, v)
        assert "m13_top" in block
        assert isinstance(block["m13_top"], list)
        assert len(block["m13_top"]) <= 5
        for entry in block["m13_top"]:
            assert "subfield" in entry and "score" in entry
            assert 0.0 <= float(entry["score"]) <= 1.0

    def test_rag_block_absent_under_dummy_provider(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """RAG entries must NOT be written when bge-local isn't active —
        cosine on SHA3 noise would inject misleading data into the
        audit chain."""
        from aegis.atv.builder import build_atv
        from aegis.config import settings as _settings
        from aegis.firewall.core import run_firewall

        monkeypatch.setattr(_settings, "aegis_embedding_provider", "dummy")
        inp = _atv_input("Bash", {"command": "ls"}, "list dir")
        atv = build_atv(inp)
        v = run_firewall(atv, inp, atv_id=inp.header.span_id)

        block = aegis_local_hook._build_explain_block(atv, inp, v)
        assert "rag" not in block

    def test_session_drift_absent_when_no_state(self) -> None:
        from aegis.atv.builder import build_atv
        from aegis.firewall.core import run_firewall

        inp = _atv_input("Bash", {"command": "ls"}, "")
        atv = build_atv(inp)
        v = run_firewall(atv, inp, atv_id=inp.header.span_id)

        block = aegis_local_hook._build_explain_block(atv, inp, v)
        # No prior session record → no drift in the explain block.
        assert "session_drift" not in block

    def test_never_raises_on_garbage(self) -> None:
        """The hook calls this in the hot path — must NEVER raise."""
        block = aegis_local_hook._build_explain_block(
            atv="not a vector",   # type: ignore[arg-type]
            inp=None,
            verdict=None,
        )
        # Returns *something* (possibly nearly empty) but doesn't crash.
        assert isinstance(block, dict)


# ─────────────────────────────────────────────────────────────────────
# Renderer: _cmd_report_explain
# ─────────────────────────────────────────────────────────────────────


@pytest.fixture
def audit_log_with_explain(tmp_path: Path) -> Path:
    """Produce an audit JSONL with one fully-populated explain block."""
    path = tmp_path / "audit.jsonl"
    rec = {
        "ts_ns": 1_777_700_000_000_000_000,
        "tool": "Edit",
        "aid": "sess-test",
        "decision": "BLOCK",
        "reason": "credential_pattern matched",
        "trace_id": "abcdef0123456789" + "f" * 16,
        "latency_ms": 14.3,
        "mode": "local",
        "explain": {
            "atv_dim": 2080,
            "atv_sha3": "0123" * 16,
            "step_traces": {
                "aegis.firewall.step310_args.run": "step310: hit credential_pattern",
                "aegis.firewall.step340_policy.run": "step340: BLOCK conf=0.95",
            },
            "m13_top": [
                {"subfield": "tool_arg_inspection",        "score": 0.95},
                {"subfield": "output_content_fingerprint", "score": 0.70},
                {"subfield": "action_blast_radius",        "score": 0.25},
            ],
            "m13_score": 0.95,
            "rag": {
                "n_retrieved": 3,
                "top_cos": 0.79,
                "top_label": "BLOCK",
                "top_text": "agent committing AWS_SECRET to .env",
            },
            "session_drift": {
                "topic_drift": 0.42,
                "max_drift": 0.55,
                "n_calls": 7,
            },
        },
        "prev_hash": "0" * 64,
        "this_hash": "1" * 64,
    }
    path.write_text(json.dumps(rec) + "\n", encoding="utf-8")
    return path


def _run_explain(audit_path: Path, target: str) -> tuple[int, str]:
    """Capture stdout from _cmd_report_explain."""
    buf = io.StringIO()
    real_stdout = sys.stdout
    sys.stdout = buf
    try:
        rc = aegis_cli._cmd_report_explain(audit_path, target)
    finally:
        sys.stdout = real_stdout
    return rc, buf.getvalue()


class TestReportExplain:
    def test_last_renders_full_block(
        self, audit_log_with_explain: Path,
    ) -> None:
        rc, out = _run_explain(audit_log_with_explain, "LAST")
        assert rc == 0
        # Header bits
        assert "Decision Explanation" in out
        assert "BLOCK" in out
        # Each section heading present
        assert "Firewall steps" in out
        assert "M13 attribution" in out
        assert "step340 RAG" in out
        assert "Session behavioural drift" in out
        # Specific values surfaced
        assert "tool_arg_inspection" in out
        assert "0.950" in out or "0.95" in out
        assert "credential" in out.lower()

    def test_explicit_trace_prefix_finds_record(
        self, audit_log_with_explain: Path,
    ) -> None:
        rc, out = _run_explain(audit_log_with_explain, "abcdef01")
        assert rc == 0
        assert "BLOCK" in out
        assert "tool_arg_inspection" in out

    def test_unknown_trace_returns_nonzero(
        self, audit_log_with_explain: Path,
    ) -> None:
        rc, out = _run_explain(audit_log_with_explain, "no-such-trace")
        assert rc != 0
        assert "no record matches" in out.lower()

    def test_record_without_explain_block_renders_header_only(
        self, tmp_path: Path,
    ) -> None:
        path = tmp_path / "old.jsonl"
        rec = {
            "ts_ns": 1, "tool": "Bash", "aid": "a",
            "decision": "ALLOW", "reason": "ok", "trace_id": "x" * 32,
            "latency_ms": 1.0, "mode": "local",
        }
        path.write_text(json.dumps(rec) + "\n", encoding="utf-8")
        rc, out = _run_explain(path, "LAST")
        assert rc == 0
        assert "ALLOW" in out
        # Should warn that explain is missing
        assert "no explain block" in out.lower() or "predates" in out.lower()

    def test_skips_posttooluse_records(self, tmp_path: Path) -> None:
        """PostToolUse records carry no `decision` field — must be
        skipped in favour of the actual PreToolUse decision."""
        path = tmp_path / "mixed.jsonl"
        post = {
            "ts_ns": 1, "tool": "Bash", "aid": "a", "hook": "PostToolUse",
            "trace_id": "post" * 8, "result_hash": "h" * 64,
        }
        pre = {
            "ts_ns": 2, "tool": "Bash", "aid": "a",
            "decision": "ALLOW", "reason": "ok", "trace_id": "pre" * 11,
            "latency_ms": 1.0, "mode": "local",
        }
        path.write_text(
            json.dumps(post) + "\n" + json.dumps(pre) + "\n",
            encoding="utf-8",
        )
        rc, out = _run_explain(path, "LAST")
        assert rc == 0
        assert "ALLOW" in out

    def test_argparse_wires_explain_flag(self) -> None:
        args = aegis_cli.build_parser().parse_args(
            ["report", "--explain", "LAST"]
        )
        assert args.explain == "LAST"
        assert args.fn is aegis_cli.cmd_report

    def test_default_explain_is_none(self) -> None:
        args = aegis_cli.build_parser().parse_args(["report"])
        assert args.explain is None
