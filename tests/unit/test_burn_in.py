"""Tests for software-emulated Burn-in measurement (PLAN Section 10)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aegis.attest.burn_in import compute_burn_in
from aegis.sign.ed25519 import load_or_create_key, load_public_key, verify


def _fake_pkg(root: Path) -> Path:
    """Create a tiny fake aegis package layout under root (created if missing)."""
    pkg = root / "aegis"
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text("__version__ = '0.0.0'\n")
    (pkg / "schema.py").write_text("ATV_VERSION = 'ATV-2080-v1'\nATV_DIM = 2080\n")
    sub = pkg / "firewall"
    sub.mkdir()
    (sub / "__init__.py").write_text("")
    (sub / "step310_args.py").write_text("def run(): pass\n")
    return pkg


def _fake_policy(root: Path) -> Path:
    pdir = root / "policies"
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "default.json").write_text(json.dumps({"deny": [], "allow": []}))
    return pdir


@pytest.fixture
def env(tmp_path: Path):
    code_root = _fake_pkg(tmp_path / "code")
    pol_dir = _fake_policy(tmp_path)
    key = load_or_create_key(tmp_path / "ed25519.pem")
    return {"code_root": code_root, "pol_dir": pol_dir, "key": key, "tmp": tmp_path}


def _measure(env, **overrides):
    args = dict(
        code_root=env["code_root"],
        policy_dir=env["pol_dir"],
        embedding_provider="dummy",
        judge_provider="dummy",
        public_key=env["key"].public_key(),
        signing_key=env["key"],
    )
    args.update(overrides)
    return compute_burn_in(**args)


class TestDeterminism:
    def test_same_inputs_same_burn_in_id(self, env: dict[str, object]) -> None:
        a = _measure(env)
        b = _measure(env)
        assert a.burn_in_id == b.burn_in_id

    def test_signature_verifies(self, env: dict[str, object]) -> None:
        m = _measure(env)
        # build a record-like dict the verify() helper accepts
        rec = {
            "payload": m.burn_in_id,  # signed message
            "signature": m.signature_hex,
        }
        # verify() expects payload as a dict for canonical JSON; here we
        # signed a raw string, so verify directly via cryptography
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        pub = serialization.load_pem_public_key(m.public_key_pem.encode())
        assert isinstance(pub, Ed25519PublicKey)
        pub.verify(bytes.fromhex(m.signature_hex), m.burn_in_id.encode())
        # The verify helper is unused here (it canonicalizes JSON); silence linter
        _ = verify
        _ = rec


class TestCodeMeasure:
    def test_code_change_changes_burn_in_id(self, env: dict[str, object]) -> None:
        before = _measure(env)
        # mutate one source file
        (env["code_root"] / "schema.py").write_text("ATV_VERSION = 'ATV-2080-v1'\nATV_DIM = 2080\n# tamper\n")  # type: ignore[union-attr]
        after = _measure(env)
        assert before.burn_in_id != after.burn_in_id
        assert before.layers["L3_code"]["hash"] != after.layers["L3_code"]["hash"]

    def test_pycache_files_ignored(self, env: dict[str, object]) -> None:
        before = _measure(env)
        cache = env["code_root"] / "__pycache__"  # type: ignore[union-attr]
        cache.mkdir()
        (cache / "schema.cpython-311.pyc").write_text("garbage")
        after = _measure(env)
        assert before.burn_in_id == after.burn_in_id

    def test_file_count_reported(self, env: dict[str, object]) -> None:
        m = _measure(env)
        assert m.layers["L3_code"]["files_counted"] == 4  # __init__, schema, fw __init__, step310


class TestConfigMeasure:
    def test_provider_swap_changes_burn_in(self, env: dict[str, object]) -> None:
        a = _measure(env, embedding_provider="dummy", judge_provider="dummy")
        b = _measure(env, embedding_provider="openai", judge_provider="haiku")
        assert a.burn_in_id != b.burn_in_id
        assert a.layers["L4_config"]["hash"] != b.layers["L4_config"]["hash"]

    def test_policy_change_changes_burn_in(self, env: dict[str, object]) -> None:
        a = _measure(env)
        # add a new policy file
        (env["pol_dir"] / "extra.json").write_text(json.dumps({"deny": [{"name": "x"}]}))  # type: ignore[union-attr]
        b = _measure(env)
        assert a.burn_in_id != b.burn_in_id
        assert "extra.json" in b.layers["L4_config"]["policies"]
        assert "extra.json" not in a.layers["L4_config"]["policies"]


class TestKeyBinding:
    def test_different_key_different_l5(self, env: dict[str, object], tmp_path: Path) -> None:
        a = _measure(env)
        other = load_or_create_key(tmp_path / "other.pem")
        b = _measure(env, public_key=other.public_key(), signing_key=other)
        assert a.layers["L5_key_binding"]["hash"] != b.layers["L5_key_binding"]["hash"]
        assert a.burn_in_id != b.burn_in_id

    def test_public_key_pem_loadable(self, env: dict[str, object], tmp_path: Path) -> None:
        m = _measure(env)
        pub_path = tmp_path / "loaded.pub"
        pub_path.write_bytes(m.public_key_pem.encode())
        # Should load without error
        _ = load_public_key(pub_path)


class TestAttestationDoc:
    def test_to_dict_shape(self, env: dict[str, object]) -> None:
        m = _measure(env)
        d = m.to_dict()
        assert d["burn_in_id"] == m.burn_in_id
        assert set(d["layers"].keys()) == {
            "L1_hardware_ek",
            "L2_firmware",
            "L3_code",
            "L4_config",
            "L5_key_binding",
        }
        assert d["layers"]["L1_hardware_ek"]["present"] is False
        assert d["layers"]["L2_firmware"]["present"] is False
        assert d["signed"]["algorithm"] == "Ed25519"
        assert d["signed"]["signed_field"] == "burn_in_id"
