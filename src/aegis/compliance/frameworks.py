"""Compliance framework definitions — SOC 2 / EU AI Act / HIPAA / ISO 42001.

Each framework ships as a ``ComplianceFramework`` with a list of
``ComplianceControl`` entries. Each control declares:

* ``id``: the official identifier (e.g. ``CC6.1``, ``ANNEX_IV_2_b``).
* ``title``: human-readable summary.
* ``aegis_evidence_query``: a structured query the
  :class:`EvidenceCollector` runs against our audit stores to gather
  evidence. Query types:

    - ``audit_chain``: full Ed25519 + Merkle chain coverage
    - ``encrypted_journal``: AES-GCM record presence + integrity
    - ``intent_log``: ATMU 2PC state machine completeness
    - ``cost_ledger``: separate-key cost attestation (Claim 34)
    - ``patrol_report``: AuditPatrol findings for the period
    - ``identity_proof``: agent identity verification samples
    - ``key_rotation``: signing-key rotation events
    - ``access_log``: ATMU intent log → who-accessed-what

The mapping is **conservative** — we only claim a control is
\"covered\" when our audit primitives directly produce evidence for
it. Where we don't, the control is marked as ``"not_implemented"``
so the auditor knows what's missing rather than blindly trusting.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

EvidenceQueryType = Literal[
    "audit_chain",
    "encrypted_journal",
    "intent_log",
    "cost_ledger",
    "patrol_report",
    "identity_proof",
    "key_rotation",
    "access_log",
    "not_implemented",
]


@dataclass(frozen=True)
class ComplianceControl:
    """One compliance control's mapping to Aegis evidence."""

    id: str
    title: str
    evidence_query: EvidenceQueryType
    notes: str = ""
    sample_size: int = 5  # how many sample records to include


@dataclass(frozen=True)
class ComplianceFramework:
    """A framework's full control set."""

    name: str
    version: str
    description: str
    controls: tuple[ComplianceControl, ...] = field(default_factory=tuple)


# ─────────────────────────────────────────────────────────────────────
# SOC 2 Trust Services Criteria — CC6, CC7, CC8 subset
# ─────────────────────────────────────────────────────────────────────

SOC2 = ComplianceFramework(
    name="SOC2",
    version="2017-revised",
    description="SOC 2 Trust Services Criteria — security & availability subset",
    controls=(
        ComplianceControl(
            id="CC6.1",
            title="Logical and physical access — implements logical access controls",
            evidence_query="identity_proof",
            notes=(
                "Aegis enforces agent identity via step308 + MCP middleware. "
                "Capability escalation structurally impossible (Claim 56)."
            ),
        ),
        ComplianceControl(
            id="CC6.2",
            title="Authorisation prior to issuing access credentials",
            evidence_query="identity_proof",
            notes="Identity proofs Ed25519-signed with TTL; revocation via expiry.",
        ),
        ComplianceControl(
            id="CC6.6",
            title="Logical access security — restrict by AID + tenant region",
            evidence_query="access_log",
            notes="M14 per-AID circuit breaker + step315 AID-region authorization.",
        ),
        ComplianceControl(
            id="CC7.1",
            title="System operations — detection and response to security events",
            evidence_query="patrol_report",
            notes="v4.0 AuditPatrol surfaces integrity findings on 5 cadences.",
        ),
        ComplianceControl(
            id="CC7.2",
            title="Monitor system components for anomalies",
            evidence_query="patrol_report",
            notes="6 finding categories: signature/hash/chain/aead/consistency/seq_gap.",
        ),
        ComplianceControl(
            id="CC7.3",
            title="Evaluate security events for incident response",
            evidence_query="patrol_report",
        ),
        ComplianceControl(
            id="CC7.4",
            title="System operations — incident response procedures",
            evidence_query="intent_log",
            notes="ATMU 2PC + compensation plans (Claim 2/15) execute incident response.",
        ),
        ComplianceControl(
            id="CC8.1",
            title="Change management — record changes to system components",
            evidence_query="audit_chain",
            notes="Every /evaluate Ed25519+Merkle-signed; verify-audit replay-able.",
        ),
        ComplianceControl(
            id="A1.2",
            title="Availability — backup and recovery",
            evidence_query="encrypted_journal",
            notes="v3.9 tiered archive (hot/warm/cold) + AES-GCM encrypted journal.",
        ),
    ),
)


# ─────────────────────────────────────────────────────────────────────
# EU AI Act — Annex IV record-keeping for high-risk AI systems
# ─────────────────────────────────────────────────────────────────────

