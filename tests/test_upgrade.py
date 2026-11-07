"""Making an item instead of buying it made: quality reroll expectations and the craft cost answer. Synthetic data only."""
from datetime import datetime, timedelta, UTC
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

import gamedata
import upgrade
from market import Store

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 11, 7, 12, tzinfo=UTC)
ITEMS = {"T7_TESTSWORD": {"w": 22.8, "f": 1.0, "v": [4096.0, 8192.0, 16384.0, 32768.0],
                          "up": [[["T7_RUNE", 384]], [["T7_SOUL", 384]], [["T7_RELIC", 384]]]},
         "T7_RUNE": {"w": 0.1, "f": 1.0}}


class RerollTests(unittest.TestCase):
    def test_nothing_to_do_at_or_above_the_target(self):
        self.assertEqual(upgrade.reroll(4096, 3, 3), {"silver": 0.0, "rolls": 0.0})

    def test_good_to_outstanding_counts_the_rerolls_that_change_nothing(self):
        # From Good, 70% moves to Outstanding or better and 30% stays: on average 1/0.7 rerolls at 5.5 x value.
        r = upgrade.reroll(4096, 2, 3)
        self.assertAlmostEqual(r["rolls"], 1 / 0.7)
        self.assertAlmostEqual(r["silver"], 4096 * 5.5 / 0.7)

    def test_normal_to_outstanding_goes_through_good_on_most_rolls(self):
        r, good = upgrade.reroll(4096, 1, 3), 4096 * 5.5 / 0.7
        to_good = 0.80 / 1.001  # Normal's row is scaled to 100%
        self.assertAlmostEqual(r["silver"], 4096 * 4.4 + to_good * good)
        self.assertAlmostEqual(r["rolls"], 1 + to_good / 0.7)

    def test_excellent_to_masterpiece_is_rare_and_dear(self):
        r = upgrade.reroll(1000, 4, 5)
        self.assertAlmostEqual(r["rolls"], 200)
        self.assertAlmostEqual(r["silver"], 1000 * 27.5 * 200)

    def test_scale_multiplies_the_price_not_the_count(self):
        plain, scaled = upgrade.reroll(4096, 1, 3), upgrade.reroll(4096, 1, 3, scale=0.8)
        self.assertAlmostEqual(scaled["silver"], plain["silver"] * 0.8)
        self.assertAlmostEqual(scaled["rolls"], plain["rolls"])


def view(sell=None, buy=None, city="1002", seen=NOW):
    """A lookup_item answer with one city's newest orders heard (the public server knows nothing)."""
    local = {}
    if sell is not None:
        local["sell"] = {"price": sell, "amount": 5, "seen": seen.isoformat(), "mine": False}
    if buy is not None:
        local["buy"] = {"price": buy, "amount": 5, "seen": seen.isoformat(), "mine": False}
    return {"cities": [{"city": city, "local": local, "public": None}], "public_loading": False}


class CraftCostTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        shutil.copy(ROOT / "config.example.json", self.root / "config.json")
        self.store = Store(self.root, clock=lambda: NOW)
        self.store.cfg.update(setup_fee=0.025, reroll_cost_scale=1.0)
        self.saved, gamedata.catalog.items = gamedata.catalog.items, ITEMS
        self.prices = {}

    def tearDown(self):
        gamedata.catalog.items = self.saved
        self.store.db.close()
        self.tmp.cleanup()

    def cost(self, item, quality):
        with patch.object(self.store, "lookup_item", lambda i, q: self.prices.get((i, q), view())):
            return self.store.craft_cost(item, quality)

    def test_picks_the_cheapest_start_and_adds_reroll_and_each_steps_materials(self):
        self.prices = {("T7_TESTSWORD", 1): view(sell=200_000), ("T7_TESTSWORD", 2): view(sell=240_000), ("T7_TESTSWORD", 3): view(sell=300_000),
                       ("T7_RUNE", 0): view(sell=450), ("T7_SOUL", 0): view(sell=2_300), ("T7_RELIC", 0): view(sell=8_000),
                       ("T7_TESTSWORD@3", 3): view(sell=6_000_000)}
        k = self.cost("T7_TESTSWORD@3", 3)
        way = k["ways"]["instant"]
        reroll = upgrade.reroll(4096, 1, 3)["silver"]  # about 43,700: Normal plus reroll beats buying Good or Outstanding
        self.assertEqual(way["start"]["quality"], 1)
        self.assertAlmostEqual(way["start"]["reroll"], reroll)
        self.assertEqual(way["materials"], 384 * (450 + 2_300 + 8_000))
        self.assertAlmostEqual(way["total"], 200_000 + reroll + 384 * (450 + 2_300 + 8_000))
        self.assertEqual(k["direct"]["instant"]["unit"], 6_000_000)

    def test_buy_orders_go_one_over_the_bid_with_the_setup_fee(self):
        self.prices = {("T7_TESTSWORD", 1): view(buy=100_000), ("T7_RUNE", 0): view(buy=400)}
        k = self.cost("T7_TESTSWORD@1", 1)
        way = k["ways"]["order"]
        self.assertAlmostEqual(way["start"]["base"], 100_001 * 1.025)
        self.assertAlmostEqual(way["materials"], 384 * 401 * 1.025)
        self.assertIsNone(k["ways"]["instant"]["total"])  # nothing listed to buy at once

    def test_a_material_without_any_price_leaves_the_total_open(self):
        self.prices = {("T7_TESTSWORD", 1): view(sell=200_000), ("T7_RUNE", 0): view(sell=450)}
        way = self.cost("T7_TESTSWORD@2", 1)["ways"]["instant"]
        self.assertEqual(way["missing"], ["T7_SOUL"])
        self.assertIsNone(way["total"])

    def test_a_day_old_cheap_price_does_not_win_over_a_current_one(self):
        old = view(sell=1_000, city="2004", seen=NOW - timedelta(days=2))
        old["cities"] += view(sell=5_000, city="1002")["cities"]
        self.prices = {("T7_TESTSWORD", 1): view(sell=200_000), ("T7_RUNE", 0): old}
        rune = self.cost("T7_TESTSWORD@1", 1)["materials"][0]["instant"]
        self.assertEqual((rune["city"], rune["unit"]), ("1002", 5_000))

    def test_a_fresh_start_wins_over_a_cheaper_day_old_one(self):
        self.prices = {("T7_TESTSWORD", 1): view(sell=100_000, seen=NOW - timedelta(days=3)), ("T7_TESTSWORD", 2): view(sell=300_000),
                       ("T7_RUNE", 0): view(sell=450)}
        start = self.cost("T7_TESTSWORD@1", 2)["ways"]["instant"]["start"]
        self.assertEqual((start["quality"], start["base"]), (2, 300_000))

    def test_any_quality_makes_a_normal_one_and_compares_with_a_normal_finished_item(self):
        self.prices = {("T7_TESTSWORD", 1): view(sell=200_000), ("T7_RUNE", 0): view(sell=450),
                       ("T7_TESTSWORD@1", 1): view(sell=500_000, buy=400_000), ("T7_TESTSWORD@1", 0): view(sell=450_000, buy=900_000)}
        k = self.cost("T7_TESTSWORD@1", 0)
        self.assertEqual(k["target"], 1)
        self.assertEqual(k["direct"]["instant"]["unit"], 500_000)
        self.assertAlmostEqual(k["direct"]["order"]["unit"], 400_001 * 1.025)

    def test_plain_items_need_nothing_and_unknown_ones_are_not_supported(self):
        self.assertFalse(self.cost("T7_TESTSWORD", 1)["needed"])
        self.assertFalse(self.cost("T5_UNKNOWN@1", 1)["supported"])
        self.assertFalse(self.cost("T7_RUNE@1", 1)["supported"])  # resources have no enchant path here
        with self.assertRaises(ValueError):
            self.cost("bad id", 1)

    def test_settings_check_the_reroll_scale(self):
        for bad in (0, 11, float("nan")):
            with self.assertRaises(ValueError):
                self.store.save_config({"reroll_cost_scale": bad})
        self.store.save_config({"reroll_cost_scale": 0.9})
        self.assertEqual(self.store.cfg["reroll_cost_scale"], 0.9)


if __name__ == "__main__":
    unittest.main()
