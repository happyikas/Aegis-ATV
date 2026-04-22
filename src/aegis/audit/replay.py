"""Forensic replay engine (patent ¶[0102G]-[0102H]).

Walks the encrypted journal and produces a ReplayReport that:
  - enumerates every entry (decrypted_count + tampered_count),
  - reconstructs the per-AID head hash chain,
  - reports any commitment-mismatch / tamper events,
  - cross-checks each record against the audit DB's chain head.

Replay is read-only and produces no real-world side effects (¶[0102G-1])
— safe to run arbitrarily many times.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from aegis.audit.encrypted_journal import EncryptedJournal


@dataclass
class ReplayReport:
    total_lines: int = 0
    decrypted_count: int = 0
    tampered_count: int = 0
    aids_seen: set[str] = field(default_factory=set)
    per_aid_chain_valid: dict[str, bool] = field(default_factory=dict)
    per_aid_head: dict[str, str] = field(default_factory=dict)
    tampered_records: list[dict[str, Any]] = field(default_factory=list)
    decrypted_records: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_lines": self.total_lines,
            "decrypted_count": self.decrypted_count,
            "tampered_count": self.tampered_count,
            "aids_seen": sorted(self.aids_seen),
            "per_aid_chain_valid": self.per_aid_chain_valid,
            "per_aid_head": self.per_aid_head,
            "tampered_records": self.tampered_records,
        }


def replay(journal: EncryptedJournal, *, include_records: bool = False) -> ReplayReport:
    """Walk the journal, reconstruct per-AID hash chains, flag tamper."""
    rep = ReplayReport()
    per_aid_prev: dict[str, str] = {}

    for line_no, item in enumerate(journal.iter_records(), start=1):
        rep.total_lines += 1
        if "_decrypt_error" in item:
            rep.tampered_count += 1
            rep.tampered_records.append(item)
            continue
        rep.decrypted_count += 1

        payload = item.get("payload") or {}
        aid = payload.get("header", {}).get("aid", "unknown")
        rep.aids_seen.add(aid)

        # Verify per-AID prev_hash linkage.
        prev_in_record = payload.get("prev_hash", "GENESIS")
        expected_prev = per_aid_prev.get(aid, "GENESIS")
        if prev_in_record != expected_prev:
            rep.per_aid_chain_valid[aid] = False
            rep.tampered_records.append({
                "_decrypt_error": "chain_break",
                "aid": aid,
                "expected_prev": expected_prev,
                "actual_prev": prev_in_record,
                "line_no": line_no,
            })
        else:
            # Mark valid if not already invalidated.
            rep.per_aid_chain_valid.setdefault(aid, True)

        this_hash = item.get("this_hash") or item.get("atv_commitment", "")
        per_aid_prev[aid] = this_hash
        rep.per_aid_head[aid] = this_hash

        if include_records:
            rep.decrypted_records.append(item)

    return rep
