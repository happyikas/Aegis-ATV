"""Tests for ``aegis.attest.model`` — GGUF SHA-256 attestation.

Verifies that a model file's hash is checked against a pinned
known-good value before the file is handed to a native model
parser (llama-cpp etc.). Mismatch must NOT silently fall through —
a future supply-chain compromise of the upstream HuggingFace mirror
must surface here.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from aegis.attest.model import (
    KNOWN_MODELS,
    AttestationError,
    AttestationResult,
    assert_gguf_attestation,
    sha256_of_file,
    verify_gguf_attestation,
)


def _make_blob(path: Path, payload: bytes) -> str:
    """Write ``payload`` to ``path`` and return its SHA-256."""
    path.write_bytes(payload)
    return hashlib.sha256(payload).hexdigest()


# ──────────────────────────────────────────────────────────────────────
# sha256_of_file
# ──────────────────────────────────────────────────────────────────────


class TestStreamHash:
    def test_matches_hashlib_one_shot(self, tmp_path: Path) -> None:
        path = tmp_path / "blob.bin"
        payload = b"hello\nworld" * 1000
        expected = _make_blob(path, payload)
        assert sha256_of_file(path) == expected

    def test_handles_large_file(self, tmp_path: Path) -> None:
        # 2 MB — exercises the chunk loop.
        path = tmp_path / "big.bin"
        payload = bytes(range(256)) * 8192   # 2 MiB
        expected = _make_blob(path, payload)
        assert sha256_of_file(path) == expected

    def test_empty_file_hash(self, tmp_path: Path) -> None:
        path = tmp_path / "empty.bin"
        path.write_bytes(b"")
        # SHA-256 of empty input is a well-known constant.
        assert sha256_of_file(path) == hashlib.sha256(b"").hexdigest()


# ──────────────────────────────────────────────────────────────────────
# verify_gguf_attestation
# ──────────────────────────────────────────────────────────────────────


class TestVerify:
    def test_match_yields_valid(self, tmp_path: Path) -> None:
        path = tmp_path / "model.gguf"
        sha = _make_blob(path, b"fake-gguf-payload")
        result = verify_gguf_attestation(
            path,
            repo_id="example/repo",
            filename="model.gguf",
            expected_sha256=sha,
        )
        assert result.valid is True
        assert result.actual_sha256 == sha
        assert result.expected_sha256 == sha
        assert result.reason == ""

    def test_mismatch_yields_invalid(self, tmp_path: Path) -> None:
        path = tmp_path / "model.gguf"
        _make_blob(path, b"fake-gguf-payload")
        result = verify_gguf_attestation(
            path,
            repo_id="example/repo",
            filename="model.gguf",
            expected_sha256="0" * 64,           # forged pin
        )
        assert result.valid is False
        assert "mismatch" in result.reason.lower()
        # Both hashes still surfaced for debugging.
        assert result.expected_sha256 == "0" * 64
        assert result.actual_sha256 != "0" * 64

    def test_unpinned_strict_mode_invalid(
        self, tmp_path: Path,
    ) -> None:
        path = tmp_path / "model.gguf"
        _make_blob(path, b"fake-gguf-payload")
        # No expected_sha256 supplied AND not in KNOWN_MODELS.
        result = verify_gguf_attestation(
            path,
            repo_id="totally/unknown",
            filename="model.gguf",
        )
        assert result.valid is False
        assert "no pin" in result.reason.lower()

    def test_unpinned_lenient_mode_valid(
        self, tmp_path: Path,
    ) -> None:
        path = tmp_path / "model.gguf"
        _make_blob(path, b"fake-gguf-payload")
        result = verify_gguf_attestation(
            path,
            repo_id="totally/unknown",
            filename="model.gguf",
            strict=False,
        )
        assert result.valid is True
        assert "lenient" in result.reason.lower()

    def test_explicit_pin_overrides_known_models(
        self, tmp_path: Path,
    ) -> None:
        # Use a known repo/filename but pass an explicit pin that
        # matches the actual file → that pin should be used,
        # NOT the (different) one in KNOWN_MODELS.
        path = tmp_path / "model.gguf"
        sha = _make_blob(path, b"different-payload")
        # Pick the first KNOWN_MODELS entry to "shadow".
        (repo_id, filename) = next(iter(KNOWN_MODELS.keys()))
        result = verify_gguf_attestation(
            path,
            repo_id=repo_id,
            filename=filename,
            expected_sha256=sha,
        )
        assert result.valid is True
        assert result.expected_sha256 == sha


# ──────────────────────────────────────────────────────────────────────
# assert_gguf_attestation
# ──────────────────────────────────────────────────────────────────────


class TestAssert:
    def test_raises_on_mismatch(self, tmp_path: Path) -> None:
        path = tmp_path / "model.gguf"
        _make_blob(path, b"fake-gguf-payload")
        with pytest.raises(AttestationError) as exc_info:
            assert_gguf_attestation(
                path,
                repo_id="example/repo",
                filename="model.gguf",
                expected_sha256="0" * 64,
            )
        assert "FAILED" in str(exc_info.value)

    def test_returns_result_on_match(self, tmp_path: Path) -> None:
        path = tmp_path / "model.gguf"
        sha = _make_blob(path, b"fake-gguf-payload")
        result = assert_gguf_attestation(
            path,
            repo_id="example/repo",
            filename="model.gguf",
            expected_sha256=sha,
        )
        assert isinstance(result, AttestationResult)
        assert result.valid is True

    def test_raises_on_unpinned(self, tmp_path: Path) -> None:
        path = tmp_path / "model.gguf"
        _make_blob(path, b"fake-gguf-payload")
        with pytest.raises(AttestationError):
            assert_gguf_attestation(
                path,
                repo_id="totally/unknown",
                filename="model.gguf",
            )


# ──────────────────────────────────────────────────────────────────────
# KNOWN_MODELS registry
# ──────────────────────────────────────────────────────────────────────


class TestRegistry:
    def test_tinyllama_pin_is_64_hex_chars(self) -> None:
        # The TinyLlama pin must be exactly 64 hex chars
        # (SHA-256 output length).
        sha = KNOWN_MODELS[(
            "TheBloke/TinyLlama-1.1B-Chat-v1.0-GGUF",
            "tinyllama-1.1b-chat-v1.0.Q4_K_M.gguf",
        )]
        assert len(sha) == 64
        assert all(c in "0123456789abcdef" for c in sha)

    def test_summary_redacts_full_hash(self) -> None:
        # AttestationResult.summary() must NOT log the full SHA —
        # 16 chars is enough to identify, full would clutter logs.
        result = AttestationResult(
            path="/tmp/x.gguf",
            repo_id="a/b",
            filename="x.gguf",
            expected_sha256="9fecc3b3cd76bba89d504f29b616eedf7da85b96540e490ca5824d3f7d2776a0",
            actual_sha256="9fecc3b3cd76bba89d504f29b616eedf7da85b96540e490ca5824d3f7d2776a0",
            valid=True,
        )
        s = result.summary()
        # Truncated form present, full not.
        assert "9fecc3b3cd76bba8" in s
        # 64-char form should NOT appear in the summary.
        assert (
            "9fecc3b3cd76bba89d504f29b616eedf7da85b96540e490ca5824d3f7d2776a0"
            not in s
        )
