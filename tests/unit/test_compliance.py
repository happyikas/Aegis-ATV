"""Unit tests for src/aegis/compliance/* (v4.3, Claim 57)."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from aegis.atmu.intent_log import IntentLog
from aegis.audit.encrypted_journal import EncryptedJournal, load_or_create_data_key
from aegis.audit.sqlite_store import AuditDB
from aegis.compliance import (
    AVAILABLE_FRAMEWORKS,
    ComplianceFramework,
    ComplianceReport,
    EvidenceCollector,
    get_framework,
)
from aegis.compliance.evidence import _deterministic_sample
from aegis.compliance.frameworks import (
    EU_AI_ACT,
    HIPAA,
    ISO_42001,
    SOC2,
)
from aegis.cost.ledger import CostAttestationLedger
from aegis.sign.ed25519 import sign_atv
from aegis.sign.merkle import GENESIS_HASH, record_hash

# ─────────────────────────────────────────────────────────────────────
# Frameworks registry
# ─────────────────────────────────────────────────────────────────────


def test_all_frameworks_registered() -> None:
    expected = {"soc2", "eu_ai_act", "hipaa", "iso_42001"}
    assert set(AVAILABLE_FRAMEWORKS.keys()) == expected


@pytest.mark.parametrize("name,expected", [
    ("soc2", SOC2),
    ("SOC2", SOC2),  # case-insensitive
    ("eu_ai_act", EU_AI_ACT),
    ("hipaa", HIPAA),
    ("iso_42001", ISO_42001),
])
def test_get_framework_lookup(name: str, expected: ComplianceFramework) -> None:
    assert get_framework(name) is expected


def test_get_framework_unknown_raises() -> None:
    with pytest.raises(KeyError, match="unknown framework"):
        get_framework("not-a-framework")


def test_each_framework_has_controls() -> None:
    for fw in AVAILABLE_FRAMEWORKS.values():
        assert len(fw.controls) > 0, f"{fw.name} has no controls"
        # Each control has unique id
        ids = [c.id for c in fw.controls]
        assert len(ids) == len(set(ids)), f"{fw.name} has duplicate control IDs"


def test_soc2_has_critical_controls() -> None:
    """Sanity check that the SOC 2 control set covers CC6/CC7/CC8 sections."""
    ids = [c.id for c in SOC2.controls]
    assert any(i.startswith("CC6") for i in ids)
    assert any(i.startswith("CC7") for i in ids)
    assert any(i.startswith("CC8") for i in ids)


def test_eu_ai_act_has_annex_iv_controls() -> None:
    ids = [c.id for c in EU_AI_ACT.controls]
    assert any(i.startswith("ANNEX_IV") for i in ids)
    assert any(i.startswith("ART_12") for i in ids)


# ─────────────────────────────────────────────────────────────────────
# Deterministic sampler
# ─────────────────────────────────────────────────────────────────────


def test_deterministic_sample_same_seed_same_output() -> None:
    items = list(range(100))
    a = _deterministic_sample(items, seed_text="test", n=5)
    b = _deterministic_sample(items, seed_text="test", n=5)
    assert a == b


def test_deterministic_sample_different_seed_different_output() -> None:
    items = list(range(100))
    a = _deterministic_sample(items, seed_text="seed-A", n=5)
    b = _deterministic_sample(items, seed_text="seed-B", n=5)
    assert a != b


def test_deterministic_sample_n_exceeds_returns_all() -> None:
    items = [1, 2, 3]
    s = _deterministic_sample(items, seed_text="x", n=10)
    assert sorted(s) == [1, 2, 3]


def test_deterministic_sample_empty() -> None:
    assert _deterministic_sample([], seed_text="x", n=5) == []


# ─────────────────────────────────────────────────────────────────────
# Collector — fixtures + integration
# ─────────────────────────────────────────────────────────────────────


@pytest.fixture
def stores(tmp_path: Path):
    audit = AuditDB(str(tmp_path / "audit.sqlite"))
    intent = IntentLog(str(tmp_path / "intent.sqlite"))
    cost = CostAttestationLedger(
        db_path=str(tmp_path / "cost.sqlite"),
        jsonl_path=tmp_path / "cost.jsonl",
        signing_key=Ed25519PrivateKey.generate(),
    )
    journal = EncryptedJournal(
        tmp_path / "journal.jsonl",
        data_key=load_or_create_data_key(tmp_path / "journal.key"),
    )
    yield {
        "audit": audit, "intent": intent, "cost": cost, "journal": journal,
    }
    audit.close()
    cost.close()


def _seed_audit(audit: AuditDB, count: int = 3) -> None:
    sk = Ed25519PrivateKey.generate()
    prev = GENESIS_HASH
    for i in range(count):
        header = {
            "aid": "agent-A", "tenant_id": "t",
            "tool_name": "Bash", "decision": "ALLOW",
            "atv_hash": "0" * 64,
        }
        rec = sign_atv(b"\x00" * 32, header, prev, sk)
        rec["atv_id"] = f"atv-{i}"
        rec["decision"] = "ALLOW"
        rec["this_hash"] = record_hash(rec["payload"])
        audit.append(rec)
        prev = rec["this_hash"]


def _seed_intents(intent: IntentLog, count: int = 3) -> None:
    for i in range(count):
        intent.append_tentative(
            aid="agent-A", tenant_id="t",
            trace_id="t" * 32, span_id=f"s{i:015d}",
            parent_span_id=None,
            tool_name="Bash", tool_args_hash=f"h{i}", blast_radius=1,
            atv_commitment=f"c{i}",
        )


def test_collector_with_no_stores_returns_not_implemented(tmp_path: Path) -> None:
    collector = EvidenceCollector()
    report = collector.collect(SOC2, period_start_ns=0, period_end_ns=time.time_ns())
    assert isinstance(report, ComplianceReport)
    assert report.framework_name == "SOC2"
    assert all(c.coverage == "not_implemented" for c in report.controls)


def test_collector_audit_chain_evidence(stores) -> None:
    _seed_audit(stores["audit"], count=5)
    collector = EvidenceCollector(audit_db=stores["audit"])
    report = collector.collect(
        SOC2, period_start_ns=0, period_end_ns=time.time_ns(),
    )
    cc81 = next(c for c in report.controls if c.control_id == "CC8.1")
    assert cc81.coverage == "covered"
    assert cc81.evidence_type == "audit_chain"
    assert cc81.record_count == 5
    assert len(cc81.sample_record_ids) <= 5


def test_collector_intent_log_evidence(stores) -> None:
    _seed_intents(stores["intent"], count=4)
    collector = EvidenceCollector(intent_log=stores["intent"])
    # SOC2 CC7.4 uses intent_log
    report = collector.collect(SOC2, period_start_ns=0, period_end_ns=time.time_ns())
    cc74 = next(c for c in report.controls if c.control_id == "CC7.4")
    assert cc74.coverage == "covered"
    assert cc74.record_count == 4


def test_collector_period_filter_excludes_old_records(stores) -> None:
    _seed_audit(stores["audit"], count=5)
    # All records have ts ≈ time.time_ns(); query for distant past
    collector = EvidenceCollector(audit_db=stores["audit"])
    report = collector.collect(SOC2, period_start_ns=0, period_end_ns=1)
    cc81 = next(c for c in report.controls if c.control_id == "CC8.1")
    assert cc81.record_count == 0
    assert cc81.coverage == "partial"  # query type works but no records


def test_collector_summary_counts_coverage_buckets(stores) -> None:
    _seed_audit(stores["audit"], count=2)
    _seed_intents(stores["intent"], count=2)
    collector = EvidenceCollector(
        audit_db=stores["audit"], intent_log=stores["intent"],
    )
    report = collector.collect(SOC2, period_start_ns=0, period_end_ns=time.time_ns())
    assert "covered" in report.summary
    assert "not_implemented" in report.summary
    assert sum(report.summary.values()) == len(SOC2.controls)


def test_collector_invalid_period_raises(stores) -> None:
    collector = EvidenceCollector()
    with pytest.raises(ValueError, match="period_start"):
        collector.collect(SOC2, period_start_ns=100, period_end_ns=50)


# ─────────────────────────────────────────────────────────────────────
# Determinism — same input twice → same output
# ─────────────────────────────────────────────────────────────────────


def test_collector_deterministic_sample_selection(stores) -> None:
    _seed_audit(stores["audit"], count=20)
    collector = EvidenceCollector(audit_db=stores["audit"])
    end_ns = time.time_ns()  # fixed for both calls (seed depends on it)
    report1 = collector.collect(SOC2, period_start_ns=0, period_end_ns=end_ns)
    report2 = collector.collect(SOC2, period_start_ns=0, period_end_ns=end_ns)
    cc81_1 = next(c for c in report1.controls if c.control_id == "CC8.1")
    cc81_2 = next(c for c in report2.controls if c.control_id == "CC8.1")
    # Same period → same sample IDs
    assert cc81_1.sample_record_ids == cc81_2.sample_record_ids


# ─────────────────────────────────────────────────────────────────────
# Output formats
# ─────────────────────────────────────────────────────────────────────


def test_report_to_dict_serializable(stores) -> None:
    collector = EvidenceCollector(audit_db=stores["audit"])
    report = collector.collect(SOC2, period_start_ns=0, period_end_ns=time.time_ns())
    blob = json.dumps(report.to_dict())
    assert "SOC2" in blob


def test_report_to_json_round_trip(stores) -> None:
    collector = EvidenceCollector(audit_db=stores["audit"])
    report = collector.collect(SOC2, period_start_ns=0, period_end_ns=time.time_ns())
    blob = report.to_json()
    parsed = json.loads(blob)
    assert parsed["framework_name"] == "SOC2"
    assert "controls" in parsed


def test_report_to_markdown_human_readable(stores) -> None:
    collector = EvidenceCollector(audit_db=stores["audit"])
    report = collector.collect(SOC2, period_start_ns=0, period_end_ns=time.time_ns())
    md = report.to_markdown()
    assert "# SOC2 Compliance Evidence Report" in md
    assert "## Coverage Summary" in md
    assert "## Controls" in md
    # Each control rendered
    for c in SOC2.controls:
        assert c.id in md


# ─────────────────────────────────────────────────────────────────────
# All 4 frameworks end-to-end
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("framework_name", ["soc2", "eu_ai_act", "hipaa", "iso_42001"])
def test_collector_runs_all_frameworks(framework_name: str, stores) -> None:
    _seed_audit(stores["audit"], count=2)
    _seed_intents(stores["intent"], count=2)
    collector = EvidenceCollector(
        audit_db=stores["audit"],
        intent_log=stores["intent"],
        cost_ledger=stores["cost"],
        encrypted_journal=stores["journal"],
    )
    framework = get_framework(framework_name)
    report = collector.collect(framework, period_start_ns=0, period_end_ns=time.time_ns())
    assert report.framework_name == framework.name
    assert len(report.controls) == len(framework.controls)
    # No raw exceptions in any control evidence
    for c in report.controls:
        assert c.coverage in ("covered", "partial", "not_implemented")


# ─────────────────────────────────────────────────────────────────────
# HTTP endpoint
# ─────────────────────────────────────────────────────────────────────


def test_compliance_endpoint_lists_frameworks() -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from aegis.api.compliance import make_router
    app = FastAPI()
    app.include_router(make_router(
        audit_db=None, intent_log=None,
        cost_ledger=None, encrypted_journal=None,
    ))
    with TestClient(app) as client:
        r = client.get("/compliance/frameworks")
    assert r.status_code == 200
    data = r.json()
    assert len(data["frameworks"]) == 4


def test_compliance_endpoint_generate_evidence(stores) -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from aegis.api.compliance import make_router
    _seed_audit(stores["audit"], count=2)
    app = FastAPI()
    app.include_router(make_router(
        audit_db=stores["audit"], intent_log=stores["intent"],
        cost_ledger=stores["cost"], encrypted_journal=stores["journal"],
    ))
    with TestClient(app) as client:
        r = client.post("/compliance/evidence", json={
            "framework": "soc2",
            "period_start_ns": 0,
            "period_end_ns": time.time_ns(),
            "format": "json",
        })
    assert r.status_code == 200
    data = json.loads(r.text)
    assert data["framework_name"] == "SOC2"


def test_compliance_endpoint_markdown_format(stores) -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from aegis.api.compliance import make_router
    app = FastAPI()
    app.include_router(make_router(
        audit_db=stores["audit"], intent_log=None,
        cost_ledger=None, encrypted_journal=None,
    ))
    with TestClient(app) as client:
        r = client.post("/compliance/evidence", json={
            "framework": "hipaa",
            "period_start_ns": 0,
            "period_end_ns": time.time_ns(),
            "format": "markdown",
        })
    assert r.status_code == 200
    assert "# HIPAA Compliance Evidence Report" in r.text


def test_compliance_endpoint_unknown_framework_400() -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from aegis.api.compliance import make_router
    app = FastAPI()
    app.include_router(make_router(
        audit_db=None, intent_log=None,
        cost_ledger=None, encrypted_journal=None,
    ))
    with TestClient(app) as client:
        r = client.post("/compliance/evidence", json={
            "framework": "made-up",
            "period_start_ns": 0,
            "period_end_ns": 100,
        })
    # Pydantic validation rejects the literal at request parse time
    assert r.status_code == 422
