"""Unit tests for src/aegis/instruction_baseline/ (v2.2.1, Day-1 #3)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aegis.instruction_baseline import (
    DEFAULT_INSTRUCTION_PATHS,
    InstructionBaseline,
    diff_baseline,
    hash_file,
    load_baseline,
    snapshot,
    write_baseline,
)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """Build a fake repo with a CLAUDE.md, AGENTS.md, .mcp.json."""
    (tmp_path / "CLAUDE.md").write_text(
        "# Project rules\n- Use Python 3.11.\n- Type hints required.\n"
    )
    (tmp_path / "AGENTS.md").write_text("Codex agent: prefer pytest.\n")
    (tmp_path / ".mcp.json").write_text(json.dumps({"servers": []}))
    skills = tmp_path / ".claude" / "skills"
    skills.mkdir(parents=True)
    (skills / "alpha.md").write_text("Skill: alpha — does X.\n")
    return tmp_path


# ---- hash_file ----------------------------------------------------------


def test_hash_file_deterministic(tmp_path: Path) -> None:
    p = tmp_path / "f.txt"
    p.write_text("hello")
    a = hash_file(p)
    b = hash_file(p)
    assert a == b
    assert len(a) == 64


def test_hash_file_changes_with_content(tmp_path: Path) -> None:
    p = tmp_path / "f.txt"
    p.write_text("v1")
    h1 = hash_file(p)
    p.write_text("v2")
    h2 = hash_file(p)
    assert h1 != h2


# ---- snapshot -----------------------------------------------------------


def test_snapshot_captures_canonical_files(repo: Path) -> None:
    bl = snapshot(repo)
    assert "CLAUDE.md" in bl.files
    assert "AGENTS.md" in bl.files
    assert ".mcp.json" in bl.files
    assert ".claude/skills/alpha.md" in bl.files


def test_snapshot_skips_missing_files(tmp_path: Path) -> None:
    (tmp_path / "CLAUDE.md").write_text("just one")
    bl = snapshot(tmp_path)
    assert list(bl.files.keys()) == ["CLAUDE.md"]


def test_snapshot_uses_default_patterns_constant() -> None:
    """Sanity check: documented surface includes these well-known files."""
    assert "CLAUDE.md" in DEFAULT_INSTRUCTION_PATHS
    assert "AGENTS.md" in DEFAULT_INSTRUCTION_PATHS
    assert ".mcp.json" in DEFAULT_INSTRUCTION_PATHS


def test_snapshot_glob_matches(repo: Path) -> None:
    skills = repo / ".claude" / "skills"
    (skills / "beta.md").write_text("Skill: beta.\n")
    bl = snapshot(repo)
    keys = set(bl.files.keys())
    assert ".claude/skills/alpha.md" in keys
    assert ".claude/skills/beta.md" in keys


# ---- diff_baseline ------------------------------------------------------


def test_diff_no_drift(repo: Path) -> None:
    bl = snapshot(repo)
    report = diff_baseline(bl, repo)
    assert report.is_clean is True
    assert report.summary() == "no drift"


def test_diff_detects_modification(repo: Path) -> None:
    bl = snapshot(repo)
    (repo / "CLAUDE.md").write_text("# poisoned content")
    report = diff_baseline(bl, repo)
    assert report.is_clean is False
    paths = [m[0] for m in report.modified]
    assert "CLAUDE.md" in paths
    # baseline_hash != current_hash
    _, old, new = report.modified[0]
    assert old != new


def test_diff_detects_addition(repo: Path) -> None:
    bl = snapshot(repo)
    # Add a new tracked file (matches DEFAULT_INSTRUCTION_PATHS)
    (repo / ".claude" / "skills" / "new.md").write_text("Skill: new.\n")
    report = diff_baseline(bl, repo)
    assert ".claude/skills/new.md" in report.added


def test_diff_detects_removal(repo: Path) -> None:
    bl = snapshot(repo)
    (repo / "AGENTS.md").unlink()
    report = diff_baseline(bl, repo)
    assert "AGENTS.md" in report.removed


def test_diff_summary_aggregates(repo: Path) -> None:
    bl = snapshot(repo)
    (repo / "CLAUDE.md").write_text("changed")
    (repo / "AGENTS.md").unlink()
    (repo / ".claude" / "commands").mkdir(parents=True)
    (repo / ".claude" / "commands" / "x.md").write_text("new")
    s = diff_baseline(bl, repo).summary()
    assert "+1 added" in s
    assert "-1 removed" in s
    assert "~1 modified" in s


def test_whitespace_only_drift_still_caught(repo: Path) -> None:
    """Poisoning sometimes hides in trailing-whitespace-only edits.
    Plain SHA3 over file bytes flags that, by design.
    """
    bl = snapshot(repo)
    original = (repo / "CLAUDE.md").read_text()
    (repo / "CLAUDE.md").write_text(original + "  \n")  # trailing whitespace
    report = diff_baseline(bl, repo)
    assert not report.is_clean
    assert any(m[0] == "CLAUDE.md" for m in report.modified)


# ---- write_baseline / load_baseline ------------------------------------


def test_roundtrip_baseline(repo: Path, tmp_path: Path) -> None:
    bl = snapshot(repo)
    out = tmp_path / "manifest.json"
    write_baseline(bl, out)
    loaded = load_baseline(out)
    assert loaded.files == bl.files
    assert loaded.root == bl.root


def test_load_baseline_missing_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_baseline(tmp_path / "absent.json")


def test_baseline_dataclass_immutable(repo: Path) -> None:
    from dataclasses import FrozenInstanceError

    bl = snapshot(repo)
    with pytest.raises(FrozenInstanceError):
        bl.version = 2  # type: ignore[misc]


def test_baseline_from_dict_handles_missing_fields(tmp_path: Path) -> None:
    """Loading a manifest with only the required minimum still works."""
    out = tmp_path / "m.json"
    out.write_text(json.dumps({"files": {}, "root": "/x"}))
    loaded = load_baseline(out)
    assert loaded.version == 1
    assert loaded.files == {}


def test_baseline_from_dict_rejects_non_dict_files() -> None:
    with pytest.raises(ValueError):
        InstructionBaseline.from_dict({"files": "not a dict"})


def test_write_baseline_creates_parent(tmp_path: Path) -> None:
    bl = InstructionBaseline(version=1, created_at_ns=0, root="/x", files={})
    out = tmp_path / "deep" / "nested" / "m.json"
    write_baseline(bl, out)
    assert out.exists()
