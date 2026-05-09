"""Unit tests for PR-E: model-weight hash baseline.

Three surfaces under test:

1. ``snapshot()`` / ``diff_baseline()`` — extending the existing
   instruction-baseline machinery to also track GGUF / safetensors
   model artifacts.
2. ``InstructionBaseline.{to_dict, from_dict}`` — round-trip with the
   new ``model_weights`` dict, AND backward-compat with v0.2.0
   manifests that don't have it.
3. ``step309`` — error message differentiates ``model_weight_drift:``
   from ``instruction_drift:`` so the operator response is clear.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest

from aegis.firewall.core import FirewallContext
from aegis.firewall.step309_instruction_drift import (
    reset_baseline_cache,
)
from aegis.firewall.step309_instruction_drift import (
    run as step309_run,
)
from aegis.instruction_baseline.manifest import (
    DEFAULT_MODEL_WEIGHT_PATTERNS,
    DriftReport,
    InstructionBaseline,
    diff_baseline,
    snapshot,
    write_baseline,
)
from aegis.schema import ATVHeader, ATVInput

# ── snapshot() with model weights ───────────────────────────────────


def _make_repo(tmp_path: Path) -> Path:
    """Build a tiny fake repo with one CLAUDE.md plus 2 mock GGUF
    files in models/."""
    (tmp_path / "CLAUDE.md").write_text("# Project rules\n")
    models = tmp_path / "models"
    models.mkdir()
    (models / "llama-3.1-8b-q4.gguf").write_bytes(b"FAKE_GGUF_HEADER\x00" * 100)
    (models / "embedding.safetensors").write_bytes(b"FAKE_ST\x01" * 50)
    return tmp_path


def test_snapshot_skips_model_weights_by_default(tmp_path: Path) -> None:
    """v0.2.0 callers must see no behaviour change — model_weights
    stays empty unless explicitly opted in."""
    root = _make_repo(tmp_path)
    bl = snapshot(root)
    assert "CLAUDE.md" in bl.files
    assert bl.model_weights == {}


def test_snapshot_with_default_patterns_picks_up_models(
    tmp_path: Path,
) -> None:
    root = _make_repo(tmp_path)
    bl = snapshot(
        root, model_weight_patterns=DEFAULT_MODEL_WEIGHT_PATTERNS,
    )
    assert "CLAUDE.md" in bl.files
    assert "models/llama-3.1-8b-q4.gguf" in bl.model_weights
    assert "models/embedding.safetensors" in bl.model_weights
    # Each weight file gets a real SHA3 hash (64 hex chars).
    for h in bl.model_weights.values():
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)


def test_snapshot_with_explicit_patterns(tmp_path: Path) -> None:
    """Custom pattern tuple — only matches the user's globs."""
    root = _make_repo(tmp_path)
    bl = snapshot(
        root,
        model_weight_patterns=("models/*.gguf",),  # only GGUF, not ST
    )
    assert "models/llama-3.1-8b-q4.gguf" in bl.model_weights
    assert "models/embedding.safetensors" not in bl.model_weights


# ── to_dict / from_dict round-trip + backward compat ───────────────


def test_baseline_dict_round_trip_with_weights(tmp_path: Path) -> None:
    bl = InstructionBaseline(
        version=1, created_at_ns=0, root="/x",
        files={"CLAUDE.md": "abc"},
        model_weights={"models/foo.gguf": "deadbeef"},
    )
    out = bl.to_dict()
    assert out["model_weights"] == {"models/foo.gguf": "deadbeef"}
    bl2 = InstructionBaseline.from_dict(out)
    assert bl2.model_weights == bl.model_weights


def test_baseline_to_dict_omits_empty_model_weights() -> None:
    """Tidy manifest: empty dict shouldn't pollute the JSON
    representation. Existing v0.2.0 manifests round-trip byte-for-byte."""
    bl = InstructionBaseline(
        version=1, created_at_ns=0, root="/x",
        files={"CLAUDE.md": "abc"},
    )
    assert "model_weights" not in bl.to_dict()


def test_baseline_from_dict_legacy_manifest_no_weights() -> None:
    """Old manifests (no model_weights key) must still load — defaults
    to empty dict."""
    legacy = {
        "version": 1,
        "created_at_ns": 0,
        "root": "/x",
        "files": {"CLAUDE.md": "abc"},
    }
    bl = InstructionBaseline.from_dict(legacy)
    assert bl.files == {"CLAUDE.md": "abc"}
    assert bl.model_weights == {}


