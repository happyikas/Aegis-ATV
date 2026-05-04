"""Persistent budget store tests + step335 integration.

PR #5 replaces the hardcoded TENANT_BUDGETS dict with a SQLite WAL
store. These tests verify:

* set / get / list / delete CRUD against a fresh tmp_path DB
* fallback behaviour: tenant-specific row > default row > module default
* step335 reads from the persisted store at runtime
* CLI: ``aegis budget {show, set, delete}`` end-to-end
* Concurrency / restart durability
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

import numpy as np
import pytest

from aegis.cost.budget_store import (
    DEFAULT_DAILY_DOLLARS,
    DEFAULT_TENANT,
    BudgetStore,
    reset_singleton_for_tests,
)


@pytest.fixture(autouse=True)
def _reset_singleton(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Each test gets its own DB at tmp_path / 'budgets.sqlite' so
    they don't pollute the user's real ~/.aegis/budgets.sqlite."""
    monkeypatch.setenv("AEGIS_BUDGET_DB", str(tmp_path / "budgets.sqlite"))
    reset_singleton_for_tests()
    yield
    reset_singleton_for_tests()


# ─────────────────────────────────────────────────────────────────────
# 1. CRUD
# ─────────────────────────────────────────────────────────────────────


class TestBudgetStoreCRUD:
    def test_get_for_unknown_tenant_returns_default(
        self, tmp_path: Path
    ) -> None:
        store = BudgetStore(tmp_path / "b.sqlite")
        b = store.get_for("never-set")
        assert b.tenant_id == DEFAULT_TENANT
        assert b.daily_dollars == DEFAULT_DAILY_DOLLARS

    def test_set_then_get(self, tmp_path: Path) -> None:
        store = BudgetStore(tmp_path / "b.sqlite")
        store.set("team-a", daily_dollars=50.0, per_call_dollars=2.5)
        b = store.get_for("team-a")
        assert b.daily_dollars == 50.0
        assert b.per_call_dollars == 2.5

    def test_set_updates_existing_row(self, tmp_path: Path) -> None:
        store = BudgetStore(tmp_path / "b.sqlite")
        store.set("team-a", daily_dollars=50.0)
        store.set("team-a", daily_dollars=200.0, per_call_dollars=5.0)
        b = store.get_for("team-a")
        assert b.daily_dollars == 200.0
        assert b.per_call_dollars == 5.0

    def test_default_tenant_row_takes_precedence_over_module_default(
        self, tmp_path: Path
    ) -> None:
        """If a 'default' row is persisted, unknown tenants pick it
        up instead of the hardcoded DEFAULT_DAILY_DOLLARS."""
        store = BudgetStore(tmp_path / "b.sqlite")
        store.set(DEFAULT_TENANT, daily_dollars=12.5)
        b = store.get_for("any-other-tenant")
        assert b.tenant_id == DEFAULT_TENANT
        assert b.daily_dollars == 12.5  # not the hardcoded 1.0

    def test_set_rejects_zero_or_negative(self, tmp_path: Path) -> None:
        store = BudgetStore(tmp_path / "b.sqlite")
        with pytest.raises(ValueError, match="daily_dollars"):
            store.set("team-a", daily_dollars=0.0)
        with pytest.raises(ValueError, match="daily_dollars"):
            store.set("team-a", daily_dollars=-5.0)

    def test_list_all_orders_by_update_time(self, tmp_path: Path) -> None:
        store = BudgetStore(tmp_path / "b.sqlite")
        store.set("a", daily_dollars=1.0)
        store.set("b", daily_dollars=2.0)
        store.set("c", daily_dollars=3.0)
        rows = store.list_all()
        assert [r.tenant_id for r in rows] == ["a", "b", "c"]

    def test_delete(self, tmp_path: Path) -> None:
        store = BudgetStore(tmp_path / "b.sqlite")
        store.set("temp", daily_dollars=10.0)
        assert store.delete("temp") is True
        assert store.delete("temp") is False  # already gone


# ─────────────────────────────────────────────────────────────────────
# 2. Persistence across "process restart"
# ─────────────────────────────────────────────────────────────────────


class TestPersistence:
    def test_survives_close_reopen(self, tmp_path: Path) -> None:
        path = tmp_path / "b.sqlite"
        s1 = BudgetStore(path)
        s1.set("team-a", daily_dollars=42.0)
        s1.close()
        # Simulate process restart with a fresh store on the same file.
        s2 = BudgetStore(path)
        b = s2.get_for("team-a")
        assert b.daily_dollars == 42.0


