"""ATMU 2PC integration tests for plugin (--mode local) hooks.

Verifies that:

* PreToolUse (``tools/aegis_local_hook.py``) opens a TENTATIVE intent
  before the firewall runs and transitions it to PREPARED+COMMITTED
  (ALLOW), PREPARED only (REQUIRE_APPROVAL), or ABORTED (BLOCK).
* PostToolUse (``tools/hooks/post_tool.py``) finds the same record by
  recomputing the deterministic record_id from ``invocation_id`` and
  attaches the tool_outcome.
* Disabling ATMU via ``AEGIS_ATMU_DISABLE=1`` leaves the firewall +
  audit-chain path completely unaffected.
* IntentLog init failure does NOT block the tool call.
"""

from __future__ import annotations

import hashlib
import importlib
import io
import json
import sys
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[2]
for sub in ("tools", "tools/hooks"):
    p = str(_ROOT / sub)
    if p not in sys.path:
        sys.path.insert(0, p)

import aegis_local_hook  # noqa: E402,I001
import post_tool  # noqa: E402,I001  (tools/hooks/post_tool.py)
from aegis.atmu import IntentLog, TxState  # noqa: E402


def _span_id_for(invocation_id: str) -> str:
    h = hashlib.sha3_256(invocation_id.encode()).hexdigest()
    return h[32:48]


@pytest.fixture
def atmu_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Path]:
    """Redirect both audit + intent-log paths into tmp_path AND reset
    the per-process IntentLog singleton so each test sees a fresh DB."""
    audit = tmp_path / "audit.jsonl"
    intent_db = tmp_path / "intent_log.sqlite"

    monkeypatch.setattr(aegis_local_hook, "LOCAL_AUDIT_PATH", audit)
    monkeypatch.setattr(aegis_local_hook, "LOCAL_INTENT_LOG_PATH", intent_db)
    monkeypatch.setattr(aegis_local_hook, "ATMU_DISABLED", False)
    monkeypatch.setattr(aegis_local_hook, "_INTENT_LOG_SINGLETON", None)

    monkeypatch.setattr(post_tool, "LOCAL_AUDIT_PATH", audit)
    monkeypatch.setattr(post_tool, "LOCAL_INTENT_LOG_PATH", intent_db)
    monkeypatch.setattr(post_tool, "ATMU_DISABLED", False)

    return {"audit": audit, "intent_db": intent_db}


def _pre(payload: dict[str, Any]) -> int:
    return aegis_local_hook.handle_pretool(
        io.StringIO(json.dumps(payload)), io.StringIO()
    )


def _post(payload: dict[str, Any]) -> int:
    return post_tool.handle_posttool(
        io.StringIO(json.dumps(payload)), io.StringIO()
    )


# ─────────────────────────────────────────────────────────────────────
# PreToolUse: 2PC phase 1 → records intent + transitions on verdict
# ─────────────────────────────────────────────────────────────────────
class TestPreToolUseATMU:
    def test_allow_path_drives_committed(self, atmu_env: dict[str, Path]) -> None:
        invocation_id = "inv-allow-001"
        rc = _pre(
            {
                "hook_event_name": "PreToolUse",
                "session_id": "sess-x",
                "invocation_id": invocation_id,
                "tool_name": "Bash",
                "tool_input": {"command": "ls -la"},
            }
        )
        assert rc == 0  # ALLOW

        record_id = _span_id_for(invocation_id)
        log = IntentLog(str(atmu_env["intent_db"]))
        try:
            rec = log.get(record_id)
            assert rec is not None
            assert rec["current_state"] == TxState.COMMITTED.value
            assert rec["tool_name"] == "Bash"
            states = [h["state"] for h in rec["state_history"]]
            assert states == ["tentative", "prepared", "committed"]
        finally:
            log.close()

    def test_block_path_drives_aborted(self, atmu_env: dict[str, Path]) -> None:
        invocation_id = "inv-block-002"
        rc = _pre(
            {
                "hook_event_name": "PreToolUse",
                "session_id": "sess-x",
                "invocation_id": invocation_id,
                "tool_name": "Bash",
                "tool_input": {"command": "rm -rf /"},
            }
        )
        # rm -rf / triggers BLOCK in step311 donor rules → exit 2
        assert rc == 2

        record_id = _span_id_for(invocation_id)
        log = IntentLog(str(atmu_env["intent_db"]))
        try:
            rec = log.get(record_id)
            assert rec is not None
            assert rec["current_state"] == TxState.ABORTED.value
            states = [h["state"] for h in rec["state_history"]]
            assert states == ["tentative", "aborted"]
        finally:
            log.close()

    def test_audit_record_carries_intent_record_id(
        self, atmu_env: dict[str, Path]
    ) -> None:
        invocation_id = "inv-audit-003"
        _pre(
            {
                "hook_event_name": "PreToolUse",
                "session_id": "sess-x",
                "invocation_id": invocation_id,
                "tool_name": "Read",
                "tool_input": {"file_path": "/etc/hosts"},
            }
        )
        line = atmu_env["audit"].read_text().strip().splitlines()[-1]
        rec = json.loads(line)
        assert rec["explain"]["intent_record_id"] == _span_id_for(invocation_id)


