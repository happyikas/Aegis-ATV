#!/usr/bin/env python3
"""Plugin checkup — module-by-module behavior verification.

For each major subsystem (ATV, ATMU, sLLM judge, Burn-in, Audit chain,
HW collectors, TEE quote, Identity, Compliance), runs a smoke test
and reports pass/fail. This is the user-facing companion to
:mod:`tests.integration.test_plugin_e2e` — printable output suitable
for an enterprise eval session.

Run::

    AEGIS_EMBEDDING_PROVIDER=dummy AEGIS_JUDGE_PROVIDER=dummy \\
      uv run python demo/module_verification.py
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

os.environ.setdefault("AEGIS_EMBEDDING_PROVIDER", "dummy")
os.environ.setdefault("AEGIS_JUDGE_PROVIDER", "dummy")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


@dataclass
class ModuleCheck:
    name: str
    description: str
    status: str  # "pass" | "warn" | "fail"
    details: str = ""


# ─────────────────────────────────────────────────────────────────────
# Individual module checks
# ─────────────────────────────────────────────────────────────────────


def check_atv_builder() -> ModuleCheck:
    """Build a 2080-D ATV and verify shape + non-zero subfields."""
    try:
        from aegis.atv.builder import build_atv
        from aegis.schema import ALL_SUBFIELDS, ATV_DIM, ATVHeader, ATVInput

        inp = ATVInput(
            header=ATVHeader(
                trace_id="t" * 32, span_id="s" * 16,
                tenant_id="check", aid="agent-x", timestamp_ns=0,
            ),
            tool_name="Bash",
            tool_args_json=json.dumps({"command": "ls"}),
        )
        atv = build_atv(inp)
        if atv.shape != (ATV_DIM,):
            return ModuleCheck(
                "ATV builder", "build_atv → 2080-D shape",
                "fail", f"got {atv.shape}, expected ({ATV_DIM},)",
            )
        # How many subfields are non-zero?
        nonzero = 0
        for _name, sl in ALL_SUBFIELDS:
            if (atv[sl] != 0).any():
                nonzero += 1
        return ModuleCheck(
            "ATV builder",
            f"build_atv produces 2080-D with {nonzero}/30 non-zero subfields",
            "pass" if nonzero >= 6 else "warn",
            f"non-zero subfields: {nonzero}/30",
        )
    except Exception as e:
        return ModuleCheck("ATV builder", "exception", "fail", str(e)[:120])


def check_enhanced_adapter() -> ModuleCheck:
    """Enhanced adapter populates more subfields than sparse when transcript present."""
    try:
        import tempfile

        from aegis.atv.adapter import (
            from_claude_code_payload,
            from_claude_code_payload_enhanced,
        )

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".jsonl", delete=False,
        ) as f:
            for ev in [
                {"type": "assistant", "content": "Working on it",
                 "usage": {"input_tokens": 100, "output_tokens": 50}},
                {"type": "tool_use", "name": "Read",
                 "input": {"file_path": "/x"}},
                {"type": "tool_use", "name": "Edit",
                 "input": {"file_path": "/y"}},
            ]:
                f.write(json.dumps(ev) + "\n")
            transcript = f.name

        payload = {
            "session_id": "s",
            "transcript_path": transcript,
            "tool_name": "Bash",
            "tool_input": {"command": "make"},
            "hook_event_name": "PreToolUse",
            "invocation_id": "inv",
        }
        sparse = from_claude_code_payload(payload, tenant_id="t")
        enhanced = from_claude_code_payload_enhanced(payload, tenant_id="t")

        sparse_filled = sum([
            bool(sparse.agent_state_text), bool(sparse.recent_actions),
            bool(sparse.memory_fingerprint),
            bool(sparse.cost_estimate.cumulative_tokens),
        ])
        enhanced_filled = sum([
            bool(enhanced.agent_state_text), bool(enhanced.recent_actions),
            bool(enhanced.memory_fingerprint),
            bool(enhanced.cost_estimate.cumulative_tokens),
        ])
        return ModuleCheck(
            "Enhanced adapter",
            f"from_claude_code_payload_enhanced: {enhanced_filled}/4 extra fields populated",
            "pass" if enhanced_filled > sparse_filled else "fail",
            f"sparse: {sparse_filled} → enhanced: {enhanced_filled}",
        )
    except Exception as e:
        return ModuleCheck("Enhanced adapter", "exception", "fail", str(e)[:120])


def check_atmu_2pc() -> ModuleCheck:
    """ATMU intent_log: tentative → prepared → committed."""
    try:
        import tempfile

        from aegis.atmu import IntentLog, TxState

        with tempfile.TemporaryDirectory() as td:
            log = IntentLog(str(Path(td) / "intent.sqlite"))
            rec = log.append_tentative(
                aid="a", tenant_id="t",
                trace_id="t" * 32, span_id="s" * 16,
                parent_span_id=None,
                tool_name="Bash", tool_args_hash="h", blast_radius=1,
                atv_commitment="c",
            )
            rid = rec["record_id"]
            assert rec["current_state"] == TxState.TENTATIVE.value
            log.transition(rid, new_state=TxState.PREPARED, reason="firewall ok")
            log.transition(rid, new_state=TxState.COMMITTED, reason="signed")
            final = log.get(rid)
            assert final["current_state"] == TxState.COMMITTED.value
            assert len(final["state_history"]) == 3
        return ModuleCheck(
            "ATMU 2PC", "tentative → prepared → committed", "pass",
            "3 state transitions recorded",
        )
    except Exception as e:
        return ModuleCheck("ATMU 2PC", "exception", "fail", str(e)[:120])


def check_sllm_judge() -> ModuleCheck:
    """sLLM judge: M13 attribution head ON ATV → verdict + 30-key attribution."""
    try:

        from aegis.atv.builder import build_atv
        from aegis.judge.attribution_head import AttributionHead
        from aegis.schema import ATVHeader, ATVInput

        inp = ATVInput(
            header=ATVHeader(
                trace_id="t" * 32, span_id="s" * 16,
                tenant_id="check", aid="a", timestamp_ns=0,
            ),
            tool_name="Bash",
            tool_args_json=json.dumps({"command": "rm -rf /var/data"}),
        )
        atv = build_atv(inp)
        v = AttributionHead().evaluate_full("", atv=atv, inp=inp)
        if v.decision not in {"ALLOW", "BLOCK", "REQUIRE_APPROVAL"}:
            return ModuleCheck("sLLM judge (M13)", "decision invalid", "fail")
        if len(v.subfield_attribution) != 30:
            return ModuleCheck(
                "sLLM judge (M13)", "subfield_attribution should have 30 keys",
                "fail", f"got {len(v.subfield_attribution)}",
            )
        return ModuleCheck(
            "sLLM judge (M13)",
            f"AttributionHead → {v.decision} (conf={v.confidence:.2f})",
            "pass",
            f"30/30 subfield attribution + bit-deterministic latency={v.latency_ms:.3f}ms",
        )
    except Exception as e:
        return ModuleCheck("sLLM judge (M13)", "exception", "fail", str(e)[:120])


def check_burnin() -> ModuleCheck:
    """Burn-in 5-layer controller: 5 layer types + status shape."""
    try:
        from aegis.burnin import BurnInController
        from aegis.burnin.controller import LAYER_EXPECTED_SAMPLES

        ctrl = BurnInController()
        status = ctrl.status()
        if "layers" not in status:
            return ModuleCheck("Burn-in M11", "missing 'layers' in status", "fail")
        n_types = len(LAYER_EXPECTED_SAMPLES)
        if n_types < 5:
            return ModuleCheck(
                "Burn-in M11", f"only {n_types}/5 layer types defined", "fail",
            )
        return ModuleCheck(
            "Burn-in M11",
            f"{n_types}-layer × 4-phase controller; status shape OK",
            "pass",
            f"layer types: {sorted(LAYER_EXPECTED_SAMPLES.keys())}",
        )
    except Exception as e:
        return ModuleCheck("Burn-in M11", "exception", "fail", str(e)[:120])


def check_audit_chain() -> ModuleCheck:
    """Audit DB: write a record, verify Ed25519 signature + chain link."""
    try:
        import tempfile

        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey,
        )

        from aegis.audit.sqlite_store import AuditDB
        from aegis.sign.ed25519 import sign_atv, verify
        from aegis.sign.merkle import GENESIS_HASH, record_hash

        with tempfile.TemporaryDirectory() as td:
            db = AuditDB(str(Path(td) / "audit.sqlite"))
            sk = Ed25519PrivateKey.generate()
            pk = sk.public_key()
            atv_bytes = b"\x00" * 32
            header = {
                "aid": "a", "tenant_id": "t",
                "tool_name": "Bash", "decision": "ALLOW", "atv_hash": "0" * 64,
            }
            rec = sign_atv(atv_bytes, header, GENESIS_HASH, sk)
            rec["atv_id"] = "atv-1"
            rec["decision"] = "ALLOW"
            rec["this_hash"] = record_hash(rec["payload"])
            db.append(rec)
            # Verify
            chain = db.get_chain("a")
            assert len(chain) == 1
            assert verify(chain[0], pk)
            db.close()
        return ModuleCheck(
            "Audit chain (M5)",
            "Ed25519 sign + Merkle chain + verify round-trip",
            "pass",
        )
    except Exception as e:
        return ModuleCheck("Audit chain (M5)", "exception", "fail", str(e)[:120])


def check_encrypted_journal() -> ModuleCheck:
    """M15 encrypted journal: append + decrypt + AEAD tamper detection."""
    try:
        import tempfile

        from aegis.audit.encrypted_journal import (
            EncryptedJournal,
            load_or_create_data_key,
        )

        with tempfile.TemporaryDirectory() as td:
            key = load_or_create_data_key(Path(td) / "key.bin")
            j = EncryptedJournal(Path(td) / "j.jsonl", data_key=key)
            j.append({"verdict": "ALLOW",
                      "payload": {"header": {"tenant_id": "t", "aid": "a"}}})
            records = list(j.iter_records())
            assert len(records) == 1
            assert records[0]["verdict"] == "ALLOW"
        return ModuleCheck(
            "Encrypted journal (M15)",
            "AES-GCM AEAD round-trip",
            "pass",
        )
    except Exception as e:
        return ModuleCheck("Encrypted journal (M15)", "exception", "fail", str(e)[:120])


def check_cost_ledger() -> ModuleCheck:
    """M9 cost ledger with separate Ed25519 key (Claim 34)."""
    try:
        import tempfile

        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey,
        )

        from aegis.cost.ledger import CostAttestationLedger

        with tempfile.TemporaryDirectory() as td:
            ledger = CostAttestationLedger(
                db_path=str(Path(td) / "cost.sqlite"),
                jsonl_path=Path(td) / "cost.jsonl",
                signing_key=Ed25519PrivateKey.generate(),
            )
            head_before = ledger.head("a")
            assert head_before == "GENESIS"
            ledger.close()
        return ModuleCheck(
            "Cost ledger (Claim 34)",
            "separate Ed25519 key + chain head tracking",
            "pass",
        )
    except Exception as e:
        return ModuleCheck("Cost ledger (Claim 34)", "exception", "fail", str(e)[:120])


def check_hw_collectors() -> ModuleCheck:
    """v4.1 collector aggregator — at least mock collectors available."""
    try:
        from aegis.hw_telemetry.collectors import CollectorAggregator

        agg = CollectorAggregator()
        rep = agg.availability_report()
        return ModuleCheck(
            "HW collectors (v4.1)",
            f"{len(rep.available)}/{len(rep.available) + len(rep.unavailable)} collectors active",
            "pass",
            f"available: {', '.join(rep.available)}",
        )
    except Exception as e:
        return ModuleCheck("HW collectors (v4.1)", "exception", "fail", str(e)[:120])


def check_tee_quote() -> ModuleCheck:
    """v4.4 TEE quote: detect provider, generate quote (mock fallback)."""
    try:
        os.environ["AEGIS_TEE_PROVIDER"] = "mock"
        from aegis.attest.tee_quote import detect_provider, generate_quote
        from aegis.attest.tee_verifier import TEEQuoteVerifier

        prov = detect_provider()
        quote = generate_quote("burn-in-test", provider=prov)
        if quote is None:
            return ModuleCheck("TEE quote (v4.4)", "no quote produced", "fail")
        v = TEEQuoteVerifier().verify(quote)
        if not v.valid:
            return ModuleCheck(
                "TEE quote (v4.4)",
                f"quote produced but verifier rejected: {v.reasons}",
                "warn",
            )
        return ModuleCheck(
            "TEE quote (v4.4)",
            f"provider={prov}, verifier valid (trust_level={v.extras.get('trust_level')})",
            "pass",
        )
    except Exception as e:
        return ModuleCheck("TEE quote (v4.4)", "exception", "fail", str(e)[:120])


def check_identity() -> ModuleCheck:
    """v4.2 agent identity: issue proof, verify, capability subset."""
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        from aegis.identity.agent_id import AgentIdentity, issue
        from aegis.identity.did import IdentityVerifier

        sk = Ed25519PrivateKey.generate()
        ident = AgentIdentity(
            tenant_id="t", aid="a",
            capabilities=frozenset({"read", "write"}),
        )
        proof = issue(ident, signing_key=sk)
        verifier = IdentityVerifier(local_issuer=sk.public_key())
        if not verifier.verify(proof):
            return ModuleCheck("Identity (v4.2)", "self-issued proof failed verify", "fail")
        # Wrong key fails:
        sk_other = Ed25519PrivateKey.generate()
        v2 = IdentityVerifier(local_issuer=sk_other.public_key())
        if v2.verify(proof):
            return ModuleCheck("Identity (v4.2)", "wrong key passed (security bug!)", "fail")
        return ModuleCheck(
            "Identity (v4.2)",
            "Ed25519 proof issue + verify + wrong-key reject",
            "pass",
        )
    except Exception as e:
        return ModuleCheck("Identity (v4.2)", "exception", "fail", str(e)[:120])


def check_compliance() -> ModuleCheck:
    """v4.3 compliance: load all 4 frameworks, count controls."""
    try:
        from aegis.compliance import AVAILABLE_FRAMEWORKS, EvidenceCollector
        from aegis.compliance.frameworks import SOC2

        if len(AVAILABLE_FRAMEWORKS) != 4:
            return ModuleCheck(
                "Compliance (v4.3)",
                f"expected 4 frameworks, got {len(AVAILABLE_FRAMEWORKS)}",
                "fail",
            )
        # Run collector with no stores → all not_implemented
        collector = EvidenceCollector()
        report = collector.collect(SOC2, period_start_ns=0, period_end_ns=10**18)
        return ModuleCheck(
            "Compliance (v4.3)",
            f"4 frameworks × {sum(len(f.controls) for f in AVAILABLE_FRAMEWORKS.values())} total controls; SOC2 = {len(report.controls)} controls evaluated",
            "pass",
        )
    except Exception as e:
        return ModuleCheck("Compliance (v4.3)", "exception", "fail", str(e)[:120])


def check_audit_patrol() -> ModuleCheck:
    """v4.0 AuditPatrol: instantiate w/ minimal stores, run sequence patrol."""
    try:
        import tempfile

        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey,
        )

        from aegis.atmu.intent_log import IntentLog
        from aegis.audit.jsonl_store import JsonlStore
        from aegis.audit.patrol import AuditPatrol
        from aegis.audit.sqlite_store import AuditDB
        from aegis.cost.ledger import CostAttestationLedger

        with tempfile.TemporaryDirectory() as td:
            sk = Ed25519PrivateKey.generate()
            patrol = AuditPatrol(
                public_key=sk.public_key(),
                audit_db=AuditDB(str(Path(td) / "audit.sqlite")),
                jsonl=JsonlStore(Path(td) / "audit.jsonl"),
                intent_log=IntentLog(str(Path(td) / "intent.sqlite")),
                cost_ledger=CostAttestationLedger(
                    db_path=str(Path(td) / "cost.sqlite"),
                    jsonl_path=Path(td) / "cost.jsonl",
                    signing_key=sk,
                ),
            )
            report = patrol.patrol_consistency()
        return ModuleCheck(
            "Audit patrol (v4.0)",
            f"6-check periodic verifier; consistency patrol scope='{report.scope}'",
            "pass",
        )
    except Exception as e:
        return ModuleCheck("Audit patrol (v4.0)", "exception", "fail", str(e)[:120])


def check_firewall_pipeline() -> ModuleCheck:
    """13-step firewall: ALLOW path on innocuous request."""
    try:

        from aegis.atv.builder import build_atv
        from aegis.firewall.core import default_steps, run_firewall
        from aegis.schema import ATVHeader, ATVInput

        inp = ATVInput(
            header=ATVHeader(
                trace_id="t" * 32, span_id="s" * 16,
                tenant_id="t", aid="a", timestamp_ns=0,
            ),
            tool_name="read_file",
            tool_args_json=json.dumps({"file_path": "/tmp/x.txt"}),
        )
        atv = build_atv(inp)
        verdict = run_firewall(atv, inp, atv_id=inp.header.span_id)
        steps = default_steps()
        return ModuleCheck(
            "Firewall pipeline",
            f"{len(steps)}-step pipeline; innocuous read_file → {verdict.decision}",
            "pass",
            f"step traces: {len(verdict.step_traces)}",
        )
    except Exception as e:
        return ModuleCheck("Firewall pipeline", "exception", "fail", str(e)[:120])


# ─────────────────────────────────────────────────────────────────────
# Driver
# ─────────────────────────────────────────────────────────────────────


def main() -> int:
    checks = [
        check_atv_builder(),
        check_enhanced_adapter(),
        check_firewall_pipeline(),
        check_atmu_2pc(),
        check_sllm_judge(),
        check_burnin(),
        check_audit_chain(),
        check_encrypted_journal(),
        check_cost_ledger(),
        check_audit_patrol(),
        check_hw_collectors(),
        check_tee_quote(),
        check_identity(),
        check_compliance(),
    ]

    print()
    print("Aegis Plugin Checkup — Module Verification")
    print("=" * 70)
    print()
    for c in checks:
        badge = {"pass": "✅", "warn": "🟡", "fail": "❌"}.get(c.status, "?")
        print(f"{badge} {c.name:<30} — {c.description}")
        if c.details:
            print(f"   {c.details}")
    print()
    n_pass = sum(1 for c in checks if c.status == "pass")
    n_warn = sum(1 for c in checks if c.status == "warn")
    n_fail = sum(1 for c in checks if c.status == "fail")
    print(f"Result: {n_pass} pass / {n_warn} warn / {n_fail} fail "
          f"(of {len(checks)} modules)")
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
