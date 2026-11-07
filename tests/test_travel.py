"""Teleport cost: route distances and the per-unit cost the trading math adds. Synthetic data only."""
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

import gamedata
import lookup
import travel
from market import CARRIED, Store

ROOT = Path(__file__).resolve().parents[1]
LYMHURST, BRIDGEWATCH, MARTLOCK, FORT_STERLING, THETFORD, BRECILIEN, CAERLEON = "1002", "2004", "3008", "4002", "7", "5003", "3005"
ITEMS = {"T8_TESTSOUL": {"w": 1.0, "f": 1.0}, "T6_TESTORE": {"w": 1.14, "f": 8.0},
         "T6_TESTSWORD": {"w": 15.2, "f": 1.0, "v": [3, 6], "up": [[["T6_RUNE", 384]]]}}


class DistanceTests(unittest.TestCase):
    def test_ring_neighbours_are_one_and_the_rest_two(self):
        self.assertEqual(travel.distance(THETFORD, FORT_STERLING), 1)
        self.assertEqual(travel.distance(MARTLOCK, THETFORD), 1)  # the ring closes
        self.assertEqual(travel.distance(LYMHURST, BRIDGEWATCH), 1)
        self.assertEqual(travel.distance(THETFORD, LYMHURST), 2)
        self.assertEqual(travel.distance(FORT_STERLING, BRIDGEWATCH), 2)
        self.assertEqual(travel.distance(LYMHURST, LYMHURST), 0)

    def test_brecilien_is_two_and_caerleon_has_no_route(self):
        for city in travel.RING:
            self.assertEqual(travel.distance(BRECILIEN, city), 2)
            self.assertEqual(travel.distance(city, BRECILIEN), 2)
            self.assertIsNone(travel.distance(CAERLEON, city))
        self.assertIsNone(travel.distance(BRECILIEN, CAERLEON))


class UnitCostTests(unittest.TestCase):
    def setUp(self):
        self.items = gamedata.Catalog()
        self.items.items = ITEMS

    def test_unit_cost_is_150_per_kg_times_factor_multiplier_and_distance(self):
        self.assertEqual(travel.unit_cost("T8_TESTSOUL", BRECILIEN, LYMHURST, 1.0, self.items), 300)
        self.assertAlmostEqual(travel.unit_cost("T6_TESTORE", THETFORD, FORT_STERLING, 1.2, self.items), 150 * 1.14 * 8 * 1.2)
        self.assertAlmostEqual(travel.unit_cost("T6_TESTSWORD@2", THETFORD, FORT_STERLING, 1.0, self.items), 150 * 15.2)
        self.assertIsNone(travel.unit_cost("T8_TESTSOUL", CAERLEON, LYMHURST, 1.0, self.items))
        self.assertIsNone(travel.unit_cost("T5_UNKNOWN", THETFORD, LYMHURST, 1.0, self.items))


class StoreCarryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        shutil.copy(ROOT / "config.example.json", self.root / "config.json")
        self.store = Store(self.root)
        self.store.cfg.update(transport_per_unit=0, teleport_multiplier=1.0)
        self.saved = gamedata.catalog.items
        gamedata.catalog.items = ITEMS

    def tearDown(self):
        gamedata.catalog.items = self.saved
        self.store.db.close()
        self.tmp.cleanup()

    def test_carry_adds_teleport_cost_and_the_extra_setting(self):
        self.assertEqual(self.store.carry("T8_TESTSOUL", BRECILIEN, LYMHURST), 300)
        self.store.cfg.update(transport_per_unit=20, teleport_multiplier=1.156)
        self.assertAlmostEqual(self.store.carry("T8_TESTSOUL", BRECILIEN, LYMHURST), 300 * 1.156 + 20)

    def test_same_city_and_carried_units_cost_nothing(self):
        self.store.cfg.update(transport_per_unit=20)
        self.assertEqual(self.store.carry("T8_TESTSOUL", LYMHURST, LYMHURST), 0)
        self.assertEqual(self.store.carry("T8_TESTSOUL", CARRIED, LYMHURST), 0)

    def test_unknown_item_or_route_falls_back_to_the_extra_setting(self):
        self.store.cfg.update(transport_per_unit=20)
        self.assertEqual(self.store.carry("T5_UNKNOWN", THETFORD, LYMHURST), 20)
        self.assertEqual(self.store.carry("T8_TESTSOUL", CAERLEON, LYMHURST), 20)

    def test_worst_carry_takes_the_dearest_trading_city(self):
        self.store.cfg.update(excluded_cities=[CAERLEON])
        # Into Thetford: Fort Sterling and Martlock are 1 step, the rest 2.
        self.assertEqual(self.store.worst_carry("T8_TESTSOUL", THETFORD), 300)

    def test_settings_check_the_multiplier_and_both_confirmations(self):
        for bad in (0, -1, 11, float("nan")):
            with self.assertRaises(ValueError):
                self.store.save_config({"teleport_multiplier": bad})
        with self.assertRaises(ValueError):
            self.store.save_config({"teleport_confirmed": "yes"})
        self.store.save_config({"teleport_multiplier": 1.2, "transport_confirmed": True, "teleport_confirmed": False})
        self.assertEqual(self.store.cfg["teleport_multiplier"], 1.2)
        self.assertFalse(self.store.transport_confirmed())
        self.store.save_config({"teleport_confirmed": True})
        self.assertTrue(self.store.transport_confirmed())
        # Without the item weights the teleport part is missing, so the cost is not confirmed either.
        gamedata.catalog.items = None
        self.assertFalse(self.store.transport_confirmed())

    def test_lookup_answer_carries_the_city_to_city_cost(self):
        self.store.cfg.update(excluded_cities=[CAERLEON])
        names = [{"id": "T8_TESTSOUL", "ko": "영혼", "en": "Soul"}]
        with patch("lookup.public.get", return_value=(None, False)), patch.object(lookup.catalog, "items", names):
            view = self.store.lookup_item("T8_TESTSOUL", 0)
        self.assertEqual(view["carry"][BRECILIEN][LYMHURST], 300)
        self.assertEqual(view["carry"][THETFORD][FORT_STERLING], 150)
        self.assertNotIn(LYMHURST, view["carry"][LYMHURST])
        self.assertEqual(view["travel"]["weight"], 1.0)
        with patch("lookup.public.get", return_value=(None, False)), patch.object(lookup.catalog, "items", names):
            self.assertIsNone(self.store.lookup_item("T5_UNKNOWN", 0)["carry"])


if __name__ == "__main__":
    unittest.main()
