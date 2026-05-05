"""Software-emulated Burn-in measurements + attestation (PLAN Section 10)."""

from aegis.attest.burn_in import BurnInMeasurement, compute_burn_in
from aegis.attest.model import (
    KNOWN_MODELS,
    AttestationError,
    AttestationResult,
    assert_gguf_attestation,
    sha256_of_file,
    verify_gguf_attestation,
)

__all__ = [
    "AttestationError",
    "AttestationResult",
    "BurnInMeasurement",
    "KNOWN_MODELS",
    "assert_gguf_attestation",
    "compute_burn_in",
    "sha256_of_file",
    "verify_gguf_attestation",
]
