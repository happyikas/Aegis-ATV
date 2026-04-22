"""Build an ATVInput JSON payload for the scenario scripts.

Usage:
    python3 _payload.py <tool_name> <tool_args_json> <aid>
                        [--role ROLE]
                        [--safety KEY=VAL,KEY=VAL]
                        [--cost KEY=VAL,KEY=VAL]
                        [--plan TEXT]
                        [--tenant TENANT]
                        [--with-extras EXTRA_JSON]

Outputs the JSON payload to stdout. Used by scenario_*.sh scripts so
no fragile bash heredoc handling of strings with quotes/$/etc is
required.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import uuid


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("tool_name")
    ap.add_argument("tool_args_json")
    ap.add_argument("aid")
    ap.add_argument("--role", default="")
    ap.add_argument("--safety", default="")
    ap.add_argument("--cost", default="")
    ap.add_argument("--plan", default="scenario demo step")
    ap.add_argument("--tenant", default="demo-tenant")
    args = ap.parse_args()

    safety: dict[str, float] = {}
    if args.safety:
        for kv in args.safety.split(","):
            k, _, v = kv.partition("=")
            safety[k.strip()] = float(v)

    cost: dict[str, float] = {
        "input_token_count": 100,
        "cumulative_dollars": 0.001,
        "forecasted_cost_to_completion": 0.01,
    }
    if args.cost:
        for kv in args.cost.split(","):
            k, _, v = kv.partition("=")
            cost[k.strip()] = float(v)

    body: dict = {
        "header": {
            "trace_id": str(uuid.uuid4()),
            "span_id": str(uuid.uuid4()),
            "tenant_id": args.tenant,
            "aid": args.aid,
            "ats": "ATV-2080-v1",
            "schema_version": "ATV-2080-v1",
            "tier_profile": "T2",
            "cost_attestation_profile": "software",
            "timestamp_ns": time.time_ns(),
        },
        "agent_state_text": "scenario demo agent",
        "plan_text": args.plan,
        "tool_name": args.tool_name,
        "tool_args_json": args.tool_args_json,
        "safety_flags": safety,
        "cost_estimate": cost,
    }
    if args.role:
        body["role_id"] = args.role

    print(json.dumps(body))
    return 0


if __name__ == "__main__":
    sys.exit(main())
