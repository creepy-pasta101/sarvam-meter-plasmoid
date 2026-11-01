# SPDX-License-Identifier: MIT
"""Tests for sarvam_meter.rates (SPEC section 5)."""

import copy
import unittest

from sarvam_meter.rates import (
    DEFAULT_RATE_CARD,
    cost_for,
    rate_card,
    rate_known,
)


class RateCardTests(unittest.TestCase):
    def test_default_card_has_every_api(self):
        self.assertEqual(
            set(DEFAULT_RATE_CARD),
            {"chat", "stt", "tts", "translate", "transliterate", "lid", "other"},
        )

    def test_default_card_chat_models(self):
        self.assertEqual(
            set(DEFAULT_RATE_CARD["chat"]),
            {
                "default",
                "sarvam-105b",
                "sarvam-105b-conversations",
                "glm5.3",
                "glm5.3-flash",
                "gemma4",
                "deepseekv4.1-flash",
            },
        )

    def test_rate_card_without_overrides_is_a_copy(self):
        card = rate_card()
        self.assertEqual(card, DEFAULT_RATE_CARD)
        card["chat"]["sarvam-105b"]["in_per_million"] = 999.0
        self.assertEqual(DEFAULT_RATE_CARD["chat"]["sarvam-105b"]["in_per_million"], 15.0)

    def test_rate_card_deep_merges_overrides(self):
        card = rate_card({"chat": {"sarvam-105b": {"in_per_million": 30.0}}})
        entry = card["chat"]["sarvam-105b"]
        self.assertEqual(entry["in_per_million"], 30.0)
        self.assertEqual(entry["cached_in_per_million"], 5.0)
        self.assertEqual(entry["out_per_million"], 60.0)
        self.assertIn("default", card["chat"])
        self.assertEqual(DEFAULT_RATE_CARD["chat"]["sarvam-105b"]["in_per_million"], 15.0)

    def test_rate_card_adds_unknown_model(self):
        card = rate_card(
            {"chat": {"sarvam-1b": {"in_per_million": 2.0, "out_per_million": 8.0}}}
        )
        self.assertEqual(card["chat"]["sarvam-1b"]["in_per_million"], 2.0)
        self.assertEqual(cost_for("chat", "sarvam-1b", 1000000, "tokens", card), 2.0)

    def test_rate_card_rejects_non_dict(self):
        with self.assertRaises(ValueError):
            rate_card(["chat"])

    def test_default_card_is_not_aliased_by_cost_for(self):
        before = copy.deepcopy(DEFAULT_RATE_CARD)
        cost_for("chat", "sarvam-105b", 1000, "tokens")
        self.assertEqual(DEFAULT_RATE_CARD, before)


class ChatCostTests(unittest.TestCase):
    def test_sarvam_105b_total_token_cost(self):
        # 1500 tokens on sarvam-105b: 1500 * 15.0 / 1e6
        self.assertAlmostEqual(
            cost_for("chat", "sarvam-105b", 1500, "tokens"), 0.0225, places=9
        )

    def test_input_and_output_tokens_priced_at_list_price(self):
        # 1000 input + 500 output tokens cost 0.045 at list price
        # (1000 * 15.0 + 500 * 60.0) / 1e6.  cost_for() receives the combined
        # 1500-token total because SPEC section 8.7 pins the proxy's call shape
        # (quantity = in + out + cached, plus cached_quantity), so the split is
        # not available to this function; see the module docstring.
        in_cost = 1000 * 15.0 / 1e6
        out_cost = 500 * 60.0 / 1e6
        self.assertAlmostEqual(in_cost + out_cost, 0.045, places=9)
        self.assertAlmostEqual(
            cost_for("chat", "sarvam-105b", 1500, "tokens"), 0.0225, places=9
        )

    def test_cached_tokens_use_cached_rate(self):
        # 1500 tokens of which 1000 cached: (500 * 15 + 1000 * 5) / 1e6
        self.assertAlmostEqual(
            cost_for("chat", "sarvam-105b", 1500, "tokens", cached_quantity=1000),
            0.0125,
            places=9,
        )

    def test_all_cached(self):
        self.assertAlmostEqual(
            cost_for("chat", "sarvam-105b", 2000, "tokens", cached_quantity=2000),
            0.01,
            places=9,
        )

    def test_cached_quantity_is_clamped_to_quantity(self):
        self.assertAlmostEqual(
            cost_for("chat", "sarvam-105b", 100, "tokens", cached_quantity=500),
            0.0005,
            places=9,
        )

    def test_model_prices(self):
        self.assertAlmostEqual(
            cost_for("chat", "glm5.3", 1000, "tokens"), 0.126, places=9
        )
        self.assertAlmostEqual(
            cost_for("chat", "glm5.3-flash", 2000, "tokens"), 0.027, places=9
        )
        self.assertAlmostEqual(cost_for("chat", "gemma4", 1000, "tokens"), 0.035, places=9)
        self.assertAlmostEqual(
            cost_for("chat", "deepseekv4.1-flash", 1000, "tokens"), 0.025, places=9
        )
        self.assertAlmostEqual(
            cost_for("chat", "sarvam-105b-conversations", 1000000, "tokens"), 15.0, places=9
        )

    def test_unknown_model_falls_back_to_default(self):
        self.assertAlmostEqual(
            cost_for("chat", "some-new-model", 1000000, "tokens"), 15.0, places=9
        )

    def test_none_model_uses_default(self):
        self.assertAlmostEqual(cost_for("chat", None, 1000000, "tokens"), 15.0, places=9)

    def test_zero_quantity_is_free(self):
        self.assertEqual(cost_for("chat", "sarvam-105b", 0, "tokens"), 0.0)