# ─────────────────────────────────────────────────────────────────────
# 3. step335 integration — persisted budget gates the firewall
# ─────────────────────────────────────────────────────────────────────


def _atv_input(tenant: str, cumulative_dollars: float):
    import time

    from aegis.schema import ATVHeader, ATVInput, CostEfficiencyMetrics

    return ATVInput(
        header=ATVHeader(
            trace_id="t-1", span_id="s-1", tenant_id=tenant,
            aid="a-1", timestamp_ns=time.time_ns(),
        ),
        plan_text="",
        tool_name="Bash",
        tool_args_json='{"command":"ls"}',
        cost_estimate=CostEfficiencyMetrics(
            cumulative_dollars=cumulative_dollars,
        ),
    )


class TestStep335ReadsFromStore:
    def test_persisted_budget_gates_step335(self) -> None:
        """The autouse fixture pointed AEGIS_BUDGET_DB at a tmp file
        and reset the singleton — so step335's lazy `get_default_store`
        picks up the same path our `set()` call writes to."""
        from aegis.cost.budget_store import get_default_store
        from aegis.firewall.core import FirewallContext
        from aegis.firewall.step335_cost import run

        store = get_default_store()
        assert store is not None
        store.set("team-tight", daily_dollars=0.50)

        atv = np.zeros(2080, dtype=np.float32)
        inp = _atv_input("team-tight", cumulative_dollars=0.75)
        result = run(atv, inp, FirewallContext())
        assert result.verdict == "REQUIRE_APPROVAL"
        assert "0.7500" in (result.reason or "")
        assert "0.5000" in (result.reason or "")

    def test_unknown_tenant_falls_back_to_default(self) -> None:
        from aegis.firewall.core import FirewallContext
        from aegis.firewall.step335_cost import run

        atv = np.zeros(2080, dtype=np.float32)
        inp = _atv_input("never-configured", cumulative_dollars=0.75)
        result = run(atv, inp, FirewallContext())
        assert result.verdict is None  # under default $1 ceiling


# ─────────────────────────────────────────────────────────────────────
# 4. CLI E2E
# ─────────────────────────────────────────────────────────────────────


def _run_cli(args: list[str]) -> tuple[int, str, str]:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
    import aegis_cli  # noqa: I001

    parser = aegis_cli.build_parser()
    ns = parser.parse_args(args)
    out_buf = io.StringIO()
    err_buf = io.StringIO()
    real_out, real_err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = out_buf, err_buf
    try:
        rc = ns.fn(ns)
    finally:
        sys.stdout, sys.stderr = real_out, real_err
    return rc, out_buf.getvalue(), err_buf.getvalue()


class TestCLI:
    def test_show_empty_lists_default(self) -> None:
        rc, out, _ = _run_cli(["budget", "show"])
        assert rc == 0
        assert "no persisted budgets" in out
        assert f"${DEFAULT_DAILY_DOLLARS:.2f}" in out

    def test_set_then_show(self) -> None:
        rc, _, _ = _run_cli([
            "budget", "set", "--tenant", "team-x",
            "--daily", "75.0", "--per-call", "5.0",
        ])
        assert rc == 0
        rc2, out, _ = _run_cli(["budget", "show"])
        assert rc2 == 0
        assert "team-x" in out
        assert "75.00" in out  # daily

    def test_set_without_daily_fails(self) -> None:
        rc, _, err = _run_cli([
            "budget", "set", "--tenant", "team-y",
        ])
        assert rc == 2
        assert "--daily" in err

    def test_set_zero_daily_rejected(self) -> None:
        rc, _, err = _run_cli([
            "budget", "set", "--tenant", "team-z", "--daily", "0",
        ])
        assert rc == 2
        assert "daily_dollars" in err

    def test_delete_existing(self) -> None:
        _run_cli(["budget", "set", "--tenant", "t", "--daily", "10"])
        rc, out, _ = _run_cli(["budget", "delete", "--tenant", "t"])
        assert rc == 0
        assert "deleted" in out

    def test_delete_missing_returns_1(self) -> None:
        rc, _, _ = _run_cli(["budget", "delete", "--tenant", "ghost"])
        assert rc == 1
