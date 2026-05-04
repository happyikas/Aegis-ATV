"""Cache-aware billing pricing — verifies Anthropic's per-class rates.

The :mod:`aegis.cost.pricing` table is the dollar source-of-truth for
``aegis cost summary`` / ``aegis cost replay`` user-facing numbers,
so wrong rates ship wrong invoices to operators. These tests pin the
rates (so a typo in the table fails a test) and verify the cache
discount math against hand-computed expected values.
"""

from __future__ import annotations

import pytest

from aegis.cost.pricing import (
    PRICING_TABLE,
    billed_dollars,
    get_rates,
)


class TestPricingTable:
    @pytest.mark.parametrize(
        "model,expected_input,expected_output",
        [
            ("claude-haiku-4-5",            0.80,  4.00),
            ("claude-haiku-4-5-20251001",   0.80,  4.00),
            ("claude-sonnet-4-5",           3.00, 15.00),
            ("claude-sonnet-4-6",           3.00, 15.00),
            ("claude-opus-4-7",            15.00, 75.00),
        ],
    )
    def test_rates_pinned_in_table(
        self,
        model: str,
        expected_input: float,
        expected_output: float,
    ) -> None:
        rates = get_rates(model)
        assert rates.input_per_mtok == pytest.approx(expected_input)
        assert rates.output_per_mtok == pytest.approx(expected_output)

    def test_unknown_model_falls_back_to_default(self) -> None:
        rates = get_rates("gpt-5-something-future")
        assert rates is PRICING_TABLE["default"]

    def test_cache_read_is_10_percent_of_input(self) -> None:
        """Anthropic charges 10 % of input rate for cache reads."""
        for model_name, rates in PRICING_TABLE.items():
            ratio = rates.cache_read_per_mtok / rates.input_per_mtok
            assert 0.05 <= ratio <= 0.15, (
                f"{model_name}: cache_read should be ~10 % of input, "
                f"got {ratio:.2%}"
            )

    def test_cache_creation_is_125_percent_of_input(self) -> None:
        """Anthropic charges 125 % of input rate for 5-min cache creation."""
        for model_name, rates in PRICING_TABLE.items():
            ratio = rates.cache_creation_per_mtok / rates.input_per_mtok
            assert 1.10 <= ratio <= 1.40, (
                f"{model_name}: cache_creation should be ~125 % of input, "
                f"got {ratio:.2%}"
            )


class TestBilledDollars:
    def test_zero_tokens_zero_dollars(self) -> None:
        assert billed_dollars(model_name="claude-haiku-4-5") == 0.0

    def test_pure_input_at_haiku_rate(self) -> None:
        # 1M input tokens at Haiku → exactly $0.80
        assert billed_dollars(
            model_name="claude-haiku-4-5",
            input_tokens=1_000_000,
        ) == pytest.approx(0.80)

    def test_pure_output_at_haiku_rate(self) -> None:
        # 1M output tokens at Haiku → exactly $4.00
        assert billed_dollars(
            model_name="claude-haiku-4-5",
            output_tokens=1_000_000,
        ) == pytest.approx(4.00)

    def test_cache_read_discount_applied(self) -> None:
        """1M cache_read tokens at Haiku → $0.08 (NOT $0.80)."""
        cost = billed_dollars(
            model_name="claude-haiku-4-5",
            cache_read_tokens=1_000_000,
        )
        assert cost == pytest.approx(0.08)
        # Without the discount it would be 10× higher.
        assert cost < 0.80

    def test_cache_creation_premium_applied(self) -> None:
        """1M cache_creation tokens at Haiku → $1.00 (input × 1.25)."""
        cost = billed_dollars(
            model_name="claude-haiku-4-5",
            cache_creation_tokens=1_000_000,
        )
        assert cost == pytest.approx(1.00)
        assert cost > 0.80

    def test_realistic_session_mix(self) -> None:
        """A typical Claude Code turn:
          100 fresh input tokens
          5 000 cache_read tokens
          200 cache_creation tokens
          50 output tokens
        Expected (Sonnet 4.6 rates):
          input=100/1M × $3.00       = 0.000300
          cache_read=5000/1M × $0.30 = 0.001500
          cache_creation=200/1M × $3.75 = 0.000750
          output=50/1M × $15.00      = 0.000750
          total                      ≈ 0.003300
        """
        cost = billed_dollars(
            model_name="claude-sonnet-4-6",
            input_tokens=100,
            output_tokens=50,
            cache_read_tokens=5_000,
            cache_creation_tokens=200,
        )
        assert cost == pytest.approx(0.0033, abs=1e-6)

    def test_cache_aware_estimate_much_lower_than_full_input_proxy(
        self,
    ) -> None:
        """If you mis-bucket cache_read at full input rate (the bug
        PR #1 fixes), the estimate is roughly 10× too high."""
        # Real (cache-aware): 1M cache_read at $0.08 = $0.08
        real = billed_dollars(
            model_name="claude-haiku-4-5",
            cache_read_tokens=1_000_000,
        )
        # Mis-bucketed (treats cache_read as full input): 1M × $0.80 = $0.80
        mis_bucketed = billed_dollars(
            model_name="claude-haiku-4-5",
            input_tokens=1_000_000,   # cache_read shoved into input
        )
        assert real == pytest.approx(0.08)
        assert mis_bucketed == pytest.approx(0.80)
        assert mis_bucketed / real == pytest.approx(10.0)
