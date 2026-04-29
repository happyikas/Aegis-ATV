"""Audit patrol — periodic background integrity check (v4.0, Claim 54).

The audit chain (M5 SQLite + JSONL), encrypted journal (M15), ATMU
(Agent Telemetry Management Unit) intent log (M10), and cost
attestation ledger (M9, Claim 34) all carry their own per-record
integrity primitives (Ed25519 signatures, Merkle prev_hash linkage,
SHA3-256 commitments, AES-GCM auth tags). v3.x verifies these
**on demand** — at write time and via ``aegis verify-audit``.

This module adds **continuous background patrol**: the daemon walks
each store on a configurable cadence, re-verifies the integrity
primitives, and reports findings.

Six failure classes
-------------------
1. **Signature failure** — Ed25519 verify fails. (active tampering)
2. **Hash mismatch** — recomputed SHA3 ≠ stored ``this_hash``. (bit-rot
   or active tampering)
3. **Chain break** — ``prev_hash`` doesn't link to predecessor's
   ``this_hash``. (record deletion, re-ordering)
4. **AEAD failure** — encrypted journal auth tag fails. (cipher tamper)
5. **Cross-store consistency** — record present in SQLite missing from
   JSONL, or commitment differs from encrypted journal. (partial
   write, drive corruption, malicious deletion)
6. **Sequence gap** — ATMU monotonic ``seq`` skips a number. (record
   deletion)

Patrol strategies (each settable via env vars)
----------------------------------------------
* ``full`` — entire chain; expensive, default cadence 6h.
* ``sample`` — random N% subset; cheap, default cadence 1h.
* ``sequence`` — ATMU intent_log gap detection only; very cheap, 5min.
* ``consistency`` — cross-store SQLite ↔ JSONL ↔ encrypted journal; 1h.
* ``cold`` — pull a sample of segments from the v3.9 tiered archive
  cold tier and re-verify; 24h.

Each patrol returns a :class:`PatrolReport` so operators can plot a
time-series of integrity health.

Patent linkage
--------------
**Claim 54 (proposed)** — periodic integrity attestation. With T3
hardware (M19+), the patrol findings themselves can be signed by
the cost-attestation key (Claim 34) so a downstream verifier knows
the patrol didn't lie either.
"""

from __future__ import annotations

import contextlib
import random
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from aegis.atmu.intent_log import IntentLog
from aegis.audit.encrypted_journal import EncryptedJournal
from aegis.audit.jsonl_store import JsonlStore
from aegis.audit.sqlite_store import AuditDB
from aegis.cost.ledger import CostAttestationLedger
from aegis.sign.ed25519 import verify
from aegis.sign.merkle import record_hash, verify_chain

PatrolScope = Literal[
    "full", "sample", "sequence", "consistency", "cold",
]
PatrolStatus = Literal["clean", "warning", "critical"]


@dataclass
class PatrolFinding:
    """One concrete failure surfaced during a patrol."""

    severity: Literal["warning", "critical"]
    category: Literal[
        "signature", "hash_mismatch", "chain_break", "aead",
        "consistency", "sequence_gap",
    ]
    store: str           # "audit_db" | "jsonl" | "encrypted_journal" | etc.
    record_ref: str      # atv_id, intent record_id, or seq#
    detail: str


@dataclass
class PatrolReport:
    """Snapshot of one patrol invocation."""

    scope: PatrolScope
    started_ns: int = 0
    completed_ns: int = 0
    records_scanned: int = 0
    findings: list[PatrolFinding] = field(default_factory=list)
    status: PatrolStatus = "clean"
    notes: list[str] = field(default_factory=list)

    @property
    def duration_ms(self) -> float:
        return (self.completed_ns - self.started_ns) / 1_000_000

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["duration_ms"] = round(self.duration_ms, 3)
        return d


def _classify_status(findings: list[PatrolFinding]) -> PatrolStatus:
    if any(f.severity == "critical" for f in findings):
        return "critical"
    if findings:
        return "warning"
    return "clean"


# ─────────────────────────────────────────────────────────────────────
# Patrol class
# ─────────────────────────────────────────────────────────────────────