EU_AI_ACT = ComplianceFramework(
    name="EU_AI_ACT",
    version="2024/1689",
    description="EU AI Act Annex IV record-keeping for high-risk AI systems",
    controls=(
        ComplianceControl(
            id="ART_12_1",
            title="High-risk AI systems shall technically allow for the automatic recording of events ('logs')",
            evidence_query="audit_chain",
            notes="Every tool call signed + chained. Tamper-evident.",
        ),
        ComplianceControl(
            id="ART_12_2_a",
            title="Identification of situations that may result in the AI system presenting a risk",
            evidence_query="patrol_report",
        ),
        ComplianceControl(
            id="ART_12_2_b",
            title="Facilitation of post-market monitoring",
            evidence_query="audit_chain",
        ),
        ComplianceControl(
            id="ART_12_2_c",
            title="Monitoring of the operation of the high-risk AI system",
            evidence_query="patrol_report",
        ),
        ComplianceControl(
            id="ANNEX_IV_2_b",
            title="Description of training, testing and validation procedures",
            evidence_query="not_implemented",
            notes="Aegis is not a training system; this control belongs to the model provider.",
        ),
        ComplianceControl(
            id="ANNEX_IV_2_g",
            title="Description of system used to produce decisions",
            evidence_query="audit_chain",
            notes="step_traces dict in every audit record carries firewall step decisions.",
        ),
        ComplianceControl(
            id="ANNEX_IV_3",
            title="Detailed information about the monitoring, functioning and control of the AI system",
            evidence_query="patrol_report",
        ),
        ComplianceControl(
            id="ANNEX_IV_4",
            title="Description of the appropriateness of the performance metrics",
            evidence_query="cost_ledger",
            notes="v3.x perf advisory + cost attestation provides metric history.",
        ),
        ComplianceControl(
            id="ANNEX_IV_6",
            title="Description of changes made to the system through its lifecycle",
            evidence_query="key_rotation",
            notes="Burn-in layer transitions + signing key rotations form change history.",
        ),
    ),
)


# ─────────────────────────────────────────────────────────────────────
# HIPAA — 45 CFR § 164.312(b) Audit controls
# ─────────────────────────────────────────────────────────────────────

HIPAA = ComplianceFramework(
    name="HIPAA",
    version="45_CFR_164",
    description="HIPAA Security Rule audit controls",
    controls=(
        ComplianceControl(
            id="164_312_a_1",
            title="Access control — unique user identification",
            evidence_query="identity_proof",
            notes="Per-aid identity proofs with capability claims.",
        ),
        ComplianceControl(
            id="164_312_a_2_iv",
            title="Encryption and decryption — implement mechanism to encrypt and decrypt ePHI",
            evidence_query="encrypted_journal",
            notes="AES-256-GCM AEAD + SHA3-256 commitment per Claim 21.",
        ),
        ComplianceControl(
            id="164_312_b",
            title="Audit controls — record and examine activity in systems that contain ePHI",
            evidence_query="audit_chain",
            notes="Ed25519 + Merkle chain. ``aegis verify-audit`` replays chain.",
        ),
        ComplianceControl(
            id="164_312_c_1",
            title="Integrity — protect ePHI from improper alteration or destruction",
            evidence_query="patrol_report",
            notes="v4.0 AuditPatrol detects bit-rot + active tampering.",
        ),
        ComplianceControl(
            id="164_312_c_2",
            title="Mechanism to authenticate ePHI",
            evidence_query="audit_chain",
        ),
        ComplianceControl(
            id="164_312_d",
            title="Person or entity authentication",
            evidence_query="identity_proof",
        ),
        ComplianceControl(
            id="164_312_e_1",
            title="Transmission security — guard against unauthorized access during transmission",
            evidence_query="not_implemented",
            notes=(
                "Aegis sidecar relies on caller's TLS termination. Recommend "
                "deploying behind mTLS proxy (e.g. Linkerd/Istio mesh)."
            ),
        ),
    ),
)


# ─────────────────────────────────────────────────────────────────────
# ISO/IEC 42001 — AI Management System controls
# ─────────────────────────────────────────────────────────────────────

ISO_42001 = ComplianceFramework(
    name="ISO_42001",
    version="2023",
    description="ISO/IEC 42001 — AI Management System (AIMS)",
    controls=(
        ComplianceControl(
            id="A_5_2",
            title="AI policy — establish, implement and maintain AI policies",
            evidence_query="audit_chain",
        ),
        ComplianceControl(
            id="A_6_1_2",
            title="AI risk assessment",
            evidence_query="patrol_report",
        ),
        ComplianceControl(
            id="A_8_1",
            title="Operational planning and control",
            evidence_query="intent_log",
            notes="ATMU 2PC governs every tool invocation lifecycle.",
        ),
        ComplianceControl(
            id="A_8_3",
            title="AI system impact assessment",
            evidence_query="cost_ledger",
        ),
        ComplianceControl(
            id="A_9_1",
            title="Performance evaluation — monitoring, measurement, analysis",
            evidence_query="patrol_report",
        ),
        ComplianceControl(
            id="A_10_2",
            title="Continual improvement",
            evidence_query="key_rotation",
            notes="Burn-in layer transitions document continual improvement.",
        ),
    ),
)


# ─────────────────────────────────────────────────────────────────────
# Registry
# ─────────────────────────────────────────────────────────────────────

AVAILABLE_FRAMEWORKS: dict[str, ComplianceFramework] = {
    "soc2": SOC2,
    "eu_ai_act": EU_AI_ACT,
    "hipaa": HIPAA,
    "iso_42001": ISO_42001,
}


def get_framework(name: str) -> ComplianceFramework:
    """Look up a framework by case-insensitive name. Raises KeyError if unknown."""
    key = name.lower().strip()
    if key not in AVAILABLE_FRAMEWORKS:
        available = ", ".join(sorted(AVAILABLE_FRAMEWORKS.keys()))
        raise KeyError(f"unknown framework {name!r}; available: {available}")
    return AVAILABLE_FRAMEWORKS[key]
