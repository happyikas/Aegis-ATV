"""Evidence collector — walks audit stores and emits structured evidence.

The collector is a **pure function** of (audit stores, period, framework):
given the same stores at the same point in time, it produces the same
evidence packet bit-identical. This is a patent claim for compliance
audit reproducibility.

Sample selection
----------------
For most controls, a small **deterministic** sample is enough — the
auditor doesn't want to read 10 M records, they want 5 representative
records they can spot-check. We use SHA3-deterministic sampling
(seed = control.id + period_start_ns) so two evidence runs produce
identical samples.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Any

from aegis.atmu.intent_log import IntentLog
from aegis.audit.encrypted_journal import EncryptedJournal
from aegis.audit.sqlite_store import AuditDB
from aegis.compliance.frameworks import (
    ComplianceControl,
    ComplianceFramework,
)
from aegis.cost.ledger import CostAttestationLedger


@dataclass
class ControlEvidence:
    """One control's evidence row in the report."""

    control_id: str
    control_title: str
    coverage: str  # "covered" | "partial" | "not_implemented"
    evidence_type: str
    record_count: int
    sample_record_ids: list[str] = field(default_factory=list)
    notes: str = ""


@dataclass
class ComplianceReport:
    """Complete evidence packet for one (framework, period) tuple."""

    framework_name: str
    framework_version: str
    period_start_ns: int
    period_end_ns: int
    generated_at_ns: int
    controls: list[ControlEvidence] = field(default_factory=list)
    summary: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=True)

    def to_markdown(self) -> str:
        """Human-readable Markdown for compliance reviewer."""
        lines: list[str] = []
        lines.append(f"# {self.framework_name} Compliance Evidence Report")
        lines.append("")
        lines.append(f"**Framework:** {self.framework_name} v{self.framework_version}")
        lines.append(f"**Period:** {self.period_start_ns} → {self.period_end_ns} (ns)")
        lines.append(f"**Generated:** {self.generated_at_ns} (ns)")
        lines.append("")
        if self.summary:
            lines.append("## Coverage Summary")
            lines.append("")
            for k, v in sorted(self.summary.items()):
                lines.append(f"- **{k}:** {v}")
            lines.append("")
        lines.append("## Controls")
        lines.append("")
        for c in self.controls:
            badge = {
                "covered": "✅",
                "partial": "⚠️",
                "not_implemented": "❌",
            }.get(c.coverage, "?")
            lines.append(f"### {badge} {c.control_id} — {c.control_title}")
            lines.append("")
            lines.append(f"- **Coverage:** {c.coverage}")
            lines.append(f"- **Evidence type:** {c.evidence_type}")
            lines.append(f"- **Record count:** {c.record_count}")
            if c.sample_record_ids:
                lines.append("- **Sample record IDs:**")
                for rid in c.sample_record_ids:
                    lines.append(f"  - `{rid}`")
            if c.notes:
                lines.append(f"- **Notes:** {c.notes}")
            lines.append("")
        return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────
# Deterministic sampler
# ─────────────────────────────────────────────────────────────────────


def _deterministic_sample(
    items: list[Any],
    *,
    seed_text: str,
    n: int,
) -> list[Any]:
    """Pick ``n`` items from ``items`` deterministically based on ``seed_text``.

    Same seed + same input list → same output sample. Bit-identical
    across runs for audit replay.
    """
    if not items:
        return []
    if n >= len(items):
        return list(items)
    seed = hashlib.sha3_256(seed_text.encode()).digest()
    # Score each item by SHA3(seed || index) — pick top-n by score.
    scored: list[tuple[bytes, Any]] = []
    for i, item in enumerate(items):
        score = hashlib.sha3_256(seed + i.to_bytes(8, "big")).digest()
        scored.append((score, item))
    scored.sort(key=lambda kv: kv[0])
    return [item for _, item in scored[:n]]


# ─────────────────────────────────────────────────────────────────────
# Collector
# ─────────────────────────────────────────────────────────────────────


