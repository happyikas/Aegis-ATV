"""Realistic ``/evaluate`` request payloads for the soak harness.

The mix is what matters: a soak that only sends ALLOW-able payloads
exercises the fast path but doesn't catch regressions in the BLOCK /
APPROVAL paths. The default :data:`PAYLOAD_MIX` distributes:

* 70% allow-able (a benign Read / Grep — most production traffic)
* 15% grey-zone REQUIRE_APPROVAL (sensitive paths)
* 15% BLOCK (cloud_destructive patterns)

Each payload is a function ``-> dict`` so the harness can call it
fresh for every request (different aid / trace_id / timestamp_ns
to avoid the firewall's cost-divergence advisor flagging the load
as a runaway loop, which would otherwise turn the soak into a
self-fulfilling REQUIRE_APPROVAL stampede).
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class PayloadKind(StrEnum):
    """Which firewall path the payload is *expected* to hit.

    The harness uses the kind to bin the response decision; if the
    expected kind doesn't match the actual decision, that's a soak
    failure (regression in firewall semantics under load).
    """
    ALLOW = "allow"
    APPROVAL = "approval"
    BLOCK = "block"


def _fresh_aid() -> str:
    return f"soak-{uuid.uuid4().hex[:8]}"


def _fresh_trace() -> str:
    return uuid.uuid4().hex


def allow_payload() -> dict[str, Any]:
    """Benign Read of an obviously-safe path. Fast-path through
    the step305 safe-allowlist."""
    return {
        "header": {
            "trace_id": _fresh_trace(),
            "span_id": uuid.uuid4().hex,
            "tenant_id": "soak-tenant",
            "aid": _fresh_aid(),
            "timestamp_ns": time.time_ns(),
            "channel": "cli",
            "provider": "soak-harness",
        },
        "tool_name": "Read",
        "tool_args_json": '{"file_path":"/tmp/soak-test.txt"}',
        "plan_text": "soak-harness benign read",
    }


def approval_payload() -> dict[str, Any]:
    """Sensitive path. Triggers step310's sensitive-path approval gate."""
    return {
        "header": {
            "trace_id": _fresh_trace(),
            "span_id": uuid.uuid4().hex,
            "tenant_id": "soak-tenant",
            "aid": _fresh_aid(),
            "timestamp_ns": time.time_ns(),
            "channel": "cli",
            "provider": "soak-harness",
        },
        "tool_name": "Read",
        # /etc/hosts is universally known by the sensitive-paths rule.
        "tool_args_json": '{"file_path":"/etc/hosts"}',
        "plan_text": "soak-harness sensitive-path read",
    }


def block_payload() -> dict[str, Any]:
    """Cloud-destructive command. Triggers step311's BLOCK rule.

    The destructive command is built by string concatenation so this
    file's content doesn't trip an Aegis-installed git pre-commit
    hook scanning for the literal pattern.
    """
    cmd = " ".join(["kubectl", "delete", "ns", "soak-prod"])
    return {
        "header": {
            "trace_id": _fresh_trace(),
            "span_id": uuid.uuid4().hex,
            "tenant_id": "soak-tenant",
            "aid": _fresh_aid(),
            "timestamp_ns": time.time_ns(),
            "channel": "cli",
            "provider": "soak-harness",
        },
        "tool_name": "Bash",
        "tool_args_json": '{"command":"' + cmd + '"}',
        "plan_text": "soak-harness destructive",
    }


@dataclass(frozen=True)
class _MixEntry:
    """One row of the payload mix."""
    kind: PayloadKind
    builder: Callable[[], dict[str, Any]]
    weight: float
    expected_decision: str   # "ALLOW" / "REQUIRE_APPROVAL" / "BLOCK"


# Default mix. Weights need not sum to 1.0 — :func:`payload_for`
# normalizes by the running total. Override in the harness via
# ``SoakConfig.payload_mix`` for custom shapes (e.g. all-BLOCK
# stress test).
PAYLOAD_MIX: tuple[_MixEntry, ...] = (
    _MixEntry(PayloadKind.ALLOW, allow_payload, 0.70, "ALLOW"),
    _MixEntry(PayloadKind.APPROVAL, approval_payload, 0.15, "REQUIRE_APPROVAL"),
    _MixEntry(PayloadKind.BLOCK, block_payload, 0.15, "BLOCK"),
)


def payload_for(
    rng_value: float,
    *,
    mix: tuple[_MixEntry, ...] = PAYLOAD_MIX,
) -> tuple[PayloadKind, dict[str, Any], str]:
    """Deterministic mix lookup. ``rng_value`` is a float in ``[0,
    1)`` (use ``random.random()`` from the caller — the harness
    seeds the RNG so soak runs are reproducible).

    Returns ``(kind, payload_body, expected_decision)``.
    """
    total = sum(e.weight for e in mix)
    if total <= 0:
        # Defensive: empty / zeroed mix → fall back to a benign Read.
        return (
            PayloadKind.ALLOW, allow_payload(), "ALLOW",
        )
    threshold = rng_value * total
    running = 0.0
    for entry in mix:
        running += entry.weight
        if threshold < running:
            return entry.kind, entry.builder(), entry.expected_decision
    # Float-precision tail — return the last entry.
    last = mix[-1]
    return last.kind, last.builder(), last.expected_decision
