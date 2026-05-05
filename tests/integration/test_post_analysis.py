"""PostToolUse content-analysis tests.

Covers the four signals the analyser surfaces:

1. **classify_response** — derives flags from tool_response without
   storing the body (privacy-first)
2. **detect_backtrack** — Edit reverts a prior Edit's insertion
3. **detect_redundant_call** — same tool + same args within lookback
4. **compute_duration_ms** — Pre→Post wall-clock from intent_log
5. **End-to-end** through post_tool.py — audit record carries the
   ``post_analysis`` block in the explain section

Privacy regression: by default the audit must NOT contain the raw
``tool_response`` body. Only ``size_bytes``, ``line_count``, classify
flags, and (opt-in) preview.
"""

from __future__ import annotations

import io
import json
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from aegis.cost.post_analysis import (
    BacktrackEvidence,
    PostAnalysis,
    ResponseClassification,
    analyse_post_tool_event,
    classify_response,
    compute_duration_ms,
    detect_backtrack,
    detect_redundant_call,
    inserted_string_hashes_for_audit,
    to_audit_dict,
)

# ─────────────────────────────────────────────────────────────────────
# 1. classify_response
# ─────────────────────────────────────────────────────────────────────


class TestClassifyResponse:
    def test_empty_response(self) -> None:
        c = classify_response(None)
        assert c.is_empty is True
        assert c.size_bytes == 0
        assert c.line_count == 0
        assert c.is_error is False
        assert c.preview is None

    def test_string_size_and_lines(self) -> None:
        body = "line1\nline2\nline3"
        c = classify_response(body)
        assert c.size_bytes == len(body.encode())
        assert c.line_count == 3
        assert c.is_empty is False

    def test_dict_with_stdout(self) -> None:
        c = classify_response({"stdout": "hello world", "stderr": ""})
        assert c.size_bytes == len(b"hello world")
        assert c.line_count == 1

    def test_error_via_is_error_flag(self) -> None:
        c = classify_response({"is_error": True, "content": "Oops"})
        assert c.is_error is True

    def test_error_via_exit_code(self) -> None:
        c = classify_response("ran fine", exit_code=1)
        assert c.is_error is True

    def test_no_error_at_exit_zero(self) -> None:
        c = classify_response("output", exit_code=0)
        assert c.is_error is False

    def test_url_detection(self) -> None:
        # URL with multi-segment path → both has_url and has_path fire.
        # That's acceptable — URLs DO contain paths. has_url is the
        # more specific signal; downstream consumers can prefer it.
        c = classify_response("see https://example.com/foo/bar for details")
        assert c.has_url is True

    def test_url_only_no_pathlike(self) -> None:
        # Bare hostname / single-segment path — only has_url.
        c = classify_response("ping https://example.com")
        assert c.has_url is True
        assert c.has_path is False

    def test_path_detection(self) -> None:
        c = classify_response("/usr/local/bin/python is at this location")
        assert c.has_path is True
        assert c.has_url is False

    def test_traceback_detection(self) -> None:
        body = (
            "Traceback (most recent call last):\n"
            "  File \"foo.py\", line 1, in <module>\n"
            "Exception: boom"
        )
        c = classify_response(body, exit_code=1)
        assert c.has_traceback is True
        assert c.is_error is True

    def test_preview_off_by_default(self) -> None:
        c = classify_response("secret-token-xyz")
        assert c.preview is None

    def test_preview_on_when_capture_true(self) -> None:
        c = classify_response("hello world", capture_preview=True)
        assert c.preview == "hello world"

    def test_preview_truncated(self) -> None:
        long = "x" * 200
        c = classify_response(long, capture_preview=True)
        assert c.preview is not None
        assert len(c.preview) <= 81   # 80 chars + ellipsis
        assert c.preview.endswith("…")


# ─────────────────────────────────────────────────────────────────────
# 2. inserted_string_hashes_for_audit (helper)
# ─────────────────────────────────────────────────────────────────────


class TestInsertedHashes:
    def test_edit_returns_one_hash(self) -> None:
        hashes = inserted_string_hashes_for_audit(
            tool_name="Edit",
            tool_input={"old_string": "foo", "new_string": "bar"},
        )
        assert len(hashes) == 1
        assert len(hashes[0]) == 16

    def test_multi_edit_returns_one_per_edit(self) -> None:
        hashes = inserted_string_hashes_for_audit(
            tool_name="MultiEdit",
            tool_input={
                "edits": [
                    {"old_string": "a", "new_string": "alpha"},
                    {"old_string": "b", "new_string": "beta"},
                    {"old_string": "c", "new_string": "gamma"},
                ],
            },
        )
        assert len(hashes) == 3

    def test_non_edit_tool_returns_empty(self) -> None:
        assert inserted_string_hashes_for_audit(
            tool_name="Bash",
            tool_input={"command": "ls"},
        ) == []


