"""Step 360 — Serialize, sign, and append to Audit Log (patent ¶[0062]).

Single canonical entry point used by /evaluate and /approve. Responsible for:

  1. Canonical-JSON serialize the decision + its inputs.
  2. SHA3-256 commit the ATV bytes and the payload header.
  3. Ed25519-sign per Section 4.
  4. Append to both the JSONL raw dump and the SQLite indexed store.
  5. If the decision was influenced by step 335's cost gate, emit an
     event for the Cost Attestation Ledger (implementation deferred
     to M12; we log the intent here so the ledger path is clearly
     marked).
"""

from __future__ import annotations

import hashlib
from typing import Any

import numpy as np

from aegis.audit.jsonl_store import JsonlStore
from aegis.audit.sqlite_store import AuditDB
from aegis.schema import ATVInput, Verdict
from aegis.sign.ed25519 import sign_atv
from aegis.sign.merkle import record_hash


def _cost_gate_influenced(verdict: Verdict) -> bool:
    """True if step 335 terminated or warned about cost.

    The Cost Attestation Ledger (M12) will hook here — any record
    influenced by cost flows into the ledger with its own signature.
    """
    for trace in verdict.step_traces.values():
        low = trace.lower()
        if "step335" in low and (
            "exceed" in low or "over ceiling" in low or "approaching" in low
        ):
            return True
    return False


def sign_and_append(
    *,
    atv: np.ndarray,
    verdict: Verdict,
    inp: ATVInput,
    key: Any,
    db: AuditDB,
    log: JsonlStore,
) -> dict[str, Any]:
    """Produce a signed record + append to audit log. Returns the record."""
    prev = db.get_head(inp.header.aid)

    header_dict: dict[str, Any] = inp.header.model_dump() | {
        "decision": verdict.decision,
        "tool_name": inp.tool_name,
        "atv_hash": hashlib.sha3_256(atv.tobytes()).hexdigest(),
    }
    record = sign_atv(atv.tobytes(), header_dict, prev, key)
    record["atv_id"] = verdict.atv_id
    record["decision"] = verdict.decision
    record["this_hash"] = record_hash(record["payload"])
    # M12 hook: flag cost-influenced records so the future Cost
    # Attestation Ledger can index them.
    record["cost_attestation_hint"] = _cost_gate_influenced(verdict)

    log.append(record)
    db.append(record)
    return record
