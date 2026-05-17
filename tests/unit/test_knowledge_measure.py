"""Tests for v0.5.18 — ``measure_context`` diagnostic helper.

Three concerns:

1. **Pure-function correctness** — given a known wiki, the
   metrics dataclass surfaces exactly the right counts.
2. **Hot-path safety** — same defensive contract as
   ``knowledge_context_for_advisor``: missing wiki / unknown aid /
   empty aid → ``None``, never raises.
3. **Demo reproducibility** — the v0.5.18 demo script runs to
   completion and produces a valid JSON payload via ``--json``.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from aegis.knowledge import (
    EntryKind,
    InfoBox,
    KnowledgeEntry,
    clear_advisor_cache,
    measure_context,
    save_entry,
    save_index,
)


def _populate(tmp_path: Path) -> None:
    """One agent + one tool + one pattern, with the agent
    cross-referencing both."""
    agent = KnowledgeEntry(
        entry_id="agent/foo",
        kind=EntryKind.AGENT,
        title="Agent foo",
        summary="Test agent.",
        infobox=InfoBox(fields={
            "n_calls": 200, "n_block": 4, "total_cost_usd": 0.0125,
        }),
        related=("tool/Bash", "pattern/Bash:loop:Bash"),
        tags=("high-volume", "unstable"),
        n_observations=200,
        confidence=1.0,
    )
    tool = KnowledgeEntry(
        entry_id="tool/Bash",
        kind=EntryKind.TOOL,
        title="Tool Bash",
        summary="bash",
        infobox=InfoBox(fields={"n_calls": 400}),
        n_observations=400,
        tags=("high-volume",),
    )
    pat = KnowledgeEntry(
        entry_id="pattern/Bash:loop:Bash",
        kind=EntryKind.PATTERN,
        title="Pattern loop:Bash",
        summary="loop",
        infobox=InfoBox(fields={"n_fires": 35}),
        n_observations=35,
        tags=("frequent",),
    )
    for e in (agent, tool, pat):
        save_entry(e, root=tmp_path)
    save_index(
        [agent, tool, pat], root=tmp_path,
        built_at_ns=1, built_from_records=1,
    )


# ──────────────────────────────────────────────────────────────────
# 1. Correctness
# ──────────────────────────────────────────────────────────────────


class TestMeasureCorrectness:
    def setup_method(self) -> None:
        clear_advisor_cache()

    def test_aggregates_across_entries(self, tmp_path: Path) -> None:
        _populate(tmp_path)
        m = measure_context("foo", root=tmp_path)
        assert m is not None
        assert m.aid == "foo"
        assert m.n_entries == 3            # agent + tool + pattern
        assert m.n_agent_entries == 1
        assert m.n_tool_entries == 1
        assert m.n_pattern_entries == 1
        # 3 + 1 + 1 = 5 infobox fields across the three entries.
        assert m.n_infobox_fields == 5
        # Agent has 2 cross-refs; tool + pattern have 0.
        assert m.n_cross_refs == 2
        # Tags: agent(2) + tool(1) + pattern(1) = 4
        assert m.n_tags == 4
        # 200 + 400 + 35 = 635 observations total.
        assert m.n_observations == 635
        assert m.markdown_chars > 0
        assert m.estimated_tokens == m.markdown_chars // 4
        assert m.has_agent_entry is True


# ──────────────────────────────────────────────────────────────────
# 2. Hot-path safety
# ──────────────────────────────────────────────────────────────────


class TestMeasureSafety:
    def setup_method(self) -> None:
        clear_advisor_cache()

    def test_empty_aid_returns_none(self, tmp_path: Path) -> None:
        assert measure_context("", root=tmp_path) is None
        assert measure_context(None, root=tmp_path) is None

    def test_missing_wiki_returns_none(self, tmp_path: Path) -> None:
        # tmp_path empty — no entries
        assert measure_context("foo", root=tmp_path) is None

    def test_unknown_aid_returns_none(self, tmp_path: Path) -> None:
        _populate(tmp_path)
        assert measure_context("nobody", root=tmp_path) is None

    def test_corrupted_dir_returns_none(self, tmp_path: Path) -> None:
        # Doesn't exist — measure should swallow and return None.
        bogus = tmp_path / "does-not-exist"
        assert measure_context("foo", root=bogus) is None


# ──────────────────────────────────────────────────────────────────
# 3. Demo reproducibility
# ──────────────────────────────────────────────────────────────────


class TestDemoRuns:
    def test_demo_emits_valid_json(self) -> None:
        """The v0.5.18 demo runs to completion and ``--json``
        produces a parseable payload with the expected structure.

        Subprocess invocation rather than a direct import so the
        demo's main() truly walks the public ``aegis`` API and
        is exercised the same way an operator runs it."""
        repo_root = Path(__file__).resolve().parents[2]
        script = repo_root / "demo" / "wiki_grounded_advisor.py"
        assert script.exists(), f"demo script missing: {script}"
        result = subprocess.run(
            [sys.executable, str(script), "--json"],
            capture_output=True,
            text=True,
            cwd=str(repo_root),
            timeout=60,
            check=False,
        )
        assert result.returncode == 0, (
            f"demo exited {result.returncode}:\n"
            f"stdout: {result.stdout[-500:]!r}\n"
            f"stderr: {result.stderr[-500:]!r}"
        )
        payload = json.loads(result.stdout)
        assert "agents" in payload
        assert "n_records_synthesized" in payload
        assert "n_entries_built" in payload
        assert payload["n_entries_built"] > 0
        # Every synthetic agent should be present.
        for aid in (
            "clean-coder", "high-cost", "unstable",
            "frequent-approvals", "sparse",
        ):
            assert aid in payload["agents"], (
                f"missing aid {aid!r} in demo output"
            )
            agent_payload = payload["agents"][aid]
            assert agent_payload["prompt_with_wiki_chars"] > \
                agent_payload["prompt_no_wiki_chars"], (
                f"{aid}: wiki prompt should be longer than baseline"
            )