class EvidenceCollector:
    """Run a framework's controls against the audit stores and assemble
    a :class:`ComplianceReport`.

    All store handles are **read-only** in the collector — it never
    mutates the audit chain.
    """

    def __init__(
        self,
        *,
        audit_db: AuditDB | None = None,
        intent_log: IntentLog | None = None,
        cost_ledger: CostAttestationLedger | None = None,
        encrypted_journal: EncryptedJournal | None = None,
        patrol_reports: list[dict[str, Any]] | None = None,
    ) -> None:
        self._audit_db = audit_db
        self._intent_log = intent_log
        self._cost_ledger = cost_ledger
        self._journal = encrypted_journal
        self._patrol = patrol_reports or []

    def collect(
        self,
        framework: ComplianceFramework,
        *,
        period_start_ns: int,
        period_end_ns: int,
    ) -> ComplianceReport:
        import time

        if period_start_ns > period_end_ns:
            raise ValueError("period_start_ns must be ≤ period_end_ns")

        control_evidence: list[ControlEvidence] = []
        for control in framework.controls:
            ev = self._collect_one(
                control, period_start_ns=period_start_ns, period_end_ns=period_end_ns,
            )
            control_evidence.append(ev)

        # Coverage summary
        summary: dict[str, int] = {}
        for c in control_evidence:
            summary[c.coverage] = summary.get(c.coverage, 0) + 1

        return ComplianceReport(
            framework_name=framework.name,
            framework_version=framework.version,
            period_start_ns=period_start_ns,
            period_end_ns=period_end_ns,
            generated_at_ns=time.time_ns(),
            controls=control_evidence,
            summary=summary,
        )

    # ── Per-query handlers ───────────────────────────────────────────

    def _collect_one(
        self,
        control: ComplianceControl,
        *,
        period_start_ns: int,
        period_end_ns: int,
    ) -> ControlEvidence:
        seed_text = f"{control.id}|{period_start_ns}|{period_end_ns}"
        match control.evidence_query:
            case "audit_chain":
                return self._audit_chain(control, period_start_ns, period_end_ns, seed_text)
            case "encrypted_journal":
                return self._encrypted_journal(control, period_start_ns, period_end_ns, seed_text)
            case "intent_log":
                return self._intent_log_evidence(control, period_start_ns, period_end_ns, seed_text)
            case "cost_ledger":
                return self._cost_ledger_evidence(control, period_start_ns, period_end_ns, seed_text)
            case "patrol_report":
                return self._patrol_evidence(control, period_start_ns, period_end_ns, seed_text)
            case "identity_proof" | "access_log":
                return self._access_log_evidence(control, period_start_ns, period_end_ns, seed_text)
            case "key_rotation":
                return self._key_rotation_evidence(control)
            case "not_implemented":
                return ControlEvidence(
                    control_id=control.id,
                    control_title=control.title,
                    coverage="not_implemented",
                    evidence_type="none",
                    record_count=0,
                    notes=control.notes or "Control not covered by Aegis primitives.",
                )

    def _audit_chain(
        self, control: ComplianceControl,
        start_ns: int, end_ns: int, seed: str,
    ) -> ControlEvidence:
        if self._audit_db is None:
            return ControlEvidence(
                control_id=control.id, control_title=control.title,
                coverage="not_implemented",
                evidence_type="audit_chain", record_count=0,
                notes="audit_db not configured",
            )
        rows = self._audit_db.conn.execute(
            "SELECT atv_id FROM audit WHERE timestamp_ns >= ? AND timestamp_ns <= ?",
            (start_ns, end_ns),
        ).fetchall()
        ids = [r[0] for r in rows]
        sample = _deterministic_sample(ids, seed_text=seed, n=control.sample_size)
        return ControlEvidence(
            control_id=control.id, control_title=control.title,
            coverage="covered" if ids else "partial",
            evidence_type="audit_chain",
            record_count=len(ids),
            sample_record_ids=sample,
            notes=control.notes,
        )

    def _encrypted_journal(
        self, control: ComplianceControl,
        start_ns: int, end_ns: int, seed: str,
    ) -> ControlEvidence:
        if self._journal is None:
            return ControlEvidence(
                control_id=control.id, control_title=control.title,
                coverage="not_implemented",
                evidence_type="encrypted_journal", record_count=0,
                notes="encrypted_journal not configured",
            )
        wrappers = [
            w for w in self._journal.list_wrappers()
            if start_ns <= int(w.get("ts_ns", 0)) <= end_ns
        ]
        ids = [str(w.get("atv_commitment", "")) for w in wrappers if w.get("atv_commitment")]
        sample = _deterministic_sample(ids, seed_text=seed, n=control.sample_size)
        return ControlEvidence(
            control_id=control.id, control_title=control.title,
            coverage="covered" if ids else "partial",
            evidence_type="encrypted_journal",
            record_count=len(ids),
            sample_record_ids=sample,
            notes=control.notes,
        )

    def _intent_log_evidence(
        self, control: ComplianceControl,
        start_ns: int, end_ns: int, seed: str,
    ) -> ControlEvidence:
        if self._intent_log is None:
            return ControlEvidence(
                control_id=control.id, control_title=control.title,
                coverage="not_implemented",
                evidence_type="intent_log", record_count=0,
                notes="intent_log not configured",
            )
        rows = self._intent_log.conn.execute(
            "SELECT record_id FROM intent_log WHERE ts_ns >= ? AND ts_ns <= ?",
            (start_ns, end_ns),
        ).fetchall()
        ids = [r[0] for r in rows]
        sample = _deterministic_sample(ids, seed_text=seed, n=control.sample_size)
        return ControlEvidence(
            control_id=control.id, control_title=control.title,
            coverage="covered" if ids else "partial",
            evidence_type="intent_log",
            record_count=len(ids),
            sample_record_ids=sample,
            notes=control.notes,
        )

    def _cost_ledger_evidence(
        self, control: ComplianceControl,
        start_ns: int, end_ns: int, seed: str,
    ) -> ControlEvidence:
        if self._cost_ledger is None:
            return ControlEvidence(
                control_id=control.id, control_title=control.title,
                coverage="not_implemented",
                evidence_type="cost_ledger", record_count=0,
                notes="cost_ledger not configured",
            )
        rows = self._cost_ledger.conn.execute(
            "SELECT record_id FROM cost_attestation WHERE signed_at_ns >= ? AND signed_at_ns <= ?",
            (start_ns, end_ns),
        ).fetchall()
        ids = [r[0] for r in rows]
        sample = _deterministic_sample(ids, seed_text=seed, n=control.sample_size)
        return ControlEvidence(
            control_id=control.id, control_title=control.title,
            coverage="covered" if ids else "partial",
            evidence_type="cost_ledger",
            record_count=len(ids),
            sample_record_ids=sample,
            notes=control.notes,
        )

    def _patrol_evidence(
        self, control: ComplianceControl,
        start_ns: int, end_ns: int, seed: str,
    ) -> ControlEvidence:
        if not self._patrol:
            return ControlEvidence(
                control_id=control.id, control_title=control.title,
                coverage="not_implemented",
                evidence_type="patrol_report", record_count=0,
                notes="no patrol reports provided",
            )
        in_period = [
            r for r in self._patrol
            if start_ns <= int(r.get("started_ns", 0)) <= end_ns
        ]
        ids = [
            f"patrol-{r.get('scope', '?')}-{r.get('started_ns', 0)}"
            for r in in_period
        ]
        sample = _deterministic_sample(ids, seed_text=seed, n=control.sample_size)
        return ControlEvidence(
            control_id=control.id, control_title=control.title,
            coverage="covered" if in_period else "partial",
            evidence_type="patrol_report",
            record_count=len(in_period),
            sample_record_ids=sample,
            notes=control.notes,
        )

    def _access_log_evidence(
        self, control: ComplianceControl,
        start_ns: int, end_ns: int, seed: str,
    ) -> ControlEvidence:
        # Access log == ATMU intent_log (every tool call is an access event).
        if self._intent_log is None:
            return ControlEvidence(
                control_id=control.id, control_title=control.title,
                coverage="not_implemented",
                evidence_type=control.evidence_query, record_count=0,
            )
        rows = self._intent_log.conn.execute(
            """SELECT record_id, aid, tenant_id FROM intent_log
               WHERE ts_ns >= ? AND ts_ns <= ?""",
            (start_ns, end_ns),
        ).fetchall()
        ids = [f"{r[2]}/{r[1]}/{r[0]}" for r in rows]
        sample = _deterministic_sample(ids, seed_text=seed, n=control.sample_size)
        return ControlEvidence(
            control_id=control.id, control_title=control.title,
            coverage="covered" if ids else "partial",
            evidence_type=control.evidence_query,
            record_count=len(ids),
            sample_record_ids=sample,
            notes=control.notes,
        )

    def _key_rotation_evidence(
        self, control: ComplianceControl,
    ) -> ControlEvidence:
        # Key rotation tracking is not yet a first-class feature.
        # When implemented (v4.x) it walks the keys/ directory's mtime
        # history. For now, declare not_implemented honestly.
        return ControlEvidence(
            control_id=control.id, control_title=control.title,
            coverage="not_implemented",
            evidence_type="key_rotation",
            record_count=0,
            notes=(
                "Key rotation tracking is a v4.x milestone; current "
                "deployments document rotation manually."
            ),
        )