class OtherApiCostTests(unittest.TestCase):
    def test_stt_prices_seconds_per_minute(self):
        self.assertAlmostEqual(cost_for("stt", None, 60, "seconds"), 0.49, places=9)
        self.assertAlmostEqual(cost_for("stt", None, 90, "seconds"), 0.735, places=9)
        self.assertAlmostEqual(cost_for("stt", None, 0, "seconds"), 0.0, places=9)

    def test_stt_rounds_to_six_decimals(self):
        # 1 second = 0.49 / 60 = 0.0081666...
        self.assertEqual(cost_for("stt", None, 1, "seconds"), 0.008167)

    def test_tts_prices_chars_per_1k(self):
        self.assertAlmostEqual(cost_for("tts", None, 1000, "chars"), 3.0, places=9)
        self.assertAlmostEqual(cost_for("tts", None, 2500, "chars"), 7.5, places=9)
        self.assertAlmostEqual(cost_for("tts", None, 3, "chars"), 0.009, places=9)

    def test_tts_ignores_cached_quantity(self):
        self.assertAlmostEqual(
            cost_for("tts", None, 1000, "chars", cached_quantity=500), 3.0, places=9
        )

    def test_unpublished_prices_cost_nothing(self):
        self.assertEqual(cost_for("translate", None, 5000, "chars"), 0.0)
        self.assertEqual(cost_for("transliterate", None, 5000, "chars"), 0.0)
        self.assertEqual(cost_for("lid", None, 1, "requests"), 0.0)
        self.assertEqual(cost_for("other", None, 1, "requests"), 0.0)

    def test_unknown_api_costs_nothing(self):
        self.assertEqual(cost_for("embeddings", "whatever", 1000, "tokens"), 0.0)

    def test_override_making_price_unpublished(self):
        card = rate_card({"tts": {"default": {"per_1k_chars": None}}})
        self.assertEqual(cost_for("tts", None, 1000, "chars", card), 0.0)
        self.assertFalse(rate_known("tts", None, card))

    def test_override_replacing_stt_price(self):
        card = rate_card({"stt": {"default": {"per_minute": 1.0}}})
        self.assertAlmostEqual(cost_for("stt", None, 120, "seconds", card), 2.0, places=9)


class RateKnownTests(unittest.TestCase):
    def test_known_rates(self):
        self.assertTrue(rate_known("chat", "sarvam-105b"))
        self.assertTrue(rate_known("chat", "glm5.3"))
        self.assertTrue(rate_known("chat", "unknown-model"))  # default entry
        self.assertTrue(rate_known("chat", None))
        self.assertTrue(rate_known("stt", None))
        self.assertTrue(rate_known("tts", None))

    def test_unpublished_rates(self):
        self.assertFalse(rate_known("translate", None))
        self.assertFalse(rate_known("transliterate", None))
        self.assertFalse(rate_known("lid", None))
        self.assertFalse(rate_known("other", None))
        self.assertFalse(rate_known("embeddings", None))

    def test_custom_card_respected(self):
        card = rate_card({"chat": {"sarvam-105b": {"in_per_million": None}}})
        self.assertFalse(rate_known("chat", "sarvam-105b", card))
        self.assertEqual(cost_for("chat", "sarvam-105b", 1000, "tokens", card), 0.0)


class QuantityValidationTests(unittest.TestCase):
    def test_negative_quantity_rejected(self):
        with self.assertRaises(ValueError):
            cost_for("chat", "sarvam-105b", -1, "tokens")
        with self.assertRaises(ValueError):
            cost_for("stt", None, -0.5, "seconds")

    def test_non_finite_quantity_rejected(self):
        for bad in (float("nan"), float("inf"), float("-inf")):
            with self.assertRaises(ValueError):
                cost_for("tts", None, bad, "chars")

    def test_non_numeric_quantity_rejected(self):
        with self.assertRaises(ValueError):
            cost_for("chat", "sarvam-105b", "1000", "tokens")

    def test_negative_cached_quantity_rejected(self):
        with self.assertRaises(ValueError):
            cost_for("chat", "sarvam-105b", 1000, "tokens", cached_quantity=-1)

    def test_non_finite_cached_quantity_rejected(self):
        with self.assertRaises(ValueError):
            cost_for("chat", "sarvam-105b", 1000, "tokens", cached_quantity=float("nan"))

    def test_integer_quantities_accepted(self):
        self.assertAlmostEqual(cost_for("tts", None, 1000, "chars"), 3.0, places=9)


if __name__ == "__main__":
    unittest.main()
