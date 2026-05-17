"""Pluggable policies loaded from ``policies/*.json``.

v0.5.22 introduces the reversibility classifier as the first
module here. Future policies (e.g. PII redaction, cost ceilings)
can join the same loader pattern."""

from aegis.policies.reversibility import (
    REVERSIBILITY_LEVELS,
    ReversibilityClassification,
    classify_reversibility,
    is_irreversible,
    reversibility_policy_path,
)

__all__ = [
    "REVERSIBILITY_LEVELS",
    "ReversibilityClassification",
    "classify_reversibility",
    "is_irreversible",
    "reversibility_policy_path",
]
