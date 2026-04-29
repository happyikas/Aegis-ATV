"""Compliance evidence automation (v4.3, Claim 57).

Turns the existing audit primitives (Ed25519 + Merkle audit chain,
encrypted journal, ATMU intent log, cost ledger, AuditPatrol reports)
into structured compliance evidence packets for four frameworks:

* **SOC 2 Trust Services Criteria** — CC6 (Logical & Physical Access),
  CC7 (System Operations), CC8 (Change Management).
* **EU AI Act Annex IV** — record-keeping for high-risk AI systems
  (Art. 12 + Annex IV §2).
* **HIPAA** — Audit Controls (45 CFR § 164.312(b)).
* **ISO/IEC 42001** — AI Management System controls (AIMS).

The collector walks the audit stores for a given period and emits
both:

1. A machine-readable JSON evidence packet (one row per control,
   with sample audit record IDs for verifiers).
2. An auditor-friendly HTML / Markdown report.

Why this matters
----------------
Enterprise procurement of AegisData is gated by compliance, not
security. \"Does this map to our SOC 2 controls?\" is the first
question. v4.3 makes the answer concrete + automated.

Patent
------
Claim 57 — automated mapping of cryptographically-signed agent
audit primitives to compliance control frameworks, with sample
evidence selection that's deterministic + audit-replayable.
"""

from __future__ import annotations

from aegis.compliance.evidence import (
    ComplianceReport,
    ControlEvidence,
    EvidenceCollector,
)
from aegis.compliance.frameworks import (
    AVAILABLE_FRAMEWORKS,
    ComplianceControl,
    ComplianceFramework,
    get_framework,
)

__all__ = [
    "AVAILABLE_FRAMEWORKS",
    "ComplianceControl",
    "ComplianceFramework",
    "ComplianceReport",
    "ControlEvidence",
    "EvidenceCollector",
    "get_framework",
]
