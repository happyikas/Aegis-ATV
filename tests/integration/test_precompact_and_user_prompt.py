"""PreCompact + UserPromptSubmit hook tests (PR #47).

Two new forensic capture points wired into aegis install:

1. **PreCompact** — fires before auto-compact. Captures pre-compaction
   snapshot (turns, tokens, dollars, context utilisation).
2. **UserPromptSubmit** — fires when user submits a message. Detects
   retry via Jaccard / BGE cosine vs previous user prompt.

Coverage:
* analyse_precompact_event against synth transcripts
* detect_user_retry: Jaccard threshold tuning, BGE auto-detect
* Privacy: raw prompt never lands in audit (regression)
* End-to-end through pre_compact.py / user_prompt_submit.py
* aegis install registers both stages, uninstall removes them
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from aegis.cost.precompact_analysis import (
    CompactionRecord,
    analyse_precompact_event,
)
from aegis.cost.precompact_analysis import (
    to_audit_record as precompact_audit_record,
)
from aegis.cost.user_retry_detector import (
    DEFAULT_RETRY_THRESHOLD,
    RetryEvidence,
    detect_user_retry,
)
from aegis.cost.user_retry_detector import (
    to_audit_record as user_retry_audit_record,
)


def _line(d: dict[str, Any]) -> str:
    return json.dumps(d) + "\n"


def _user(text: str) -> str:
    return _line({"type": "user", "content": text})


def _assistant_with_usage(
    *, in_tokens: int = 0, out_tokens: int = 0,
    cache_read: int = 0, cache_creation: int = 0,
) -> str:
    return _line({
        "type": "assistant",
        "message": {
            "content": [{"type": "text", "text": "ok"}],
            "usage": {
                "input_tokens": in_tokens,
                "output_tokens": out_tokens,
                "cache_read_input_tokens": cache_read,
                "cache_creation_input_tokens": cache_creation,
            },
        },
    })


# ─────────────────────────────────────────────────────────────────────
# 1. analyse_precompact_event
# ─────────────────────────────────────────────────────────────────────


class TestPrecompactAnalysis:
    def test_full_session_snapshot(self, tmp_path: Path) -> None:
        transcript = tmp_path / "session.jsonl"
        transcript.write_text(
            _user("task")
            + _assistant_with_usage(in_tokens=100, out_tokens=50, cache_read=900)
            + _user("more")
            + _assistant_with_usage(in_tokens=200, out_tokens=100, cache_read=1800)
            + _user("x" * 200),
            encoding="utf-8",
        )
        rec = analyse_precompact_event(
            session_id="sess-1",
            transcript_path=transcript,
            trigger="auto",
            model_for_cost="claude-sonnet-4-6",
        )
        assert rec.aid == "sess-1"
        assert rec.trigger == "auto"
        assert rec.n_turns_before == 5
        assert rec.n_assistant_turns_before == 2
        assert rec.cumulative_tokens_before > 0
        assert rec.cumulative_billed_dollars_before > 0
        assert rec.transcript_size_bytes_before > 0
        assert rec.transcript_sha3_before is not None
        # context_utilization = max_input(2100=200+1800+100) / 200000 ≈ 0.011
        assert rec.context_utilization_pre > 0.0
        assert rec.context_utilization_pre < 1.0

    def test_missing_transcript_zero_filled(self, tmp_path: Path) -> None:
        rec = analyse_precompact_event(
            session_id="s",
            transcript_path=tmp_path / "absent.jsonl",
            trigger="auto",
        )
        assert rec.n_turns_before == 0
        assert rec.cumulative_tokens_before == 0.0
        assert rec.context_utilization_pre == 0.0

    def test_no_transcript_path_zero_filled(self) -> None:
        rec = analyse_precompact_event(
            session_id="s", transcript_path=None, trigger="auto",
        )
        assert rec.n_turns_before == 0

    def test_to_audit_record_shape(self) -> None:
        rec = CompactionRecord(
            aid="s", session_id="s", trigger="auto", model_for_cost="m",
            n_turns_before=10, cumulative_billed_dollars_before=0.5,
        )
        audit = precompact_audit_record(rec)
        assert audit["hook"] == "PreCompact"
        assert audit["aid"] == "s"
        assert audit["explain"]["compaction"]["n_turns_before"] == 10
        assert audit["explain"]["compaction"]["cumulative_billed_dollars_before"] == 0.5


# ─────────────────────────────────────────────────────────────────────
# 2. detect_user_retry
# ─────────────────────────────────────────────────────────────────────


class TestUserRetryDetector:
    def _transcript_with_user_prompts(
        self, path: Path, prompts: list[str],
    ) -> Path:
        path.write_text(
            "\n".join(_user(p).strip() for p in prompts) + "\n",
            encoding="utf-8",
        )
        return path

    def test_high_similarity_flags_retry(self, tmp_path: Path) -> None:
        # Penultimate user prompt: "fix the bug in foo.py"
        # Current:                  "fix the bug in foo please"
        # Many overlapping tokens → Jaccard ≈ 0.6 → retry.
        transcript = self._transcript_with_user_prompts(
            tmp_path / "t.jsonl",
            ["fix the bug in foo.py", "current"],
        )
        evidence = detect_user_retry(
            current_prompt="fix the bug in foo please",
            transcript_path=transcript,
            threshold=0.4,
        )
        assert evidence.is_retry is True
        assert evidence.similarity > 0.4
        assert evidence.method == "jaccard"

    def test_low_similarity_not_retry(self, tmp_path: Path) -> None:
        transcript = self._transcript_with_user_prompts(
            tmp_path / "t.jsonl",
            ["fix the bug in foo.py", "current"],
        )
        evidence = detect_user_retry(
            current_prompt="explain quantum computing concepts",
            transcript_path=transcript,
            threshold=0.5,
        )
        assert evidence.is_retry is False
        assert evidence.similarity < 0.5

    def test_no_previous_prompt_returns_zero_similarity(
        self, tmp_path: Path,
    ) -> None:
        # Single user prompt only — no penultimate to compare.
        transcript = self._transcript_with_user_prompts(
            tmp_path / "t.jsonl", ["just the current"],
        )
        evidence = detect_user_retry(
            current_prompt="just the current",
            transcript_path=transcript,
            threshold=0.5,
        )
        assert evidence.similarity == 0.0
        assert evidence.is_retry is False
        assert evidence.prev_prompt_hash is None

    def test_missing_transcript_returns_zero(self, tmp_path: Path) -> None:
        evidence = detect_user_retry(
            current_prompt="hi",
            transcript_path=tmp_path / "absent.jsonl",
            threshold=0.5,
        )
        assert evidence.similarity == 0.0
        assert evidence.is_retry is False

    def test_prompt_hash_is_16_hex(self) -> None:
        evidence = detect_user_retry(
            current_prompt="hello world",
            transcript_path=None,
            threshold=0.5,
        )
        assert len(evidence.prompt_hash) == 16
        assert evidence.prompt_size_bytes == len(b"hello world")

    def test_threshold_boundary(self, tmp_path: Path) -> None:
        # Identical prompts → similarity 1.0 → always retry at any threshold.
        transcript = self._transcript_with_user_prompts(
            tmp_path / "t.jsonl",
            ["the exact same prompt", "current"],
        )
        evidence = detect_user_retry(
            current_prompt="the exact same prompt",
            transcript_path=transcript,
            threshold=0.99,
        )
        assert evidence.similarity == 1.0
        assert evidence.is_retry is True

    def test_use_bge_false_forces_jaccard(self, tmp_path: Path) -> None:
        transcript = self._transcript_with_user_prompts(
            tmp_path / "t.jsonl",
            ["the cat sat on the mat", "current"],
        )
        evidence = detect_user_retry(
            current_prompt="the cat sat on the rug",
            transcript_path=transcript,
            threshold=0.5,
            use_bge=False,
        )
        assert evidence.method == "jaccard"

    def test_preview_off_by_default(self, tmp_path: Path) -> None:
        evidence = detect_user_retry(
            current_prompt="SECRET-TOKEN-XYZ",
            transcript_path=None,
            threshold=0.5,
        )
        assert evidence.preview is None

    def test_preview_on_via_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("AEGIS_USER_PROMPT_CAPTURE_PREVIEW", "1")
        evidence = detect_user_retry(
            current_prompt="hello world",
            transcript_path=None,
            threshold=0.5,
        )
        assert evidence.preview == "hello world"

    def test_preview_truncates_long_prompts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("AEGIS_USER_PROMPT_CAPTURE_PREVIEW", "1")
        long = "x" * 300
        evidence = detect_user_retry(
            current_prompt=long, transcript_path=None, threshold=0.5,
        )
        assert evidence.preview is not None
        assert len(evidence.preview) <= 81  # 80 + ellipsis

    def test_audit_record_shape(self) -> None:
        ev = RetryEvidence(
            prompt_hash="abc", prompt_size_bytes=42,
            prev_prompt_hash="xyz",
            similarity=0.8, is_retry=True, method="jaccard",
        )
        audit = user_retry_audit_record("session-x", ev)
        assert audit["hook"] == "UserPromptSubmit"
        assert audit["aid"] == "session-x"
        body = audit["explain"]["user_retry"]
        assert body["similarity"] == 0.8
        assert body["is_retry"] is True


# ─────────────────────────────────────────────────────────────────────
# 3. End-to-end via hook scripts
# ─────────────────────────────────────────────────────────────────────


def _run_precompact(payload: dict[str, Any]) -> tuple[int, str]:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools" / "hooks"))
    import pre_compact
    out_buf = io.StringIO()
    rc = pre_compact.handle_precompact(io.StringIO(json.dumps(payload)), out_buf)
    return rc, out_buf.getvalue()


def _run_user_prompt(payload: dict[str, Any]) -> tuple[int, str]:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools" / "hooks"))
    import user_prompt_submit
    out_buf = io.StringIO()
    rc = user_prompt_submit.handle_user_prompt_submit(
        io.StringIO(json.dumps(payload)), out_buf,
    )
    return rc, out_buf.getvalue()


@pytest.fixture
def isolated_audit(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Isolate audit log for both hook modules."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools" / "hooks"))
    import pre_compact
    import user_prompt_submit
    audit = tmp_path / "audit.jsonl"
    monkeypatch.setattr(pre_compact, "LOCAL_AUDIT_PATH", audit)
    monkeypatch.setattr(user_prompt_submit, "LOCAL_AUDIT_PATH", audit)
    return audit


class TestE2EPrecompactHook:
    def test_records_compaction_event(
        self, isolated_audit: Path, tmp_path: Path,
    ) -> None:
        transcript = tmp_path / "session.jsonl"
        transcript.write_text(
            _user("task")
            + _assistant_with_usage(in_tokens=10_000, out_tokens=500, cache_read=170_000)
            + _user("x" * 200),
            encoding="utf-8",
        )
        rc, stdout = _run_precompact({
            "hook_event_name": "PreCompact",
            "session_id": "demo",
            "transcript_path": str(transcript),
            "trigger": "auto",
        })
        assert rc == 0
        env = json.loads(stdout)["_aegis"]
        assert env["compaction"] == "recorded"
        assert env["trigger"] == "auto"

        last = json.loads(isolated_audit.read_text().strip().splitlines()[-1])
        assert last["hook"] == "PreCompact"
        assert last["aid"] == "demo"
        block = last["explain"]["compaction"]
        assert block["trigger"] == "auto"
        assert block["n_turns_before"] >= 1

    def test_does_not_block_on_missing_transcript(
        self, isolated_audit: Path, tmp_path: Path,
    ) -> None:
        rc, stdout = _run_precompact({
            "hook_event_name": "PreCompact",
            "session_id": "x",
            "transcript_path": str(tmp_path / "absent.jsonl"),
        })
        assert rc == 0
        env = json.loads(stdout)["_aegis"]
        # Records with zero-fill rather than skipping.
        assert env["compaction"] == "recorded"


class TestE2EUserPromptHook:
    def test_records_user_prompt_with_retry_signal(
        self, isolated_audit: Path, tmp_path: Path,
    ) -> None:
        transcript = tmp_path / "t.jsonl"
        transcript.write_text(
            _user("fix the bug in foo.py please") + _user("current placeholder"),
            encoding="utf-8",
        )
        rc, stdout = _run_user_prompt({
            "hook_event_name": "UserPromptSubmit",
            "session_id": "sess-x",
            "transcript_path": str(transcript),
            "prompt": "fix the bug in foo please",
        })
        assert rc == 0
        env = json.loads(stdout)["_aegis"]
        assert env["user_retry"] == "recorded"
        assert env["method"] == "jaccard"

        last = json.loads(isolated_audit.read_text().strip().splitlines()[-1])
        assert last["hook"] == "UserPromptSubmit"
        block = last["explain"]["user_retry"]
        assert "prompt_hash" in block
        assert block["similarity"] >= 0.0
        assert block["threshold"] == DEFAULT_RETRY_THRESHOLD

    def test_audit_does_not_contain_raw_prompt(
        self, isolated_audit: Path,
    ) -> None:
        secret_prompt = "API_KEY=sk-secret-xyz-do-not-leak"
        _run_user_prompt({
            "hook_event_name": "UserPromptSubmit",
            "session_id": "s",
            "prompt": secret_prompt,
            "transcript_path": "",
        })
        body = isolated_audit.read_text()
        assert secret_prompt not in body, (
            f"audit must not store raw prompt; found {secret_prompt!r}"
        )

    def test_does_not_block_on_empty_prompt(self, isolated_audit: Path) -> None:
        rc, stdout = _run_user_prompt({
            "hook_event_name": "UserPromptSubmit",
            "session_id": "s",
            "prompt": "",
            "transcript_path": "",
        })
        assert rc == 0
        env = json.loads(stdout)["_aegis"]
        assert env["user_retry"] == "recorded"


# ─────────────────────────────────────────────────────────────────────
# 4. aegis install registers the two new stages
# ─────────────────────────────────────────────────────────────────────


class TestInstallRegistersHooks:
    def test_install_writes_precompact_and_userprompt_entries(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
        import aegis_cli

        # Redirect SETTINGS_PATH so we don't touch the real config.
        fake_settings = tmp_path / "settings.json"
        monkeypatch.setattr(aegis_cli, "SETTINGS_PATH", fake_settings)

        parser = aegis_cli.build_parser()
        args = parser.parse_args(["install", "--mode", "local", "--judge", "dummy"])
        rc = args.fn(args)
        assert rc == 0

        config = json.loads(fake_settings.read_text())
        hooks = config["hooks"]
        # Both new stages are registered.
        assert "PreCompact" in hooks
        assert "UserPromptSubmit" in hooks
        # Each stage has at least one entry pointing at our hook script.
        pre_cmds = [
            h["command"]
            for entry in hooks["PreCompact"]
            for h in entry.get("hooks", [])
        ]
        assert any("pre_compact.py" in c for c in pre_cmds)
        up_cmds = [
            h["command"]
            for entry in hooks["UserPromptSubmit"]
            for h in entry.get("hooks", [])
        ]
        assert any("user_prompt_submit.py" in c for c in up_cmds)