# ─────────────────────────────────────────────────────────────────────
# 3. detect_backtrack
# ─────────────────────────────────────────────────────────────────────


def _write_audit(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")


def _posttool_edit_record(
    *, ts_ns: int, trace_id: str, file_path: str,
    old_string: str, new_string: str,
) -> dict[str, Any]:
    """Synthesize the PostToolUse audit record an Edit would write —
    `inserted_string_hashes` lives in the PostToolUse explain block
    (that's where `to_audit_dict` puts it). detect_backtrack walks
    PostToolUse records to find priors to compare against."""
    return {
        "ts_ns": ts_ns,
        "tool": "Edit",
        "aid": "test-session",
        "hook": "PostToolUse",
        "status": "success",
        "trace_id": trace_id,
        "explain": {
            "post_analysis": {
                "file_path": file_path,
                "inserted_string_hashes": (
                    inserted_string_hashes_for_audit(
                        tool_name="Edit",
                        tool_input={"old_string": old_string, "new_string": new_string},
                    )
                ),
            },
        },
    }


class TestDetectBacktrack:
    def test_revert_detected(self, tmp_path: Path) -> None:
        audit = tmp_path / "audit.jsonl"
        # First Edit: replace "foo" → "bar" in /a/b.py
        _write_audit(audit, [
            _posttool_edit_record(
                ts_ns=1, trace_id="trace-1",
                file_path="/a/b.py",
                old_string="foo", new_string="bar",
            ),
        ])
        # Second Edit on same file: replace "bar" → "foo" — this is a revert.
        evidence = detect_backtrack(
            tool_name="Edit",
            tool_input={
                "file_path": "/a/b.py",
                "old_string": "bar",
                "new_string": "foo",
            },
            audit_path=audit,
        )
        assert evidence is not None
        assert evidence.reverted_trace_id == "trace-1"
        assert evidence.file_path == "/a/b.py"

    def test_no_revert_when_strings_dont_match(self, tmp_path: Path) -> None:
        audit = tmp_path / "audit.jsonl"
        _write_audit(audit, [
            _posttool_edit_record(
                ts_ns=1, trace_id="trace-1",
                file_path="/a/b.py",
                old_string="foo", new_string="bar",
            ),
        ])
        # Different second Edit — not a revert.
        evidence = detect_backtrack(
            tool_name="Edit",
            tool_input={
                "file_path": "/a/b.py",
                "old_string": "baz",
                "new_string": "qux",
            },
            audit_path=audit,
        )
        assert evidence is None

    def test_no_revert_on_different_file(self, tmp_path: Path) -> None:
        audit = tmp_path / "audit.jsonl"
        _write_audit(audit, [
            _posttool_edit_record(
                ts_ns=1, trace_id="trace-1",
                file_path="/a/b.py",
                old_string="foo", new_string="bar",
            ),
        ])
        evidence = detect_backtrack(
            tool_name="Edit",
            tool_input={
                "file_path": "/different.py",   # different file
                "old_string": "bar",
                "new_string": "foo",
            },
            audit_path=audit,
        )
        assert evidence is None

    def test_no_revert_for_non_edit_tool(self, tmp_path: Path) -> None:
        audit = tmp_path / "audit.jsonl"
        evidence = detect_backtrack(
            tool_name="Bash",
            tool_input={"command": "ls"},
            audit_path=audit,
        )
        assert evidence is None

    def test_missing_audit_returns_none(self, tmp_path: Path) -> None:
        evidence = detect_backtrack(
            tool_name="Edit",
            tool_input={"file_path": "/x", "old_string": "a", "new_string": "b"},
            audit_path=tmp_path / "missing.jsonl",
        )
        assert evidence is None


# ─────────────────────────────────────────────────────────────────────
# 4. detect_redundant_call
# ─────────────────────────────────────────────────────────────────────


class TestDetectRedundant:
    def test_same_args_detected(self, tmp_path: Path) -> None:
        audit = tmp_path / "audit.jsonl"
        # Prior PostToolUse with same args. detect_redundant_call now
        # walks PostToolUse records (where args_hash actually lands).
        import hashlib
        args = {"command": "ls -la /tmp"}
        args_hash = hashlib.sha3_256(
            json.dumps(args, sort_keys=True).encode()
        ).hexdigest()[:16]
        _write_audit(audit, [{
            "ts_ns": 1, "tool": "Bash", "aid": "s",
            "hook": "PostToolUse", "status": "success",
            "trace_id": "first-trace",
            "explain": {"post_analysis": {"args_hash": args_hash}},
        }])
        # Same call now → should detect redundancy.
        result = detect_redundant_call(
            tool_name="Bash",
            tool_input=args,
            audit_path=audit,
        )
        assert result == "first-trace"

    def test_different_args_no_match(self, tmp_path: Path) -> None:
        audit = tmp_path / "audit.jsonl"
        import hashlib
        args1 = {"command": "ls -la /tmp"}
        args1_hash = hashlib.sha3_256(
            json.dumps(args1, sort_keys=True).encode()
        ).hexdigest()[:16]
        _write_audit(audit, [{
            "ts_ns": 1, "tool": "Bash", "aid": "s",
            "hook": "PostToolUse", "status": "success",
            "trace_id": "trace-1",
            "explain": {"post_analysis": {"args_hash": args1_hash}},
        }])
        result = detect_redundant_call(
            tool_name="Bash",
            tool_input={"command": "ls -la /var"},   # different
            audit_path=audit,
        )
        assert result is None


# ─────────────────────────────────────────────────────────────────────
# 5. compute_duration_ms (against a real synth intent_log)
# ─────────────────────────────────────────────────────────────────────


class TestComputeDuration:
    def test_real_intent_log_lookup(self, tmp_path: Path) -> None:
        from aegis.atmu import IntentLog
        path = tmp_path / "intent.sqlite"
        log = IntentLog(str(path))
        try:
            past = time.time_ns() - 250_000_000   # 250ms ago
            log.append_tentative(
                aid="a", tenant_id="t", trace_id="tr", span_id="sp",
                parent_span_id=None,
                tool_name="Edit", tool_args_hash="0" * 64,
                blast_radius=2, atv_commitment="atv-x",
                cost_profile="software",
                record_id="my-record-id",
            )
            # Manually backdate the row for deterministic duration.
            log.conn.execute(
                "UPDATE intent_log SET created_at_ns=? WHERE record_id=?",
                (past, "my-record-id"),
            )
            log.conn.commit()
        finally:
            log.close()

        d = compute_duration_ms("my-record-id", path)
        assert d is not None
        # 250ms ago means duration is ≥ 200ms (some slack for test runtime).
        assert 200.0 < d < 5_000.0

    def test_missing_record(self, tmp_path: Path) -> None:
        from aegis.atmu import IntentLog
        path = tmp_path / "intent.sqlite"
        IntentLog(str(path)).close()
        d = compute_duration_ms("never-set", path)
        assert d is None

    def test_missing_db_returns_none(self, tmp_path: Path) -> None:
        d = compute_duration_ms("anything", tmp_path / "absent.sqlite")
        assert d is None


# ─────────────────────────────────────────────────────────────────────
# 6. analyse_post_tool_event — bundled
# ─────────────────────────────────────────────────────────────────────


class TestAnalyseFull:
    def test_simple_bash_response(self, tmp_path: Path) -> None:
        analysis = analyse_post_tool_event(
            tool_name="Bash",
            tool_input={"command": "ls"},
            tool_response={"stdout": "file1\nfile2"},
            exit_code=0,
            audit_path=tmp_path / "audit.jsonl",   # missing OK
            intent_log_path=tmp_path / "intent.sqlite",
        )
        assert analysis.classification.size_bytes == len(b"file1\nfile2")
        assert analysis.classification.line_count == 2
        assert analysis.backtrack is None
        assert analysis.redundant_of is None
        assert analysis.duration_ms is None    # no intent_log

    def test_edit_revert_detected(self, tmp_path: Path) -> None:
        audit = tmp_path / "audit.jsonl"
        _write_audit(audit, [
            _posttool_edit_record(
                ts_ns=1, trace_id="tr-1",
                file_path="/a.py",
                old_string="foo", new_string="bar",
            ),
        ])
        analysis = analyse_post_tool_event(
            tool_name="Edit",
            tool_input={"file_path": "/a.py", "old_string": "bar", "new_string": "foo"},
            tool_response={"success": True},
            exit_code=0,
            audit_path=audit,
            intent_log_path=tmp_path / "intent.sqlite",
        )
        assert analysis.backtrack is not None
        assert analysis.backtrack.reverted_trace_id == "tr-1"


# ─────────────────────────────────────────────────────────────────────
# 7. to_audit_dict — the wire-format that lands in audit.jsonl
# ─────────────────────────────────────────────────────────────────────


class TestAuditDict:
    def test_includes_classification_and_args_hash(self) -> None:
        analysis = PostAnalysis(
            classification=ResponseClassification(
                size_bytes=10, line_count=1, is_empty=False,
            ),
        )
        out = to_audit_dict(
            analysis,
            tool_name="Bash",
            tool_input={"command": "ls"},
        )
        assert "classification" in out
        assert out["classification"]["size_bytes"] == 10
        assert "args_hash" in out
        assert len(out["args_hash"]) == 16

    def test_edit_carries_inserted_hashes(self) -> None:
        analysis = PostAnalysis()
        out = to_audit_dict(
            analysis,
            tool_name="Edit",
            tool_input={
                "file_path": "/x.py",
                "old_string": "old",
                "new_string": "new",
            },
        )
        assert "inserted_string_hashes" in out
        assert len(out["inserted_string_hashes"]) == 1
        assert out["file_path"] == "/x.py"

    def test_backtrack_evidence_serialised(self) -> None:
        analysis = PostAnalysis(
            backtrack=BacktrackEvidence(
                reverted_trace_id="tr-x",
                file_path="/p",
                matched_string_hash="abcd",
            ),
        )
        out = to_audit_dict(
            analysis, tool_name="Edit",
            tool_input={"old_string": "a", "new_string": "b"},
        )
        assert out["backtrack"]["reverted_trace_id"] == "tr-x"


# ─────────────────────────────────────────────────────────────────────
# 8. End-to-end — drive post_tool.py with full analysis
# ─────────────────────────────────────────────────────────────────────


def _run_post_hook(payload: dict[str, Any]) -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools" / "hooks"))
    import post_tool
    return post_tool.handle_posttool(
        io.StringIO(json.dumps(payload)), io.StringIO(),
    )


@pytest.fixture
def isolated_paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Redirect both audit + intent log into tmp_path."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools" / "hooks"))
    import post_tool

    audit = tmp_path / "audit.jsonl"
    intent_db = tmp_path / "intent.sqlite"
    monkeypatch.setattr(post_tool, "LOCAL_AUDIT_PATH", audit)
    monkeypatch.setattr(post_tool, "LOCAL_INTENT_LOG_PATH", intent_db)
    monkeypatch.setattr(post_tool, "ATMU_DISABLED", True)   # skip ATMU for this E2E
    return audit


