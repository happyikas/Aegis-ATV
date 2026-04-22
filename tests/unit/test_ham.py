"""Unit tests for the HAM store (M16 — patent §13A)."""

from __future__ import annotations

import secrets

import pytest

from aegis.ham import KEY_LEN, HierarchicalMemoryStore


@pytest.fixture
def store() -> HierarchicalMemoryStore:
    return HierarchicalMemoryStore(":memory:", secrets.token_bytes(KEY_LEN))


# ─────────────────────────────────────────────────────────────────────
# memory + recall
# ─────────────────────────────────────────────────────────────────────
class TestMemoryRecall:
    def test_memory_returns_object_id_and_seq(self, store) -> None:
        rec = store.memory(aid="a", tenant_id="t", body={"x": 1})
        assert rec["object_id"]
        assert rec["seq"] == 1
        assert rec["digest"]

    def test_seq_monotonic(self, store) -> None:
        a = store.memory(aid="a", tenant_id="t", body={"x": 1})
        b = store.memory(aid="a", tenant_id="t", body={"x": 2})
        assert b["seq"] == a["seq"] + 1

    def test_recall_returns_decrypted_bodies(self, store) -> None:
        store.memory(aid="a", tenant_id="t", body={"text": "hello"})
        items = store.recall(aid="a", tenant_id="t")
        assert len(items) == 1
        assert items[0]["body"]["text"] == "hello"

    def test_recall_orders_by_recency(self, store) -> None:
        for i in range(3):
            store.memory(aid="a", tenant_id="t", body={"i": i})
        items = store.recall(aid="a", tenant_id="t")
        # Newest first
        assert items[0]["body"]["i"] == 2
        assert items[-1]["body"]["i"] == 0

    def test_recall_isolates_per_aid(self, store) -> None:
        store.memory(aid="a", tenant_id="t", body={"x": 1})
        store.memory(aid="b", tenant_id="t", body={"x": 2})
        a_items = store.recall(aid="a", tenant_id="t")
        b_items = store.recall(aid="b", tenant_id="t")
        assert len(a_items) == 1 and a_items[0]["body"]["x"] == 1
        assert len(b_items) == 1 and b_items[0]["body"]["x"] == 2

    def test_recall_isolates_per_tenant(self, store) -> None:
        store.memory(aid="a", tenant_id="t1", body={"x": 1})
        store.memory(aid="a", tenant_id="t2", body={"x": 2})
        items = store.recall(aid="a", tenant_id="t1")
        assert len(items) == 1 and items[0]["body"]["x"] == 1

    def test_recall_filters_by_tag(self, store) -> None:
        store.memory(aid="a", tenant_id="t", body={"x": 1}, tags=["red"])
        store.memory(aid="a", tenant_id="t", body={"x": 2}, tags=["blue"])
        store.memory(aid="a", tenant_id="t", body={"x": 3}, tags=["red", "blue"])
        items = store.recall(aid="a", tenant_id="t", tags=["red"])
        bodies = sorted(it["body"]["x"] for it in items)
        assert bodies == [1, 3]


# ─────────────────────────────────────────────────────────────────────
# Encryption invariants
# ─────────────────────────────────────────────────────────────────────
class TestEncryption:
    def test_wrong_key_cant_decrypt(self) -> None:
        k1 = secrets.token_bytes(KEY_LEN)
        k2 = secrets.token_bytes(KEY_LEN)
        s1 = HierarchicalMemoryStore(":memory:", k1)
        # Insert with k1.
        rec = s1.memory(aid="a", tenant_id="t", body={"secret": "hi"})
        # Read raw row + try to decrypt with k2.
        s2 = HierarchicalMemoryStore(":memory:", k2)
        from cryptography.exceptions import InvalidTag
        with pytest.raises(InvalidTag):
            s2._decrypt(
                # Same row contents but we have to fish them out of s1.
                *_raw_nonce_ct(s1.conn, rec["object_id"]),
                aad=s2._aad_for("a", "t", rec["seq"]),
            )

    def test_invalid_key_length_rejected(self) -> None:
        with pytest.raises(ValueError):
            HierarchicalMemoryStore(":memory:", b"too-short")


def _raw_nonce_ct(conn, object_id: str) -> tuple[str, str]:
    row = conn.execute(
        "SELECT nonce, ciphertext FROM ham_objects WHERE object_id=?",
        (object_id,),
    ).fetchone()
    return row[0], row[1]


# ─────────────────────────────────────────────────────────────────────
# context / forget / summarize / ground
# ─────────────────────────────────────────────────────────────────────
class TestContext:
    def test_context_assembles_recent_items(self, store) -> None:
        for i in range(8):
            store.memory(aid="a", tenant_id="t", body={"i": i})
        ctx = store.context(aid="a", tenant_id="t", max_items=3)
        assert len(ctx["bundle"]["items"]) == 3
        assert len(ctx["source_ids"]) == 3
        # Newest first → i=7,6,5
        assert ctx["bundle"]["items"][0]["body"]["i"] == 7


class TestForget:
    def test_forget_tombstones_object(self, store) -> None:
        rec = store.memory(aid="a", tenant_id="t", body={"x": 1})
        ok = store.forget(object_id=rec["object_id"], aid="a", tenant_id="t")
        assert ok is True
        # Tombstoned items don't appear in recall.
        assert store.recall(aid="a", tenant_id="t") == []

    def test_forget_nonexistent_returns_false(self, store) -> None:
        ok = store.forget(object_id="never-existed", aid="a", tenant_id="t")
        assert ok is False

    def test_forget_isolates_per_tenant(self, store) -> None:
        rec = store.memory(aid="a", tenant_id="t1", body={"x": 1})
        # Try to forget from a different tenant — should be no-op.
        ok = store.forget(object_id=rec["object_id"], aid="a", tenant_id="t2")
        assert ok is False
        # Still visible to the right tenant.
        assert len(store.recall(aid="a", tenant_id="t1")) == 1


class TestSummarize:
    def test_summary_counts_and_tag_histogram(self, store) -> None:
        store.memory(aid="a", tenant_id="t", body={"x": 1}, tags=["red"])
        store.memory(aid="a", tenant_id="t", body={"x": 2}, tags=["red", "blue"])
        store.memory(aid="a", tenant_id="t", body={"x": 3}, tags=["green"])
        s = store.summarize(aid="a", tenant_id="t")
        assert s["item_count"] == 3
        assert s["tag_histogram"]["red"] == 2
        assert s["tag_histogram"]["blue"] == 1
        assert s["tag_histogram"]["green"] == 1


class TestGround:
    def test_ground_validates_references(self, store) -> None:
        a = store.memory(aid="a", tenant_id="t", body={"x": 1})
        b = store.memory(aid="a", tenant_id="t", body={"x": 2})
        bundle = store.ground(
            aid="a", tenant_id="t",
            claim="value of x ranges 1..2",
            reference_ids=[a["object_id"], b["object_id"], "fake-id"],
        )
        assert sorted(bundle["references"]) == sorted([a["object_id"], b["object_id"]])
        assert bundle["missing"] == ["fake-id"]
        assert bundle["claim_hash"]
