"""Tests for ``aegis.burnin.labels`` — the human adjudication overlay
on top of the shadow corpus.

Covers:

* canonicalisation between patent vocab (benign/suspicious/malicious)
  and firewall vocab (ALLOW/REQUIRE_APPROVAL/BLOCK)
* persistence round-trip via ``append_label`` / ``read_labels``
* supersede semantics (later ``ts_ns`` wins for the same ``trace_id``)
* validation (empty trace_id, unknown label, confidence out of [0,1])
* malformed line tolerance in ``read_labels``
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from aegis.burnin import labels as _labels
from aegis.burnin.labels import (
    LABEL_TO_VERDICT,
    SCHEMA_VERSION,
    VERDICT_TO_LABEL,
    LabelError,
    LabelRecord,
    append_label,
    canonicalise,
    labels_by_trace,
    latest_label_for,
    read_labels,
)

# ── canonicalise ──────────────────────────────────────────────────


def test_canonicalise_from_label() -> None:
    lab, ver = canonicalise(label="suspicious")
    assert lab == "suspicious"
    assert ver == "REQUIRE_APPROVAL"


def test_canonicalise_from_verdict() -> None:
    lab, ver = canonicalise(verdict="BLOCK")
    assert lab == "malicious"
    assert ver == "BLOCK"


def test_canonicalise_label_case_insensitive() -> None:
    lab, ver = canonicalise(label="  MALICIOUS  ")
    assert lab == "malicious"
    assert ver == "BLOCK"


def test_canonicalise_verdict_case_insensitive() -> None:
    lab, ver = canonicalise(verdict="allow")
    assert lab == "benign"
    assert ver == "ALLOW"


def test_canonicalise_rejects_both_none() -> None:
    with pytest.raises(LabelError, match="exactly one"):
        canonicalise()


def test_canonicalise_rejects_both_set() -> None:
    with pytest.raises(LabelError, match="exactly one"):
        canonicalise(label="benign", verdict="ALLOW")


def test_canonicalise_unknown_label() -> None:
    with pytest.raises(LabelError, match="unknown label"):
        canonicalise(label="totally-bogus")


def test_canonicalise_unknown_verdict() -> None:
    with pytest.raises(LabelError, match="unknown verdict"):
        canonicalise(verdict="UNKNOWN")


def test_label_verdict_classes_are_in_bijection() -> None:
    """Every label maps to exactly one verdict, and vice versa, with
    no orphans. This is the patent-vs-firewall vocab contract."""
    for label, verdict in LABEL_TO_VERDICT.items():
        assert VERDICT_TO_LABEL[verdict] == label
    assert len(LABEL_TO_VERDICT) == len(VERDICT_TO_LABEL) == 3


# ── append_label ──────────────────────────────────────────────────


def test_append_label_round_trip(tmp_path: Path) -> None:
    p = tmp_path / "labels.jsonl"
    rec = append_label(
        trace_id="abc-123",
        label="suspicious",
        reason="looks odd",
        analyst="alice",
        confidence=0.85,
        path=p,
    )
    assert rec.trace_id == "abc-123"
    assert rec.label == "suspicious"
    assert rec.verdict == "REQUIRE_APPROVAL"  # canonicalised
    assert rec.reason == "looks odd"
    assert rec.analyst == "alice"
    assert rec.confidence == pytest.approx(0.85)
    assert rec.schema_version == SCHEMA_VERSION

    # File contents are the same record
    recs = read_labels(path=p)
    assert len(recs) == 1
    assert recs[0] == rec


def test_append_label_accepts_verdict_vocabulary(tmp_path: Path) -> None:
    p = tmp_path / "labels.jsonl"
    rec = append_label(trace_id="t1", verdict="BLOCK", path=p)
    assert rec.label == "malicious"  # canonicalised back
    assert rec.verdict == "BLOCK"


def test_append_label_requires_trace_id(tmp_path: Path) -> None:
    p = tmp_path / "labels.jsonl"
    with pytest.raises(LabelError, match="trace_id is required"):
        append_label(trace_id="   ", label="benign", path=p)


def test_append_label_rejects_both_label_and_verdict(tmp_path: Path) -> None:
    p = tmp_path / "labels.jsonl"
    with pytest.raises(LabelError, match="exactly one"):
        append_label(
            trace_id="t1", label="benign", verdict="ALLOW", path=p,
        )


def test_append_label_rejects_neither(tmp_path: Path) -> None:
    p = tmp_path / "labels.jsonl"
    with pytest.raises(LabelError, match="exactly one"):
        append_label(trace_id="t1", path=p)


def test_append_label_rejects_bad_confidence(tmp_path: Path) -> None:
    p = tmp_path / "labels.jsonl"
    with pytest.raises(LabelError, match="confidence must be in"):
        append_label(
            trace_id="t1", label="benign", confidence=1.5, path=p,
        )
    with pytest.raises(LabelError, match="confidence must be in"):
        append_label(
            trace_id="t1", label="benign", confidence=-0.01, path=p,
        )


def test_append_label_truncates_long_reason(tmp_path: Path) -> None:
    p = tmp_path / "labels.jsonl"
    rec = append_label(
        trace_id="t1",
        label="benign",
        reason="x" * 1000,
        path=p,
    )
    assert len(rec.reason) == _labels.MAX_REASON_LEN


def test_append_label_persists_jsonl_format(tmp_path: Path) -> None:
    p = tmp_path / "labels.jsonl"
    append_label(trace_id="t1", label="benign", path=p)
    raw = p.read_text(encoding="utf-8")
    # One newline-terminated JSON object
    assert raw.endswith("\n")
    obj = json.loads(raw.strip())
    # Stable key set
    expected_keys = {
        "ts_ns", "trace_id", "invocation_id", "label", "verdict",
        "reason", "analyst", "confidence", "schema_version",
    }
    assert set(obj.keys()) == expected_keys


def test_append_label_creates_parent_directory(tmp_path: Path) -> None:
    p = tmp_path / "deep" / "nested" / "labels.jsonl"
    append_label(trace_id="t1", label="benign", path=p)
    assert p.exists()


# ── multiple records + supersede ─────────────────────────────────


def test_multiple_traces_round_trip(tmp_path: Path) -> None:
    p = tmp_path / "labels.jsonl"
    append_label(trace_id="t1", label="benign", path=p)
    append_label(trace_id="t2", label="suspicious", path=p)
    append_label(trace_id="t3", label="malicious", path=p)
    recs = read_labels(path=p)
    assert len(recs) == 3
    assert [r.trace_id for r in recs] == ["t1", "t2", "t3"]
    assert [r.label for r in recs] == ["benign", "suspicious", "malicious"]


def test_supersede_latest_ts_wins(tmp_path: Path) -> None:
    p = tmp_path / "labels.jsonl"
    # Same trace, two labels with strictly increasing ts_ns
    t0 = 1_000_000_000_000_000_000  # arbitrary epoch ns
    append_label(
        trace_id="t1", label="benign", ts_ns=t0, path=p,
        analyst="bob",
    )
    append_label(
        trace_id="t1", label="malicious", ts_ns=t0 + 100, path=p,
        analyst="alice",
    )
    latest = latest_label_for("t1", path=p)
    assert latest is not None
    assert latest.label == "malicious"
    assert latest.analyst == "alice"


def test_supersede_in_labels_by_trace(tmp_path: Path) -> None:
    p = tmp_path / "labels.jsonl"
    t0 = 1_000_000_000_000_000_000
    append_label(trace_id="t1", label="benign", ts_ns=t0, path=p)
    append_label(trace_id="t1", label="suspicious", ts_ns=t0 + 50, path=p)
    append_label(trace_id="t2", label="malicious", ts_ns=t0 + 30, path=p)
    by_trace = labels_by_trace(path=p)
    assert set(by_trace.keys()) == {"t1", "t2"}
    assert by_trace["t1"].label == "suspicious"  # latest of two
    assert by_trace["t2"].label == "malicious"


def test_latest_label_returns_none_for_unknown_trace(tmp_path: Path) -> None:
    p = tmp_path / "labels.jsonl"
    append_label(trace_id="t1", label="benign", path=p)
    assert latest_label_for("does-not-exist", path=p) is None


# ── reader robustness ────────────────────────────────────────────


def test_read_labels_missing_file_returns_empty(tmp_path: Path) -> None:
    assert read_labels(path=tmp_path / "absent.jsonl") == []


def test_read_labels_skips_blank_lines(tmp_path: Path) -> None:
    p = tmp_path / "labels.jsonl"
    p.write_text(
        '\n'
        + json.dumps({
            "ts_ns": 1,
            "trace_id": "t1",
            "label": "benign",
            "verdict": "ALLOW",
        }) + "\n"
        + "   \n"
        + json.dumps({
            "ts_ns": 2,
            "trace_id": "t2",
            "label": "malicious",
            "verdict": "BLOCK",
        }) + "\n",
        encoding="utf-8",
    )
    recs = read_labels(path=p)
    assert len(recs) == 2


def test_read_labels_skips_malformed_json(tmp_path: Path) -> None:
    p = tmp_path / "labels.jsonl"
    p.write_text(
        json.dumps({
            "ts_ns": 1, "trace_id": "ok", "label": "benign",
            "verdict": "ALLOW",
        }) + "\n"
        + "{ not valid json\n"
        + json.dumps({
            "ts_ns": 2, "trace_id": "also-ok", "label": "malicious",
            "verdict": "BLOCK",
        }) + "\n",
        encoding="utf-8",
    )
    recs = read_labels(path=p)
    assert len(recs) == 2
    assert [r.trace_id for r in recs] == ["ok", "also-ok"]


def test_read_labels_skips_records_missing_required_keys(
    tmp_path: Path,
) -> None:
    p = tmp_path / "labels.jsonl"
    p.write_text(
        # Missing 'label' / 'verdict' — must skip without raising
        json.dumps({"ts_ns": 1, "trace_id": "broken"}) + "\n"
        + json.dumps({
            "ts_ns": 2, "trace_id": "ok", "label": "benign",
            "verdict": "ALLOW",
        }) + "\n",
        encoding="utf-8",
    )
    recs = read_labels(path=p)
    assert len(recs) == 1
    assert recs[0].trace_id == "ok"


# ── env override ─────────────────────────────────────────────────


def test_labels_path_respects_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    custom = tmp_path / "custom-labels.jsonl"
    monkeypatch.setenv("AEGIS_LABELS_PATH", str(custom))
    assert _labels.labels_path() == custom


def test_labels_path_default_when_env_blank(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AEGIS_LABELS_PATH", "")
    p = _labels.labels_path()
    assert p.name == "labels.jsonl"
    assert p.parent.name == ".aegis"


# ── record (dataclass) ───────────────────────────────────────────


def test_label_record_to_from_dict_round_trip() -> None:
    rec = LabelRecord(
        ts_ns=time.time_ns(),
        trace_id="t1",
        invocation_id="inv-1",
        label="suspicious",
        verdict="REQUIRE_APPROVAL",
        reason="hmm",
        analyst="cli",
        confidence=0.7,
        schema_version=SCHEMA_VERSION,
    )
    d = rec.to_dict()
    rec2 = LabelRecord.from_dict(d)
    assert rec == rec2


def test_label_record_from_dict_tolerates_missing_optional_fields() -> None:
    """from_dict should accept minimal records (no invocation_id /
    reason / analyst / confidence) — supports forward compatibility
    with leaner future writers."""
    rec = LabelRecord.from_dict({
        "ts_ns": 12345,
        "trace_id": "t1",
        "label": "benign",
        "verdict": "ALLOW",
    })
    assert rec.invocation_id == ""
    assert rec.reason == ""
    assert rec.analyst == "cli"
    assert rec.confidence == 1.0
    assert rec.schema_version == SCHEMA_VERSION