# ─────────────────────────────────────────────────────────────────────
# PostToolUse: 2PC phase 2 → attaches tool_outcome
# ─────────────────────────────────────────────────────────────────────
class TestPostToolUseATMU:
    def test_post_attaches_outcome(self, atmu_env: dict[str, Path]) -> None:
        invocation_id = "inv-post-010"
        _pre(
            {
                "hook_event_name": "PreToolUse",
                "session_id": "sess-x",
                "invocation_id": invocation_id,
                "tool_name": "Bash",
                "tool_input": {"command": "echo hi"},
            }
        )
        # PreToolUse closes its IntentLog connection only at process exit;
        # in the test we drop the singleton so PostToolUse opens its own.
        aegis_local_hook._INTENT_LOG_SINGLETON = None  # noqa: SLF001

        rc = _post(
            {
                "hook_event_name": "PostToolUse",
                "session_id": "sess-x",
                "invocation_id": invocation_id,
                "tool_name": "Bash",
                "tool_input": {"command": "echo hi"},
                "tool_response": {"stdout": "hi\n"},
                "exit_code": 0,
            }
        )
        assert rc == 0

        record_id = _span_id_for(invocation_id)
        log = IntentLog(str(atmu_env["intent_db"]))
        try:
            rec = log.get(record_id)
            assert rec is not None
            assert rec["tool_outcome"] is not None
            assert rec["tool_outcome"]["status"] == "success"
            assert (
                rec["tool_outcome"]["result_hash"]
                == hashlib.sha3_256(
                    json.dumps({"stdout": "hi\n"}, sort_keys=True).encode()
                ).hexdigest()
            )
        finally:
            log.close()

    def test_post_failure_outcome(self, atmu_env: dict[str, Path]) -> None:
        invocation_id = "inv-post-fail-011"
        _pre(
            {
                "hook_event_name": "PreToolUse",
                "session_id": "sess-x",
                "invocation_id": invocation_id,
                "tool_name": "Read",
                "tool_input": {"file_path": "/tmp/notes.txt"},
            }
        )
        aegis_local_hook._INTENT_LOG_SINGLETON = None  # noqa: SLF001

        _post(
            {
                "hook_event_name": "PostToolUse",
                "session_id": "sess-x",
                "invocation_id": invocation_id,
                "tool_name": "Read",
                "tool_input": {"file_path": "/tmp/notes.txt"},
                "tool_response": {"is_error": True, "error": "not found"},
                "exit_code": 1,
            }
        )

        log = IntentLog(str(atmu_env["intent_db"]))
        try:
            rec = log.get(_span_id_for(invocation_id))
            assert rec is not None
            assert rec["tool_outcome"]["status"] == "failure"
        finally:
            log.close()

    def test_post_unknown_invocation_silent_noop(
        self, atmu_env: dict[str, Path]
    ) -> None:
        # No PreToolUse first — Post should silently skip rather than crash.
        rc = _post(
            {
                "hook_event_name": "PostToolUse",
                "session_id": "sess-x",
                "invocation_id": "inv-orphan-999",
                "tool_name": "Bash",
                "tool_input": {"command": "true"},
                "tool_response": {"stdout": ""},
                "exit_code": 0,
            }
        )
        assert rc == 0  # no crash; just a no-op


# ─────────────────────────────────────────────────────────────────────
# Disable / failure paths — must NEVER block the tool
# ─────────────────────────────────────────────────────────────────────
class TestATMUDisabled:
    def test_atmu_disabled_skips_intent_recording(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        audit = tmp_path / "audit.jsonl"
        intent_db = tmp_path / "intent_log.sqlite"
        monkeypatch.setattr(aegis_local_hook, "LOCAL_AUDIT_PATH", audit)
        monkeypatch.setattr(aegis_local_hook, "LOCAL_INTENT_LOG_PATH", intent_db)
        monkeypatch.setattr(aegis_local_hook, "ATMU_DISABLED", True)
        monkeypatch.setattr(aegis_local_hook, "_INTENT_LOG_SINGLETON", None)

        rc = _pre(
            {
                "hook_event_name": "PreToolUse",
                "session_id": "sess-x",
                "invocation_id": "inv-disabled-020",
                "tool_name": "Bash",
                "tool_input": {"command": "ls"},
            }
        )
        assert rc == 0
        # Audit chain still receives the record …
        assert audit.exists() and audit.read_text().strip()
        # … but no IntentLog DB was created.
        assert not intent_db.exists()

    def test_atmu_init_failure_does_not_block(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        audit = tmp_path / "audit.jsonl"
        # Point intent log at an *unwritable* path (parent is a file, not dir).
        bad_parent = tmp_path / "nope"
        bad_parent.write_text("file-not-dir")
        intent_db = bad_parent / "intent.sqlite"
        monkeypatch.setattr(aegis_local_hook, "LOCAL_AUDIT_PATH", audit)
        monkeypatch.setattr(aegis_local_hook, "LOCAL_INTENT_LOG_PATH", intent_db)
        monkeypatch.setattr(aegis_local_hook, "ATMU_DISABLED", False)
        monkeypatch.setattr(aegis_local_hook, "_INTENT_LOG_SINGLETON", None)

        rc = _pre(
            {
                "hook_event_name": "PreToolUse",
                "session_id": "sess-x",
                "invocation_id": "inv-fail-init-021",
                "tool_name": "Bash",
                "tool_input": {"command": "ls"},
            }
        )
        assert rc == 0
        assert audit.exists()


# Force fresh singleton at module load to avoid contamination if the same
# pytest worker runs the older test_aegis_local_hook.py first.
def _reset_singleton() -> None:
    importlib.reload(aegis_local_hook)


_reset_singleton()
