"""Software-emulated Five-Layer Burn-in measurement (PLAN Section 10).

A poor-man's TEE quote for a software-only deployment. Patterned after
Intel SGX's MRENCLAVE / MRSIGNER but produced by Python hashing of files
on disk + the public Ed25519 key:

    L1 (hardware EK)      — N/A in T2; placeholder layer for future
                            hardware root of trust.
    L2 (firmware measure) — N/A in T2; placeholder.
    L3 (code measure)     — SHA3-256 over a deterministic manifest of
                            the aegis package's .py files (sorted
                            relative paths + per-file SHA3-256).
    L4 (config measure)   — SHA3-256 over runtime config: ATV version,
                            embedding/judge provider names, and the
                            content hash of every policy JSON.
    L5 (key binding)      — SHA3-256( raw_pubkey || L3 || L4 ). Proves
                            "records signed by this Ed25519 key were
                            produced by software matching L3+L4".

The final ``burn_in_id`` is a SHA3-256 chained over (versions, L3, L4,
L5). The whole measurement is signed once at startup with the same
Ed25519 key used for audit signatures, so any external verifier can
confirm "the audit chain I'm reading was emitted by this exact code +
config".

CAVEAT: a software measurement is **not** a runtime integrity guarantee.
An attacker with write access to the process or its filesystem can mutate
code AFTER the measurement is computed and the measurement will not
notice. Real attestation requires a hardware root of trust (T3+). This
module exists to (a) wire the API/audit shape T3 will need and (b) catch
accidental drift between deployments.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from aegis import __version__
from aegis.schema import ATV_VERSION


def _sha3(data: bytes) -> str:
    return hashlib.sha3_256(data).hexdigest()


@dataclass(frozen=True)
class BurnInMeasurement:
    burn_in_id: str
    measured_at_ns: int
    aegis_version: str
    atv_version: str
    layers: dict[str, dict[str, Any]]
    public_key_pem: str
    signature_hex: str
    signature_algorithm: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "burn_in_id": self.burn_in_id,
            "measured_at_ns": self.measured_at_ns,
            "aegis_version": self.aegis_version,
            "atv_version": self.atv_version,
            "layers": self.layers,
            "public_key_pem": self.public_key_pem,
            "signed": {
                "signature": self.signature_hex,
                "algorithm": self.signature_algorithm,
                "signed_field": "burn_in_id",
            },
        }


def _hash_code_tree(root: Path) -> tuple[str, int]:
    """Hash all .py files under ``root`` (excluding __pycache__).

    Returns ``(hash, file_count)``. Hash is deterministic across machines:
    it depends only on the relative paths and per-file content, not on
    the absolute root path.
    """
    entries: list[tuple[str, str]] = []
    for p in sorted(root.rglob("*.py")):
        if not p.is_file() or "__pycache__" in p.parts:
            continue
        rel = str(p.relative_to(root))
        entries.append((rel, _sha3(p.read_bytes())))
    manifest = "\n".join(f"{path}:{h}" for path, h in entries).encode("utf-8")
    return _sha3(manifest), len(entries)


def _hash_policies(policy_dir: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not policy_dir.is_dir():
        return out
    for p in sorted(policy_dir.glob("*.json")):
        out[p.name] = _sha3(p.read_bytes())
    return out


def _hash_config(
    atv_version: str,
    embedding_provider: str,
    judge_provider: str,
    policies: dict[str, str],
) -> str:
    parts = [
        f"atv_version={atv_version}",
        f"embedding_provider={embedding_provider}",
        f"judge_provider={judge_provider}",
    ]
    for name, h in sorted(policies.items()):
        parts.append(f"policy:{name}={h}")
    return _sha3("\n".join(parts).encode("utf-8"))


def compute_burn_in(
    *,
    code_root: Path,
    policy_dir: Path,
    embedding_provider: str,
    judge_provider: str,
    public_key: Ed25519PublicKey,
    signing_key: Ed25519PrivateKey,
) -> BurnInMeasurement:
    """Produce + sign a fresh measurement. Run once at process startup."""

    pub_pem = public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    pub_raw = public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )

    l3_hash, n_files = _hash_code_tree(code_root)
    policies = _hash_policies(policy_dir)
    l4_hash = _hash_config(ATV_VERSION, embedding_provider, judge_provider, policies)
    l5_hash = _sha3(pub_raw + bytes.fromhex(l3_hash) + bytes.fromhex(l4_hash))

    burn_in_id = _sha3(
        (
            f"aegis={__version__}|atv={ATV_VERSION}|"
            f"L3={l3_hash}|L4={l4_hash}|L5={l5_hash}"
        ).encode()
    )

    layers: dict[str, dict[str, Any]] = {
        "L1_hardware_ek": {"present": False, "note": "T2 software-only"},
        "L2_firmware":    {"present": False, "note": "T2 software-only"},
        "L3_code": {
            "hash": l3_hash,
            "files_counted": n_files,
            "root": str(code_root),
        },
        "L4_config": {
            "hash": l4_hash,
            "atv_version": ATV_VERSION,
            "embedding_provider": embedding_provider,
            "judge_provider": judge_provider,
            "policies": policies,
        },
        "L5_key_binding": {
            "hash": l5_hash,
            "public_key_fingerprint": _sha3(pub_raw),
        },
    }

    signature = signing_key.sign(burn_in_id.encode("utf-8"))

    return BurnInMeasurement(
        burn_in_id=burn_in_id,
        measured_at_ns=time.time_ns(),
        aegis_version=__version__,
        atv_version=ATV_VERSION,
        layers=layers,
        public_key_pem=pub_pem.decode("utf-8"),
        signature_hex=signature.hex(),
        signature_algorithm="Ed25519",
    )