def test_baseline_from_dict_rejects_non_dict_weights() -> None:
    bad = {
        "version": 1,
        "created_at_ns": 0,
        "root": "/x",
        "files": {},
        "model_weights": ["not a dict"],
    }
    with pytest.raises(ValueError, match="model_weights"):
        InstructionBaseline.from_dict(bad)


# ── DriftReport: model + instruction drift independent ─────────────


def test_drift_report_clean_with_no_drift() -> None:
    r = DriftReport()
    assert r.is_clean is True
    assert r.has_model_drift is False
    assert r.has_instruction_drift is False
    assert r.summary() == "no drift"


def test_drift_report_instruction_only() -> None:
    r = DriftReport(modified=[("CLAUDE.md", "old", "new")])
    assert not r.is_clean
    assert r.has_instruction_drift is True
    assert r.has_model_drift is False
    assert "model weights" not in r.summary()


def test_drift_report_model_only() -> None:
    r = DriftReport(modified_weights=[("models/foo.gguf", "a", "b")])
    assert not r.is_clean
    assert r.has_model_drift is True
    assert r.has_instruction_drift is False
    assert "model weights" in r.summary()


def test_drift_report_both_categories_summary_shows_both() -> None:
    r = DriftReport(
        added=["AGENTS.md"],
        modified_weights=[("models/foo.gguf", "a", "b")],
    )
    s = r.summary()
    assert "+1 added" in s
    assert "model weights" in s
    assert "~1 modified" in s


# ── diff_baseline() detects model drift ────────────────────────────


def test_diff_detects_model_weight_modification(tmp_path: Path) -> None:
    root = _make_repo(tmp_path)
    bl = snapshot(
        root, model_weight_patterns=DEFAULT_MODEL_WEIGHT_PATTERNS,
    )
    # Mutate a model file (silent quantization swap simulation)
    (root / "models" / "llama-3.1-8b-q4.gguf").write_bytes(
        b"DIFFERENT_GGUF_PAYLOAD\x00" * 100,
    )
    report = diff_baseline(
        bl, root,
        model_weight_patterns=DEFAULT_MODEL_WEIGHT_PATTERNS,
    )
    assert report.has_model_drift is True
    assert any(
        m[0] == "models/llama-3.1-8b-q4.gguf"
        for m in report.modified_weights
    )


def test_diff_detects_model_weight_removal(tmp_path: Path) -> None:
    root = _make_repo(tmp_path)
    bl = snapshot(
        root, model_weight_patterns=DEFAULT_MODEL_WEIGHT_PATTERNS,
    )
    (root / "models" / "embedding.safetensors").unlink()
    report = diff_baseline(
        bl, root,
        model_weight_patterns=DEFAULT_MODEL_WEIGHT_PATTERNS,
    )
    assert "models/embedding.safetensors" in report.removed_weights


def test_diff_no_model_drift_when_baseline_is_v0_2_0_legacy(
    tmp_path: Path,
) -> None:
    """A pre-PR-E baseline (no model_weights tracked) should NEVER
    produce model-drift reports — even if model files exist on disk."""
    root = _make_repo(tmp_path)
    bl_legacy = snapshot(root)  # no model_weight_patterns → empty
    assert bl_legacy.model_weights == {}
    # Caller doesn't pass weight patterns to diff either.
    report = diff_baseline(bl_legacy, root)
    assert report.has_model_drift is False


# ── step309 reason text differentiates the two categories ──────────


def _make_atv_input(tmp_path: Path) -> ATVInput:
    return ATVInput(
        header=ATVHeader(
            trace_id="t", span_id="s", tenant_id="x", aid="y",
            timestamp_ns=0,
        ),
        tool_name="Bash",
        tool_args_json="{}",
    )


