"""HAM endpoint helper — avoids bash heredoc quoting hell.

Usage:
    python3 _ham.py memory <aid> <tenant> <body_json> [tags_csv]
    python3 _ham.py recall <aid> <tenant> [tags_csv]
    python3 _ham.py forget <aid> <tenant> <object_id> <reason>
    python3 _ham.py ground <aid> <tenant> <claim> <ref_ids_csv>
    python3 _ham.py stats  [tenant]
    python3 _ham.py admin_release <aid> <reason>

Outputs the JSON response from Aegis.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request

AEGIS_URL = os.environ.get("AEGIS_URL", "http://localhost:8000")


def _post(path: str, body: dict, headers: dict | None = None) -> dict:
    req = urllib.request.Request(
        f"{AEGIS_URL}{path}",
        data=json.dumps(body).encode("utf-8"),
        headers={"content-type": "application/json", **(headers or {})},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    op = sys.argv[1]
    if op == "memory":
        aid, tenant, body_json = sys.argv[2], sys.argv[3], sys.argv[4]
        tags = sys.argv[5].split(",") if len(sys.argv) >= 6 and sys.argv[5] else []
        out = _post("/ham/memory", {
            "aid": aid, "tenant_id": tenant,
            "body": json.loads(body_json), "tags": tags,
        })
    elif op == "recall":
        aid, tenant = sys.argv[2], sys.argv[3]
        tags = sys.argv[4].split(",") if len(sys.argv) >= 5 and sys.argv[4] else []
        out = _post("/ham/recall", {"aid": aid, "tenant_id": tenant, "tags": tags})
    elif op == "forget":
        aid, tenant, oid, reason = sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5]
        out = _post("/ham/forget", {
            "object_id": oid, "aid": aid, "tenant_id": tenant, "reason": reason,
        })
    elif op == "ground":
        aid, tenant, claim, refs_csv = sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5]
        refs = [r for r in refs_csv.split(",") if r]
        out = _post("/ham/ground", {
            "aid": aid, "tenant_id": tenant,
            "claim": claim, "reference_ids": refs,
        })
    elif op == "stats":
        tenant = sys.argv[2] if len(sys.argv) >= 3 else None
        suffix = f"?tenant_id={tenant}" if tenant else ""
        with urllib.request.urlopen(f"{AEGIS_URL}/ham/stats{suffix}", timeout=10) as resp:
            out = json.loads(resp.read())
    elif op == "admin_release":
        aid, reason = sys.argv[2], sys.argv[3]
        token = os.environ.get("AEGIS_ADMIN_TOKEN", "dev-admin-token")
        out = _post("/admin/aid/release", {"aid": aid, "reason": reason},
                    headers={"X-Aegis-Admin-Token": token})
    else:
        print(f"unknown op: {op}", file=sys.stderr)
        return 2

    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
