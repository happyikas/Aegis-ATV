"""GET /forensic/replay — patent ¶[0102G-1] forensic-reconstruction endpoint.

Walks the encrypted ATV journal end-to-end, returns per-AID chain
validity, decryption results, and any tampered/torn records. Read-only;
safe to call repeatedly.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from aegis.audit.encrypted_journal import EncryptedJournal
from aegis.audit.replay import replay


def make_router(*, journal: EncryptedJournal | None = None) -> APIRouter:
    r = APIRouter()

    @r.get("/forensic/replay")
    def forensic_replay() -> dict[str, Any]:
        if journal is None:
            return {
                "available": False,
                "reason": "encrypted journal not configured (set AEGIS_JOURNAL_DATA_KEY_PATH + AEGIS_JOURNAL_PATH)",
            }
        report = replay(journal)
        return {"available": True, **report.to_dict()}

    return r