def test_step309_reason_says_model_weight_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ONLY model weights changed, the reason field must
    surface 'model_weight_drift' (not 'instruction_drift') so the
    operator's playbook is clear."""
    root = _make_repo(tmp_path)
    bl = snapshot(
        root, model_weight_patterns=DEFAULT_MODEL_WEIGHT_PATTERNS,
    )
    baseline_path = tmp_path / "baseline.json"
    write_baseline(bl, baseline_path)

    # Tamper a model weight.
    (root / "models" / "llama-3.1-8b-q4.gguf").write_bytes(b"TAMPERED")

    # Wire step309 to read our baseline.
    from aegis.firewall import step309_instruction_drift as step309_mod

    reset_baseline_cache()
    monkeypatch.setattr(
        step309_mod.settings, "aegis_instruction_baseline_path",
        str(baseline_path),
    )
    monkeypatch.setattr(
        step309_mod.settings, "aegis_instruction_root", str(root),
    )

    inp = _make_atv_input(tmp_path)
    atv = np.zeros(2080, dtype=np.float32)
    ctx = FirewallContext()
    result = step309_run(atv, inp, ctx)
    assert result.verdict == "BLOCK"
    assert "model_weight_drift" in result.reason
    assert "reattest --include-model-weights" in result.reason


def test_step309_reason_says_instruction_drift_when_only_files_changed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ONLY instruction files changed, the reason stays
    'instruction_drift' — backward-compat with v0.2.0 reason text
    (no surprise for callers parsing it)."""
    root = _make_repo(tmp_path)
    bl = snapshot(
        root, model_weight_patterns=DEFAULT_MODEL_WEIGHT_PATTERNS,
    )
    baseline_path = tmp_path / "baseline.json"
    write_baseline(bl, baseline_path)

    # Tamper an instruction file (NOT a model weight).
    (root / "CLAUDE.md").write_text("# Project rules\n# tampered line\n")

    from aegis.firewall import step309_instruction_drift as step309_mod

    reset_baseline_cache()
    monkeypatch.setattr(
        step309_mod.settings, "aegis_instruction_baseline_path",
        str(baseline_path),
    )
    monkeypatch.setattr(
        step309_mod.settings, "aegis_instruction_root", str(root),
    )

    inp = _make_atv_input(tmp_path)
    atv = np.zeros(2080, dtype=np.float32)
    ctx = FirewallContext()
    result = step309_run(atv, inp, ctx)
    assert result.verdict == "BLOCK"
    assert "instruction_drift" in result.reason
    assert "model_weight_drift" not in result.reason


def test_step309_reason_prefers_model_drift_when_both(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When BOTH categories drift simultaneously, the higher-severity
    model-weight signal wins the reason field. step309's job is to
    BLOCK and route the operator's attention; the secondary
    instruction drift surfaces in the explain block + status output."""
    root = _make_repo(tmp_path)
    bl = snapshot(
        root, model_weight_patterns=DEFAULT_MODEL_WEIGHT_PATTERNS,
    )
    baseline_path = tmp_path / "baseline.json"
    write_baseline(bl, baseline_path)

    # Tamper BOTH.
    (root / "CLAUDE.md").write_text("# tampered")
    (root / "models" / "llama-3.1-8b-q4.gguf").write_bytes(b"TAMPERED")

    from aegis.firewall import step309_instruction_drift as step309_mod

    reset_baseline_cache()
    monkeypatch.setattr(
        step309_mod.settings, "aegis_instruction_baseline_path",
        str(baseline_path),
    )
    monkeypatch.setattr(
        step309_mod.settings, "aegis_instruction_root", str(root),
    )

    inp = _make_atv_input(tmp_path)
    atv = np.zeros(2080, dtype=np.float32)
    ctx = FirewallContext()
    result = step309_run(atv, inp, ctx)
    assert result.verdict == "BLOCK"
    assert "model_weight_drift" in result.reason


# ── CLI parser smoke ────────────────────────────────────────────────


def test_baseline_cli_include_model_weights_arg() -> None:
    from tools import aegis_cli
    parser = aegis_cli.build_parser()
    args = parser.parse_args(["baseline", "init", "--include-model-weights"])
    assert args.include_model_weights is True
    args = parser.parse_args(["baseline", "init"])
    assert args.include_model_weights is False


def test_baseline_cli_model_weight_paths_arg() -> None:
    from tools import aegis_cli
    parser = aegis_cli.build_parser()
    args = parser.parse_args([
        "baseline", "reattest",
        "--model-weight-paths", "/opt/llama/*.gguf", "vendor/*.bin",
    ])
    assert args.model_weight_paths == ["/opt/llama/*.gguf", "vendor/*.bin"]


# Suppress unused-import check on MagicMock — kept for future end-to-end test.
_ = MagicMock