class TestE2EHookCarriesAnalysis:
    def test_audit_record_has_post_analysis_block(
        self, isolated_paths: Path
    ) -> None:
        audit = isolated_paths
        rc = _run_post_hook({
            "hook_event_name": "PostToolUse",
            "session_id": "s",
            "invocation_id": "inv-1",
            "tool_name": "Bash",
            "tool_input": {"command": "ls -la /tmp"},
            "tool_response": {"stdout": "file1\nfile2\nfile3"},
            "exit_code": 0,
        })
        assert rc == 0
        assert audit.is_file()
        rec = json.loads(audit.read_text().strip().splitlines()[-1])
        assert rec["hook"] == "PostToolUse"
        # The new explain.post_analysis block is the headline of this PR.
        post = rec["explain"]["post_analysis"]
        assert post["classification"]["size_bytes"] == len(
            b"file1\nfile2\nfile3"
        )
        assert post["classification"]["line_count"] == 3
        assert post["classification"]["is_error"] is False
        assert "args_hash" in post
        # Privacy: raw stdout body is NOT in the record.
        assert "file1" not in rec["explain"].get("preview", "")

    def test_audit_does_not_contain_raw_response_body(
        self, isolated_paths: Path
    ) -> None:
        audit = isolated_paths
        secret = "SUPER_SECRET_TOKEN_xyz"
        _run_post_hook({
            "hook_event_name": "PostToolUse",
            "session_id": "s",
            "invocation_id": "inv-2",
            "tool_name": "Bash",
            "tool_input": {"command": "echo $SECRET"},
            "tool_response": {"stdout": secret},
            "exit_code": 0,
        })
        # The raw body must NOT appear in the audit chain.
        body = audit.read_text()
        assert secret not in body, (
            f"audit chain must not store raw tool_response body; "
            f"found {secret!r}"
        )

    def test_edit_revert_caught_in_audit(self, isolated_paths: Path) -> None:
        audit = isolated_paths
        # First Edit: foo → bar.
        _run_post_hook({
            "hook_event_name": "PostToolUse",
            "session_id": "s",
            "invocation_id": "inv-edit-1",
            "tool_name": "Edit",
            "tool_input": {
                "file_path": "/x.py",
                "old_string": "foo",
                "new_string": "bar",
            },
            "tool_response": {"success": True},
            "exit_code": 0,
        })
        # Second Edit reverts: bar → foo.
        _run_post_hook({
            "hook_event_name": "PostToolUse",
            "session_id": "s",
            "invocation_id": "inv-edit-2",
            "tool_name": "Edit",
            "tool_input": {
                "file_path": "/x.py",
                "old_string": "bar",
                "new_string": "foo",
            },
            "tool_response": {"success": True},
            "exit_code": 0,
        })
        last = json.loads(audit.read_text().strip().splitlines()[-1])
        post = last["explain"]["post_analysis"]
        # Note: backtrack detection looks for matching `inserted_string_hashes`
        # in PRIOR records — the first call's PostToolUse audit record
        # carries those hashes, so the second call's analysis finds
        # the revert.
        assert "backtrack" in post
        assert post["backtrack"]["file_path"] == "/x.py"
