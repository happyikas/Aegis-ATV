"""Model-file attestation — verify a GGUF (or other model artefact)
against a known SHA-256 before loading it into a native runtime.

Why
---

llama-cpp / vLLM / etc. parse model files in native code. A maliciously
crafted GGUF could exploit a parser CVE (there is a precedent: GGUF
heap overflow CVE-2024-21825 in llama.cpp), and HuggingFace mirrors
have historically been compromised. Aegis demos and any production
deployment should refuse to load a model whose hash doesn't match the
expected attested value.

Usage
-----

::

    from aegis.attest.model import (
        verify_gguf_attestation, KNOWN_MODELS, AttestationError,
    )

    result = verify_gguf_attestation(
        path,
        repo_id="TheBloke/TinyLlama-1.1B-Chat-v1.0-GGUF",
        filename="tinyllama-1.1b-chat-v1.0.Q4_K_M.gguf",
    )
    if not result.valid:
        raise AttestationError(result.summary())

    # ... safe to hand to llama_cpp.Llama(model_path=str(path)) ...

The attestation result carries both the expected and actual hash so
the caller can record an audit-chain entry for the verification.

Adding a new model
------------------

Pin the SHA-256 in :data:`KNOWN_MODELS` after a manual review of the
HuggingFace repo (verify the upload date, contributor, and that the
file matches the source weights). The dict key is ``(repo_id,
filename)`` so the same filename in two different repos is treated as
two distinct entries.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

# ──────────────────────────────────────────────────────────────────────
# Pinned SHA-256s for known-good model artefacts
# ──────────────────────────────────────────────────────────────────────

# To add a model: download it, compute `shasum -a 256 <file>`, manually
# review the source repo for trust signals, then add the entry here in
# its own commit. Pin commit message: "attest: pin <repo>/<file> @ <sha>".
KNOWN_MODELS: dict[tuple[str, str], str] = {
    # demo/placement_advisor_llamacpp.py — TinyLlama-1.1B-Chat Q4_K_M.
    # Verified 2026-05-05 against TheBloke/TinyLlama-1.1B-Chat-v1.0-GGUF
    # at HuggingFace; 637 MB GGUF.
    (
        "TheBloke/TinyLlama-1.1B-Chat-v1.0-GGUF",
        "tinyllama-1.1b-chat-v1.0.Q4_K_M.gguf",
    ): (
        "9fecc3b3cd76bba89d504f29b616eedf7da85b96540e490ca5824d3f7d2776a0"
    ),
}


# ──────────────────────────────────────────────────────────────────────
# Result + error types
# ──────────────────────────────────────────────────────────────────────


class AttestationError(Exception):
    """Raised when a model's hash does not match its attested value.

    Caller is expected to refuse to load the model. Never catch this
    silently — a hash mismatch on a model file means either a
    legitimate update (in which case the pin needs review) or a
    supply-chain compromise.
    """


@dataclass(frozen=True)
class AttestationResult:
    """Outcome of one verification call. Caller can persist the
    expected/actual pair to the audit chain."""

    path: str
    repo_id: str
    filename: str
    expected_sha256: str | None       # None if no pin known
    actual_sha256: str
    valid: bool
    reason: str = ""
    extras: dict[str, str] = field(default_factory=dict)

    def summary(self) -> str:
        if self.valid:
            return (
                f"attestation OK: {self.filename} "
                f"matches pin @ {self.expected_sha256[:16] if self.expected_sha256 else '?'}…"
            )
        return (
            f"attestation FAILED for {self.filename}: "
            f"expected={(self.expected_sha256 or 'no-pin')[:16]}…, "
            f"actual={self.actual_sha256[:16]}…  "
            f"(reason: {self.reason})"
        )


# ──────────────────────────────────────────────────────────────────────
# Hashing
# ──────────────────────────────────────────────────────────────────────


def sha256_of_file(path: Path, *, chunk_size: int = 64 * 1024) -> str:
    """Stream-hash a file. Used internally by verify_gguf_attestation
    and exposed for callers that want the raw digest."""
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(chunk_size), b""):
            h.update(chunk)
    return h.hexdigest()


# ──────────────────────────────────────────────────────────────────────
# Public verifier
# ──────────────────────────────────────────────────────────────────────


def verify_gguf_attestation(
    path: Path,
    *,
    repo_id: str,
    filename: str,
    expected_sha256: str | None = None,
    strict: bool = True,
) -> AttestationResult:
    """Verify a model file's SHA-256 against the pinned known-good value.

    Parameters
    ----------
    path:
        Local path to the (already-downloaded) model file.
    repo_id:
        HuggingFace repo it came from, e.g.
        ``"TheBloke/TinyLlama-1.1B-Chat-v1.0-GGUF"``.
    filename:
        File within that repo, e.g.
        ``"tinyllama-1.1b-chat-v1.0.Q4_K_M.gguf"``.
    expected_sha256:
        Override the pinned value (useful in tests, or for a
        deployment that pins different revisions). Defaults to
        :data:`KNOWN_MODELS` lookup; ``None`` if the model isn't
        pinned. When ``None`` and ``strict=True``, the result is
        invalid with reason ``"no pin"``.
    strict:
        ``True`` (default): unpinned models are treated as
        attestation failures. ``False``: unpinned models are valid
        with a warning in the result extras (useful for development
        of new model pins).

    Returns
    -------
    AttestationResult — ``.valid`` is the gate the caller should check.
    Mismatch DOES NOT raise here; the caller decides whether to raise
    :class:`AttestationError` or downgrade to a warning. Centralising
    raise / warn at the call site lets demos differ from production.
    """
    actual = sha256_of_file(path)
    pin = (
        expected_sha256
        if expected_sha256 is not None
        else KNOWN_MODELS.get((repo_id, filename))
    )

    if pin is None:
        return AttestationResult(
            path=str(path),
            repo_id=repo_id,
            filename=filename,
            expected_sha256=None,
            actual_sha256=actual,
            valid=not strict,
            reason="no pin in KNOWN_MODELS" if strict else "unpinned (lenient mode)",
            extras={"strict": str(strict)},
        )

    valid = pin.lower() == actual.lower()
    return AttestationResult(
        path=str(path),
        repo_id=repo_id,
        filename=filename,
        expected_sha256=pin,
        actual_sha256=actual,
        valid=valid,
        reason="" if valid else "SHA-256 mismatch",
    )


def assert_gguf_attestation(
    path: Path,
    *,
    repo_id: str,
    filename: str,
    expected_sha256: str | None = None,
) -> AttestationResult:
    """Stricter form: raise :class:`AttestationError` on any failure.

    Use this in production / demo entry points where a non-matching
    hash MUST stop execution. Returns the (valid) AttestationResult
    on success so the caller can still record the verification.
    """
    result = verify_gguf_attestation(
        path,
        repo_id=repo_id,
        filename=filename,
        expected_sha256=expected_sha256,
        strict=True,
    )
    if not result.valid:
        raise AttestationError(result.summary())
    return result