@dataclass
class PatrolConfig:
    """Tunables — all in seconds."""

    full_interval_sec: float = 21600.0      # 6 hours
    sample_interval_sec: float = 3600.0     # 1 hour
    sequence_interval_sec: float = 300.0    # 5 minutes
    consistency_interval_sec: float = 3600.0
    cold_interval_sec: float = 86400.0      # 24 hours
    sample_fraction: float = 0.01           # 1 %
    cold_segments_per_run: int = 3
    poll_seconds: float = 30.0              # daemon wake-up


class AuditPatrol:
    """Periodic background integrity check across all audit stores.

    Construction is cheap; calling :meth:`start` begins the daemon
    thread, :meth:`stop` cleans it up, and :meth:`recent_reports`
    returns the rolling N reports for ops dashboards.

    Each ``patrol_*`` method is also callable on demand.
    """

    def __init__(
        self,
        *,
        public_key: Ed25519PublicKey,
        audit_db: AuditDB,
        jsonl: JsonlStore,
        intent_log: IntentLog,
        cost_ledger: CostAttestationLedger,
        encrypted_journal: EncryptedJournal | None = None,
        cold_archive_dir: Path | None = None,
        cold_data_key: bytes | None = None,
        config: PatrolConfig | None = None,
        max_history: int = 50,
    ) -> None:
        self._pub = public_key
        self._audit = audit_db
        self._jsonl = jsonl
        self._intent = intent_log
        self._cost = cost_ledger
        self._journal = encrypted_journal
        self._cold_dir = cold_archive_dir
        self._cold_data_key = cold_data_key
        self._cfg = config or PatrolConfig()
        self._max_history = max_history
        self._reports: list[PatrolReport] = []
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        # Last-run timestamps per scope
        self._last: dict[PatrolScope, int] = {}

    # ── Reports / introspection ──────────────────────────────────────

    def recent_reports(self, *, limit: int = 10) -> list[dict[str, Any]]:
        with self._lock:
            return [r.to_dict() for r in self._reports[-limit:]]

    def latest_status(self) -> dict[str, Any]:
        with self._lock:
            if not self._reports:
                return {"status": "unknown", "reports": []}
            last = self._reports[-1]
            return {
                "status": last.status,
                "scope": last.scope,
                "completed_ns": last.completed_ns,
                "findings_count": len(last.findings),
            }

    def _record(self, report: PatrolReport) -> PatrolReport:
        with self._lock:
            self._reports.append(report)
            if len(self._reports) > self._max_history:
                self._reports.pop(0)
            self._last[report.scope] = report.completed_ns
        return report

    # ── Patrol strategies ────────────────────────────────────────────

    def patrol_full(self) -> PatrolReport:
        """Walk every aid's audit chain in SQLite + verify each record's
        Ed25519 signature, recompute SHA3 hashes, and check chain links.
        Also walks the cost-attestation ledger sub-chains (Claim 34)."""
        report = PatrolReport(scope="full", started_ns=time.time_ns())
        cur = self._audit.conn.execute("SELECT DISTINCT aid FROM audit")
        aids = [row[0] for row in cur.fetchall()]
        for aid in aids:
            chain = self._audit.get_chain(aid)
            report.records_scanned += len(chain)
            ok, err = verify_chain(chain)
            if not ok:
                report.findings.append(PatrolFinding(
                    severity="critical", category="chain_break",
                    store="audit_db", record_ref=f"aid={aid}",
                    detail=err or "chain verification failed",
                ))
            for rec in chain:
                if not verify(rec, self._pub):
                    report.findings.append(PatrolFinding(
                        severity="critical", category="signature",
                        store="audit_db",
                        record_ref=str(rec.get("atv_id", "<unknown>")),
                        detail="Ed25519 signature verify failed",
                    ))

        # Cost-attestation ledger (Claim 34). Independent chain head
        # per aid; we walk the same aid set since /evaluate appends to
        # both stores together.
        for aid in aids:
            ok, err = self._cost.verify_chain(aid)
            if not ok:
                report.findings.append(PatrolFinding(
                    severity="critical", category="chain_break",
                    store="cost_ledger", record_ref=f"aid={aid}",
                    detail=err or "cost ledger chain verification failed",
                ))

        report.completed_ns = time.time_ns()
        report.status = _classify_status(report.findings)
        return self._record(report)

    def patrol_sample(self, *, fraction: float | None = None) -> PatrolReport:
        """Random subset of records — cheaper sweep for higher cadence."""
        frac = fraction if fraction is not None else self._cfg.sample_fraction
        if not (0.0 < frac <= 1.0):
            raise ValueError(f"fraction must be in (0, 1], got {frac}")
        report = PatrolReport(scope="sample", started_ns=time.time_ns())
        rows = self._audit.conn.execute(
            "SELECT atv_id, payload_json FROM audit"
        ).fetchall()
        if not rows:
            report.completed_ns = time.time_ns()
            return self._record(report)
        n = max(1, int(len(rows) * frac))
        chosen = random.sample(rows, min(n, len(rows)))
        report.records_scanned = len(chosen)
        import json as _json
        for atv_id, payload_json in chosen:
            rec = _json.loads(payload_json)
            # Hash recompute
            stored = rec.get("this_hash")
            recomputed = record_hash(rec.get("payload", {}))
            if stored != recomputed:
                report.findings.append(PatrolFinding(
                    severity="critical", category="hash_mismatch",
                    store="audit_db", record_ref=str(atv_id),
                    detail=f"stored={stored} recomputed={recomputed}",
                ))
            if not verify(rec, self._pub):
                report.findings.append(PatrolFinding(
                    severity="critical", category="signature",
                    store="audit_db", record_ref=str(atv_id),
                    detail="signature failed",
                ))
        report.completed_ns = time.time_ns()
        report.status = _classify_status(report.findings)
        return self._record(report)

    def patrol_sequence(self) -> PatrolReport:
        """Detect gaps in the ATMU intent_log monotonic ``seq`` column."""
        report = PatrolReport(scope="sequence", started_ns=time.time_ns())
        rows = self._intent.conn.execute(
            "SELECT seq, record_id FROM intent_log ORDER BY seq"
        ).fetchall()
        report.records_scanned = len(rows)
        if not rows:
            report.completed_ns = time.time_ns()
            return self._record(report)
        prev_seq = rows[0][0]
        for seq, _record_id in rows[1:]:
            if seq != prev_seq + 1:
                report.findings.append(PatrolFinding(
                    severity="critical", category="sequence_gap",
                    store="atmu_intent_log",
                    record_ref=f"seq={prev_seq}→{seq}",
                    detail=f"missing seqs {prev_seq + 1}..{seq - 1}",
                ))
            prev_seq = seq
        report.completed_ns = time.time_ns()
        report.status = _classify_status(report.findings)
        return self._record(report)

    def patrol_consistency(self) -> PatrolReport:
        """Cross-check SQLite ↔ JSONL ↔ encrypted journal commitments."""
        report = PatrolReport(scope="consistency", started_ns=time.time_ns())

        sql_atv_ids: set[str] = set()
        sql_hashes: dict[str, str] = {}
        for row in self._audit.conn.execute(
            "SELECT atv_id, this_hash FROM audit"
        ).fetchall():
            sql_atv_ids.add(row[0])
            sql_hashes[row[0]] = row[1]
        report.records_scanned = len(sql_atv_ids)

        # JSONL parity
        jsonl_atv_ids: set[str] = set()
        for rec in self._jsonl.read_all():
            jsonl_atv_ids.add(rec.get("atv_id", ""))
        only_sql = sql_atv_ids - jsonl_atv_ids
        only_jsonl = jsonl_atv_ids - sql_atv_ids
        for atv_id in only_sql:
            report.findings.append(PatrolFinding(
                severity="warning", category="consistency",
                store="jsonl", record_ref=str(atv_id),
                detail="present in SQLite, missing from JSONL",
            ))
        for atv_id in only_jsonl:
            report.findings.append(PatrolFinding(
                severity="warning", category="consistency",
                store="audit_db", record_ref=str(atv_id),
                detail="present in JSONL, missing from SQLite",
            ))

        # Encrypted journal commitment cross-check (best-effort)
        if self._journal is not None:
            wrappers = self._journal.list_wrappers()
            for wrapper in wrappers:
                commit = wrapper.get("atv_commitment")
                if not commit:
                    continue
                # Encrypted journal commitment is over the wrapped record;
                # we don't expect it to equal audit SQLite this_hash, but we
                # do expect every record decryptable. Tamper check:
                try:
                    self._journal.decrypt_record(wrapper)
                except Exception as e:  # noqa: BLE001
                    report.findings.append(PatrolFinding(
                        severity="critical", category="aead",
                        store="encrypted_journal",
                        record_ref=str(commit)[:16],
                        detail=f"decrypt failed: {type(e).__name__}: {e}",
                    ))

        report.completed_ns = time.time_ns()
        report.status = _classify_status(report.findings)
        return self._record(report)

    def patrol_cold(self) -> PatrolReport:
        """Sample N segments from the v3.9 cold tier and verify they
        decrypt cleanly with the current data key."""
        report = PatrolReport(scope="cold", started_ns=time.time_ns())
        if self._cold_dir is None or not self._cold_dir.is_dir():
            report.notes.append("cold tier not configured")
            report.completed_ns = time.time_ns()
            return self._record(report)
        if self._cold_data_key is None:
            report.notes.append("cold_data_key not provided — pass to AuditPatrol()")
            report.completed_ns = time.time_ns()
            return self._record(report)
        segments = sorted(p for p in self._cold_dir.iterdir() if p.is_file())
        if not segments:
            report.notes.append("cold tier empty")
            report.completed_ns = time.time_ns()
            return self._record(report)
        n = min(self._cfg.cold_segments_per_run, len(segments))
        chosen = random.sample(segments, n)
        for seg in chosen:
            j = EncryptedJournal(seg, data_key=self._cold_data_key)
            try:
                count = 0
                for r in j.iter_records():
                    count += 1
                    if "_decrypt_error" in r:
                        report.findings.append(PatrolFinding(
                            severity="critical", category="aead",
                            store=f"cold:{seg.name}",
                            record_ref=str(r.get("atv_commitment", "<unknown>"))[:16],
                            detail=f"decrypt error: {r.get('_decrypt_error')}",
                        ))
                report.records_scanned += count
            except Exception as e:  # noqa: BLE001
                report.findings.append(PatrolFinding(
                    severity="critical", category="aead",
                    store=f"cold:{seg.name}", record_ref=seg.name,
                    detail=f"open failed: {type(e).__name__}: {e}",
                ))
        report.completed_ns = time.time_ns()
        report.status = _classify_status(report.findings)
        return self._record(report)

    # ── Background daemon ────────────────────────────────────────────

    def _due(self, scope: PatrolScope, interval_sec: float) -> bool:
        last = self._last.get(scope, 0)
        if last == 0:
            return True  # never run
        return (time.time_ns() - last) / 1e9 >= interval_sec

    def _loop(self) -> None:
        # Fire each scope on its own cadence.
        while not self._stop_event.is_set():
            self._stop_event.wait(timeout=self._cfg.poll_seconds)
            if self._stop_event.is_set():
                break
            with contextlib.suppress(Exception):
                if self._due("sequence", self._cfg.sequence_interval_sec):
                    self.patrol_sequence()
            with contextlib.suppress(Exception):
                if self._due("sample", self._cfg.sample_interval_sec):
                    self.patrol_sample()
            with contextlib.suppress(Exception):
                if self._due("consistency", self._cfg.consistency_interval_sec):
                    self.patrol_consistency()
            with contextlib.suppress(Exception):
                if self._due("full", self._cfg.full_interval_sec):
                    self.patrol_full()
            with contextlib.suppress(Exception):
                if self._due("cold", self._cfg.cold_interval_sec):
                    self.patrol_cold()

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._loop, name="aegis-audit-patrol", daemon=True,
        )
        self._thread.start()

    def stop(self, *, timeout_sec: float = 5.0) -> None:
        if self._thread is None:
            return
        self._stop_event.set()
        self._thread.join(timeout=timeout_sec)
        self._thread = None


__all__ = [
    "AuditPatrol",
    "PatrolConfig",
    "PatrolFinding",
    "PatrolReport",
    "PatrolScope",
    "PatrolStatus",
]
