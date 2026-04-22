#!/usr/bin/env python3
"""Export an aid's audit chain to JSONL for dogfood analysis.

Usage:
    python3 tools/dogfood/export_chain.py <aid> [<output.jsonl>]

Defaults to aid='claude-code-{session-prefix}' and writes to
data/dogfood/<aid>.jsonl.

The audit chain stores decision + tool + signature + Merkle link
per call (integrity) but NOT the verdict reason or full args
(privacy). Use this script to export decisions; pair with the
hook's stderr log for reasons.
"""
from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

AEGIS_URL = "http://localhost:8000"


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2

    aid = sys.argv[1]
    out_path = Path(sys.argv[2]) if len(sys.argv) >= 3 else Path(f"data/dogfood/{aid}.jsonl")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with urllib.request.urlopen(f"{AEGIS_URL}/audit/{aid}", timeout=10) as r:
        chain = json.loads(r.read())

    rows = []
    for i, rec in enumerate(chain.get("chain", []), 1):
        p = rec.get("payload", {})
        h = p.get("header", {})
        rows.append({
            "n": i,
            "atv_id": rec.get("atv_id"),
            "decision": rec.get("decision"),
            "tool_name": h.get("tool_name"),
            "timestamp_ns": h.get("timestamp_ns"),
            "tier_profile": h.get("tier_profile"),
            "signature_hex": rec.get("signature", "")[:16] + "…",
            "this_hash": rec.get("this_hash"),
            "prev_hash": p.get("prev_hash"),
            "atv_sha3_256": p.get("atv_sha3_256"),
        })

    with out_path.open("w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")

    # Summary
    counts: dict[str, int] = {}
    by_tool: dict[str, dict[str, int]] = {}
    for r in rows:
        d = r["decision"] or "?"
        t = r["tool_name"] or "?"
        counts[d] = counts.get(d, 0) + 1
        by_tool.setdefault(t, {}).setdefault(d, 0)
        by_tool[t][d] += 1

    print(f"chain exported: aid={aid}  len={chain['length']}  chain_valid={chain['chain_valid']}")
    print(f"head: {chain.get('head', '?')[:40]}…")
    print(f"output: {out_path}")
    print()
    print("decisions:")
    for k in sorted(counts.keys()):
        print(f"  {k:18}  {counts[k]:3}")
    print()
    print("by tool:")
    for tool in sorted(by_tool.keys()):
        items = "  ".join(f"{d}={n}" for d, n in sorted(by_tool[tool].items()))
        print(f"  {tool:18}  {items}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
