"""Synthetic fixtures only: the any-item lookup never reaches the network in tests."""
import json
import shutil
import tempfile
import time
import unittest
from unittest.mock import patch
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

import lookup
import market
from market import Store, location

ROOT = Path(__file__).resolve().parents[1]
UTC = timezone.utc


class LookupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        shutil.copy(ROOT / "config.json", self.root / "config.json")
        cfg = json.loads((self.root / "config.json").read_text(encoding="utf-8"))
        cfg["excluded_cities"] = ["3005"]
        (self.root / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
        self.now = datetime(2026, 11, 1, 12, tzinfo=UTC)
        self.store = Store(self.root, clock=lambda: self.now)
        lookup.catalog.items = [{"id": "T6_2H_CLAYMORE", "ko": "마스터의 클레이모어", "en": "Master's Claymore"},
                                {"id": "T6_2H_CLAYMORE@1", "ko": "마스터의 클레이모어", "en": "Master's Claymore"},
                                {"id": "T4_2H_CLAYMORE", "ko": "숙련자의 클레이모어", "en": "Adept's Claymore"},
                                {"id": "T3_BAG", "ko": "장인의 가방", "en": "Journeyman's Bag"},
                                {"id": "T4_BAG", "ko": "숙련자의 가방", "en": "Adept's Bag"},
                                {"id": "UNIQUE_HIDEOUT", "ko": "은신처 건설 키트", "en": "Hideout Construction Kit"}]

    def tearDown(self):
        self.store.db.close()
        lookup.public.cache.clear()
        self.tmp.cleanup()

    def order(self, oid, price, amount, loc="1002", side="offer", item="T6_2H_CLAYMORE@1", quality=1):
        return {"Id": oid, "ItemTypeId": item, "LocationId": loc, "QualityLevel": quality, "EnchantmentLevel": 1,
                "UnitPriceSilver": price * 10000, "Amount": amount, "AuctionType": side,
                "Expires": (self.now + timedelta(days=20)).replace(tzinfo=None).isoformat()}

    def db(self):
        return closing(lookup.connect(self.root))

    def test_book_merges_a_city_zone_and_keeps_cheapest_sell_and_dearest_buy(self):
        seen = self.now.isoformat()
        with self.db() as db:
            lookup.add_orders(db, [self.order(1, 200_000, 2), self.order(2, 190_000, 1, loc="1000"), self.order(3, 150_000, 4, side="request"),
                                   self.order(4, 160_000, 1, side="request"), self.order(5, 180_000, 9, loc="3005"),
                                   self.order(6, 100, 1, quality=2), {"Id": "bad"}], "stream", seen)
            lookup.add_orders(db, [self.order(1, 195_000, 1)], "me", seen)
            book = lookup.markets(db, "T6_2H_CLAYMORE@1", 1, lambda loc: location(loc) if location(loc) != "3005" else None, self.now)
        self.assertEqual(set(book), {"1002"})
        sell, buy = book["1002"]["sell"], book["1002"]["buy"]
        self.assertEqual((sell["price"], sell["amount"], sell["units"], sell["mine"]), (190_000, 1, 2, True))
        self.assertEqual((buy["price"], buy["amount"], buy["units"]), (160_000, 1, 5))
        # Any quality: the quality-2 order at 100 is now the cheapest and says which quality it is.
        with self.db() as db:
            anyq = lookup.markets(db, "T6_2H_CLAYMORE@1", lookup.ANY_QUALITY, lambda loc: location(loc) if location(loc) != "3005" else None, self.now)
        self.assertEqual((anyq["1002"]["sell"]["price"], anyq["1002"]["sell"]["quality"], anyq["1002"]["sell"]["units"]), (100, 2, 3))

    def test_old_and_expired_orders_are_purged(self):
        with self.db() as db:
            lookup.add_orders(db, [self.order(1, 10, 1)], "stream", (self.now - timedelta(hours=25)).isoformat())
            gone = self.order(2, 10, 1)
            gone["Expires"] = (self.now - timedelta(hours=1)).replace(tzinfo=None).isoformat()
            lookup.add_orders(db, [gone, self.order(3, 10, 1)], "stream", self.now.isoformat())
            lookup.purge(db, self.now)
            self.assertEqual([r[0] for r in db.execute("SELECT id FROM orders")], [3])
            # Past the row cap the oldest sightings go first.
            lookup.add_orders(db, [self.order(4, 10, 1)], "stream", (self.now + timedelta(minutes=1)).isoformat())
            cap, lookup.MAX_ROWS = lookup.MAX_ROWS, 1
            try:
                lookup.purge(db, self.now)
            finally:
                lookup.MAX_ROWS = cap
            self.assertEqual([r[0] for r in db.execute("SELECT id FROM orders")], [4])

    def test_markets_i_open_feed_the_lookup_but_my_order_lists_do_not(self):
        self.store.ingest("marketorders.ingest", {"Orders": [self.order(1, 200_000, 1)]})
        self.store.claim_my_order_list = lambda: True
        self.store.ingest("marketorders.ingest", {"Orders": [self.order(2, 1_000, 1)]})
        with self.db() as db:
            self.assertEqual(db.execute("SELECT id, source FROM orders").fetchall(), [(1, "me")])

    def test_item_view_joins_local_orders_with_the_cached_public_answer(self):
        with self.db() as db:
            lookup.add_orders(db, [self.order(1, 200_000, 2)], "stream", (self.now - timedelta(minutes=5)).isoformat())
        day = (self.now - timedelta(days=1)).replace(tzinfo=None, hour=0).isoformat()
        lookup.public.cache["T6_2H_CLAYMORE@1"] = {"fetched": time.time(), "error": "", "prices": [
            {"city": "Lymhurst", "quality": 1, "sell_price_min": 210_000, "sell_price_min_date": "2026-11-01T06:00:00", "buy_price_max": 0, "buy_price_max_date": "0001-01-01T00:00:00"},
            {"city": "Martlock", "quality": 1, "sell_price_min": 205_000, "sell_price_min_date": "2026-11-01T11:00:00", "buy_price_max": 150_000, "buy_price_max_date": "2026-11-01T11:30:00"},
            {"city": "Martlock", "quality": 3, "sell_price_min": 199_000, "sell_price_min_date": "2026-11-01T10:00:00", "buy_price_max": 0, "buy_price_max_date": "0001-01-01T00:00:00"},
            {"city": "Thetford", "quality": 3, "sell_price_min": 230_000, "sell_price_min_date": "2026-11-01T09:00:00", "buy_price_max": 0, "buy_price_max_date": "0001-01-01T00:00:00"},
            {"city": "Caerleon", "quality": 1, "sell_price_min": 1, "sell_price_min_date": "2026-11-01T11:00:00"}],
            "history": [{"location": "Martlock", "quality": 3, "data": [{"item_count": 6, "avg_price": 200_000, "timestamp": day},
                                                          {"item_count": 1, "avg_price": 100, "timestamp": "2026-10-01T00:00:00"}]}]}
        view = self.store.lookup_item("T6_2H_CLAYMORE@1", 1)
        rows = {r["city"]: r for r in view["cities"]}
        self.assertNotIn("3005", rows)
        self.assertEqual(view["name"], "마스터의 클레이모어")
        self.assertFalse(view["public_loading"])
        self.assertEqual(rows["1002"]["local"]["sell"]["price"], 200_000)
        self.assertEqual(rows["1002"]["public"]["sell_seen"], "2026-11-01T06:00:00+00:00")
        self.assertIsNone(rows["1002"]["public"]["buy_seen"])
        self.assertEqual(rows["3008"]["public"]["buy"], 150_000)
        # No quality-1 history in Martlock, so every quality stands in and says so.
        self.assertEqual((rows["3008"]["history"]["average"], rows["3008"]["history"]["last_count"], rows["3008"]["history"]["all_qualities"]), (200_000, 6, True))
        # Quality 1 alone: Thetford only has quality 3, so nothing there; Martlock keeps its quality-1 price.
        self.assertIsNone(rows["7"]["public"])
        self.assertEqual(rows["3008"]["public"]["sell"], 205_000)
        # Any quality: the cheapest of every quality, with the quality named; the history is not flagged.
        rows = {r["city"]: r for r in self.store.lookup_item("T6_2H_CLAYMORE@1", lookup.ANY_QUALITY)["cities"]}
        self.assertEqual((rows["3008"]["public"]["sell"], rows["3008"]["public"]["sell_quality"]), (199_000, 3))
        self.assertEqual((rows["3008"]["public"]["buy"], rows["3008"]["public"]["buy_quality"]), (150_000, 1))
        self.assertEqual(rows["7"]["public"]["sell"], 230_000)
        self.assertFalse(rows["3008"]["history"]["all_qualities"])

    def test_family_grid_shows_where_each_tier_and_enchant_has_a_sell_order(self):
        with self.db() as db:
            lookup.add_orders(db, [self.order(1, 210_000, 1)], "stream", (self.now - timedelta(minutes=5)).isoformat())
        lookup.public.cache["family:2H_CLAYMORE"] = {"fetched": time.time(), "error": "", "prices": [
            {"item_id": "T4_2H_CLAYMORE", "city": "Martlock", "quality": 2, "sell_price_min": 9_000, "sell_price_min_date": "2026-10-31T10:00:00"},
            {"item_id": "T4_2H_CLAYMORE", "city": "Thetford", "quality": 1, "sell_price_min": 9_500, "sell_price_min_date": "2026-11-01T10:00:00"},
            {"item_id": "T4_2H_CLAYMORE", "city": "Lymhurst", "quality": 1, "sell_price_min": 1_000, "sell_price_min_date": "2026-10-01T10:00:00"},
            {"item_id": "T6_2H_CLAYMORE@1", "city": "Lymhurst", "quality": 1, "sell_price_min": 205_000, "sell_price_min_date": "2026-11-01T09:00:00"},
            {"item_id": "T6_2H_CLAYMORE", "city": "Caerleon", "quality": 1, "sell_price_min": 5, "sell_price_min_date": "2026-11-01T09:00:00"}]}
        view = self.store.lookup_family("2H_CLAYMORE")
        combos = {(c["tier"], c["enchant"]): c for c in view["combos"]}
        self.assertEqual(set(combos), {(6, 0), (6, 1), (4, 0)})
        # A month-old Lymhurst price drops out; the cheapest of the rest, across qualities, wins.
        self.assertEqual((combos[(4, 0)]["cheapest"], combos[(4, 0)]["city"], combos[(4, 0)]["cities"]), (9_000, "3008", 2))
        # Lymhurst T6.1: the stream heard it more recently than the public server, so its price stands.
        self.assertEqual((combos[(6, 1)]["cheapest"], combos[(6, 1)]["cities"]), (210_000, 1))
        # Caerleon is excluded, so T6.0 has no city.
        self.assertEqual((combos[(6, 0)]["cities"], combos[(6, 0)]["cheapest"]), (0, None))
        with self.assertRaises(ValueError):
            self.store.lookup_family("../x")

    def test_item_view_rejects_ids_outside_the_item_id_shape(self):
        for bad in ("", "T6_2H_CLAYMORE@9", "../x", "t4_bag"):
            with self.assertRaises(ValueError):
                self.store.lookup_item(bad, 1)
        with self.assertRaises(ValueError):
            self.store.lookup_item("T4_BAG", 7)

    def test_search_returns_item_families_with_tiers_as_a_filter(self):
        hits = self.store.lookup_search("클레이모어")["items"]
        self.assertEqual([(h["family"], h["ko"], h["tiers"], h["tier"]) for h in hits],
                         [("2H_CLAYMORE", "클레이모어", [{"tier": 4, "enchants": [0]}, {"tier": 6, "enchants": [0, 1]}], None)])
        # A tier word in the query opens the family on that tier.
        self.assertEqual(self.store.lookup_search("마스터의 클레이모어")["items"][0]["tier"], 6)
        bag = self.store.lookup_search("adept bag")["items"][0]
        self.assertEqual((bag["family"], bag["en"], bag["tier"]), ("BAG", "Bag", 4))
        kit = self.store.lookup_search("은신처")["items"][0]
        self.assertEqual((kit["family"], kit["tiers"]), ("UNIQUE_HIDEOUT", [{"tier": None, "enchants": [0]}]))

    def test_stream_parser_answers_ping_and_keeps_partial_messages(self):
        sent = []
        sock = type("Sock", (), {"sendall": lambda self, b: sent.append(b)})()
        body = json.dumps({"Orders": [self.order(1, 5, 1)]}).encode()
        frame = b"PING\r\nMSG marketorders.ingest 1 " + str(len(body)).encode() + b"\r\n" + body + b"\r\n"
        batch = []
        rest = lookup.Stream.parse(frame[:-10], batch, sock)
        self.assertEqual((batch, sent), ([], [b"PONG\r\n"]))
        rest = lookup.Stream.parse(rest + frame[-10:], batch, sock)
        self.assertEqual((rest, batch[0]["Id"]), (b"", 1))


if __name__ == "__main__":
    unittest.main()


class LookupRobustnessTests(LookupTests):
    # Runs only its own tests; the inherited fixture is all it needs.
    test_book_merges_a_city_zone_and_keeps_cheapest_sell_and_dearest_buy = test_old_and_expired_orders_are_purged = None
    test_markets_i_open_feed_the_lookup_but_my_order_lists_do_not = test_item_view_joins_local_orders_with_the_cached_public_answer = None
    test_item_view_rejects_ids_outside_the_item_id_shape = test_search_returns_item_families_with_tiers_as_a_filter = None
    test_family_grid_shows_where_each_tier_and_enchant_has_a_sell_order = None
    test_stream_parser_answers_ping_and_keeps_partial_messages = None

    def test_a_failing_lookup_write_does_not_lose_the_tracked_market(self):
        original = lookup.add_orders

        def broken(*args, **kwargs):
            raise lookup.sqlite3.OperationalError("database is locked")
        lookup.add_orders = broken
        try:
            with self.assertLogs(level="ERROR"):
                relic = {**self.order(1, 9000, 3, item="T7_RELIC"), "EnchantmentLevel": 0}
                self.store.ingest("marketorders.ingest", {"Orders": [relic]})
        finally:
            lookup.add_orders = original
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM orders").fetchone()[0], 1)

    def test_stream_retry_wait_resets_after_a_long_healthy_connection(self):
        stream = lookup.Stream(self.root)
        clock, waits = iter([0, 1, 2, 3, 100, 400]), []

        def listen(db):
            raise ConnectionError("dropped")
        stream.listen = listen

        def wait(seconds):
            waits.append(seconds)
            if len(waits) == 3:
                stream.stopped.set()
            return stream.stopped.is_set()
        stream.stopped.wait = wait
        real = lookup.time.monotonic
        lookup.time.monotonic = lambda: next(clock)
        try:
            with self.assertLogs(level="WARNING"):
                stream.run()
        finally:
            lookup.time.monotonic = real
        # Quick failure: 5; next quick failure: 10; then a 300-second healthy run resets to 5.
        self.assertEqual(waits, [5, 10, 5])


class WatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        shutil.copy(ROOT / "config.json", self.root / "config.json")
        cfg = json.loads((self.root / "config.json").read_text(encoding="utf-8"))
        cfg["excluded_cities"] = ["3005"]
        (self.root / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
        self.now = datetime(2026, 11, 2, 12, tzinfo=UTC)
        self.store = Store(self.root, clock=lambda: self.now)
        lookup.catalog.items = [{"id": "T7_RELIC", "ko": "그랜드 마스터의 유물", "en": "Grandmaster's Relic"}]

    def tearDown(self):
        self.store.db.close()
        lookup.public.cache.clear()
        self.tmp.cleanup()

    def order(self, oid, price, loc, side="offer", minutes=5):
        return {"Id": oid, "ItemTypeId": "T7_RELIC", "LocationId": loc, "QualityLevel": 1, "EnchantmentLevel": 0,
                "UnitPriceSilver": price * 10000, "Amount": 3, "AuctionType": side,
                "Expires": (self.now + timedelta(days=20)).replace(tzinfo=None).isoformat()}

    def forecast_case(self, per_day=100):
        """Two asks then a price with no profit left; the list city sold per_day a day over the last week."""
        with closing(lookup.connect(self.root)) as db:
            seen = (self.now - timedelta(minutes=5)).isoformat()
            lookup.add_orders(db, [self.order(1, 7_000, "3008"), self.order(2, 8_000, "3008"), self.order(3, 9_900, "3008"), self.order(4, 10_000, "1002")], "stream", seen)
        days = [{"item_count": per_day, "avg_price": 9_000, "timestamp": (self.now - timedelta(days=d)).replace(tzinfo=None).isoformat()} for d in range(1, 8)]
        lookup.public.cache["T7_RELIC"] = {"fetched": time.time(), "error": "", "prices": [], "history": [{"location": "Lymhurst", "quality": 1, "data": days}]}
        self.store.cfg.update(sales_tax=0.08, setup_fee=0.025, transport_per_unit=0, cycle_share=0.2)
        self.store.set_watch({"action": "add", "item": "T7_RELIC", "quality": 0})
        return self.store.watches()["watches"][0]["forecast"]

    def test_watch_shows_the_average_of_buying_a_sum_and_where(self):
        self.forecast_case()
        with closing(lookup.connect(self.root)) as db:
            lookup.add_orders(db, [self.order(9, 100_000_000, "4002")], "stream", self.now.isoformat())
        with patch.object(market, "WATCH_DEPTHS", (15_000, 10_000_000)):
            depth = self.store.watches()["watches"][0]["depth"]
        self.assertEqual((depth[0]["units"], depth[0]["average"], depth[0]["cities"], depth[0]["short"]), (2, 7_000, {"3008": 2}, False))
        # Only 12 units were heard, and the absurd order is not one of them.
        self.assertEqual((depth[1]["units"], depth[1]["cities"], depth[1]["short"]), (12, {"3008": 9, "1002": 3}, True))
        self.assertAlmostEqual(depth[1]["average"], 104_700 / 12)

    def test_forecast_buys_only_what_still_leaves_a_profit(self):
        f = self.forecast_case()
        self.assertEqual(f["cities"], {"3008": 6})
        # Lists at 10,000 and keeps 8,950 after fees: the 9,900 ask and dearer are not bought.
        self.assertEqual((round(f["net_price"]), f["units"], f["invested"]), (8_950, 6, 45_000))
        self.assertAlmostEqual(f["profit"], 8_700)
        self.assertAlmostEqual(f["roi"], 8_700 / 45_000)
        self.assertTrue(f["depth_known"])
        self.assertAlmostEqual(f["my_per_day"], 100 * market.PLAN_INSTANT_SHARE * 0.2)
        self.assertAlmostEqual(f["days"], 6 / f["my_per_day"])
        self.assertIsNone(f["fit"])
        # The 7-day average sells below the listing, so the lower return is given too.
        self.assertAlmostEqual(f["average_roi"], (9_000 * 0.895 * 6 - 45_000) / 45_000)

    def test_forecast_says_when_the_money_will_not_sell_within_a_week(self):
        f = self.forecast_case(per_day=2)
        self.assertGreater(f["days"], market.WATCH_SELL_DAYS)
        self.assertEqual(f["fit"]["units"], int(f["my_per_day"] * market.WATCH_SELL_DAYS))
        self.assertLess(f["fit"]["invested"], f["invested"])

    def test_forecast_without_heard_orders_or_profit(self):
        self.store.cfg.update(sales_tax=0.08, setup_fee=0.025, transport_per_unit=0)
        lookup.public.cache["T7_RELIC"] = {"fetched": time.time(), "error": "", "history": [], "prices": [
            {"city": "Thetford", "quality": 1, "sell_price_min": 9_000, "sell_price_min_date": "2026-11-02T11:00:00", "buy_price_max": 0, "buy_price_max_date": "0001-01-01T00:00:00"},
            {"city": "Lymhurst", "quality": 1, "sell_price_min": 12_000, "sell_price_min_date": "2026-11-02T11:00:00", "buy_price_max": 0, "buy_price_max_date": "0001-01-01T00:00:00"}]}
        self.store.set_watch({"action": "add", "item": "T7_RELIC", "quality": 0})
        f = self.store.watches()["watches"][0]["forecast"]
        self.assertFalse(f["depth_known"])
        self.assertEqual(f["units"], 10_000_000 // 9_000)
        self.assertIsNone(f["days"])
        lookup.public.cache["T7_RELIC"]["prices"][1]["sell_price_min"] = 9_500
        f = self.store.watches()["watches"][0]["forecast"]
        self.assertEqual((f["units"], f["invested"], f["roi"]), (0, 0, None))
        self.assertLess(f["best_roi"], 0)

    def scan(self, first_id, prices, minutes_ago, loc="1002", side="offer"):
        with closing(lookup.connect(self.root)) as db:
            lookup.add_orders(db, [self.order(first_id + i, p, loc, side) for i, p in enumerate(prices)], "stream", (self.now - timedelta(minutes=minutes_ago)).isoformat())

    def test_a_newer_scan_removes_orders_that_are_gone(self):
        city = lambda loc: location(loc)
        self.scan(1, [6_000, 7_000, 7_400, 9_000, 12_000], 120)
        self.scan(11, [7_300, 7_350, 7_500, 7_600, 8_000], 5)
        with closing(lookup.connect(self.root)) as db:
            prices = [r["price"] for r in lookup.sell_levels(db, "T7_RELIC", 0, city, self.now)]
            book = lookup.markets(db, "T7_RELIC", 0, city, self.now)["1002"]["sell"]
        # 6,000 and 7,000 sit under the new best price and 7,400 inside the scan, not listed again: all gone.
        # 9,000 and 12,000 are beyond where the new scan stopped, so they may still be there.
        self.assertEqual(prices, [7_300, 7_350, 7_500, 7_600, 8_000, 9_000, 12_000])
        self.assertEqual(book["price"], 7_300)
        self.assertTrue(book["scan"])
        # One lone order heard later proves nothing about the others.
        self.scan(21, [9_500], 1)
        with closing(lookup.connect(self.root)) as db:
            self.assertEqual(lookup.markets(db, "T7_RELIC", 0, city, self.now)["1002"]["sell"]["price"], 7_300)

    def test_a_newer_scan_removes_buy_orders_from_the_top(self):
        self.scan(1, [9_000, 8_500, 8_000, 7_500, 7_000], 120, side="request")
        self.scan(11, [8_200, 8_100, 7_900, 7_800, 7_700], 5, side="request")
        with closing(lookup.connect(self.root)) as db:
            book = lookup.markets(db, "T7_RELIC", 0, location, self.now)["1002"]["buy"]
        self.assertEqual(book["price"], 8_200)
        # The five of the new scan, plus 7,500 and 7,000 beyond where it stopped: 7 orders of 3.
        self.assertEqual(book["units"], 21)

    def test_watch_list_price_trusts_a_scan_newer_than_the_public_price(self):
        self.scan(1, [9_100, 9_200, 9_300, 9_400, 9_500], 5)
        lookup.public.cache["T7_RELIC"] = {"fetched": time.time(), "error": "", "history": [], "prices": [
            {"city": "Lymhurst", "quality": 1, "sell_price_min": 8_800, "sell_price_min_date": "2026-11-02T07:00:00", "buy_price_max": 0, "buy_price_max_date": "0001-01-01T00:00:00"}]}
        self.store.set_watch({"action": "add", "item": "T7_RELIC", "quality": 0})
        w = self.store.watches()["watches"][0]
        self.assertEqual(w["list"]["price"], 9_100)

    def test_forecast_drops_heard_orders_a_newer_public_price_has_passed(self):
        with closing(lookup.connect(self.root)) as db:
            lookup.add_orders(db, [self.order(1, 6_000, "3008"), self.order(2, 7_000, "3008")], "stream", (self.now - timedelta(hours=5)).isoformat())
        lookup.public.cache["T7_RELIC"] = {"fetched": time.time(), "error": "", "history": [], "prices": [
            {"city": "Martlock", "quality": 1, "sell_price_min": 6_500, "sell_price_min_date": "2026-11-02T11:00:00", "buy_price_max": 0, "buy_price_max_date": "0001-01-01T00:00:00"},
            {"city": "Lymhurst", "quality": 1, "sell_price_min": 10_000, "sell_price_min_date": "2026-11-02T11:00:00", "buy_price_max": 0, "buy_price_max_date": "0001-01-01T00:00:00"}]}
        self.store.cfg.update(sales_tax=0.08, setup_fee=0.025, transport_per_unit=0)
        self.store.set_watch({"action": "add", "item": "T7_RELIC", "quality": 0})
        f = self.store.watches()["watches"][0]["forecast"]
        self.assertEqual((f["units"], f["invested"]), (3, 21_000))

    def test_forecast_reads_this_pc_history_when_newer(self):
        self.forecast_case()
        for hours in range(24, 0, -1):
            self.store.db.execute("INSERT INTO history VALUES('T7_RELIC','1002',1,?,10,95000,?)", ((self.now - timedelta(hours=hours)).isoformat(), self.now.isoformat()))
        f = self.store.watches()["watches"][0]["forecast"]
        self.assertEqual((f["sales_source"], round(f["per_day"]), round(f["average"])), ("me", 240, 9_500))

    def test_public_answers_survive_a_restart(self):
        answer = [{"city": "Lymhurst", "quality": 1, "sell_price_min": 9_000}]
        lookup.public.directory = self.root
        with patch.object(lookup, "fetch_json", return_value=answer):
            lookup.public.fetch("T7_RELIC", ["Lymhurst"])
        lookup.public.cache.clear()
        with patch.object(lookup.public, "fetch"):
            hit, _ = lookup.public.get("T7_RELIC", ["Lymhurst"], self.root)
        self.assertEqual(hit["prices"], answer)
        lookup.public.running.clear()
        # A refused request is not saved over the good answer.
        with patch.object(lookup, "fetch_json", side_effect=OSError("429")):
            lookup.public.fetch("T7_RELIC", ["Lymhurst"])
        lookup.public.cache.clear()
        with patch.object(lookup.public, "fetch"):
            hit, _ = lookup.public.get("T7_RELIC", ["Lymhurst"], self.root)
        self.assertEqual(hit["prices"], answer)
        lookup.public.running.clear()
        lookup.public.directory = None

    def test_family_grid_gives_each_combination_its_average_price(self):
        lookup.catalog.items = [{"id": "T4_MAIN_TEST", "ko": "숙련자의 시험", "en": "Adept's Test"}, {"id": "T4_MAIN_TEST@1", "ko": "숙련자의 시험", "en": "Adept's Test"}]
        day = (self.now - timedelta(days=1)).replace(tzinfo=None).isoformat()
        old = (self.now - timedelta(days=20)).replace(tzinfo=None).isoformat()
        family = lookup.catalog.family_of("T4_MAIN_TEST")[0]
        lookup.public.cache["family:" + family] = {"fetched": time.time(), "error": "", "prices": [], "history": [
            {"item_id": "T4_MAIN_TEST", "location": "Lymhurst", "quality": 1, "data": [{"item_count": 1, "avg_price": 100, "timestamp": day}, {"item_count": 50, "avg_price": 999, "timestamp": old}]},
            {"item_id": "T4_MAIN_TEST", "location": "Martlock", "quality": 2, "data": [{"item_count": 3, "avg_price": 200, "timestamp": day}]},
            {"item_id": "T4_MAIN_TEST", "location": "Caerleon", "quality": 1, "data": [{"item_count": 9, "avg_price": 9_000, "timestamp": day}]}]}
        combos = {c["item"]: c for c in self.store.lookup_family(family)["combos"]}
        self.assertEqual(combos["T4_MAIN_TEST"]["average"], 175)
        self.assertEqual(combos["T4_MAIN_TEST"]["average_days"], 7)
        self.assertIsNone(combos["T4_MAIN_TEST@1"]["average"])
        # With no sale in the last week, the older days of the history stand in and say so.
        lookup.public.cache["family:" + family]["history"][0]["data"].pop(0)
        lookup.public.cache["family:" + family]["history"].pop(1)
        combo = next(c for c in self.store.lookup_family(family)["combos"] if c["item"] == "T4_MAIN_TEST")
        self.assertEqual((combo["average"], combo["average_days"]), (999, 30))

    def test_failed_public_fetch_keeps_what_we_knew(self):
        lookup.public.cache["T7_RELIC"] = {"fetched": 0, "error": "", "prices": [{"city": "Lymhurst"}], "history": [{"location": "Lymhurst"}]}
        with patch.object(lookup, "fetch_json", side_effect=OSError("429")):
            lookup.public.fetch("T7_RELIC", ["Lymhurst"])
        hit = lookup.public.cache["T7_RELIC"]
        self.assertEqual((hit["prices"], hit["history"]), ([{"city": "Lymhurst"}], [{"location": "Lymhurst"}]))
        self.assertIn("429", hit["error"])

    def test_watch_finds_best_prices_and_raises_alerts_only_on_fresh_values(self):
        with closing(lookup.connect(self.root)) as db:
            lookup.add_orders(db, [self.order(1, 9_500, "1002"), self.order(2, 9_200, "3008")], "stream", (self.now - timedelta(minutes=5)).isoformat())
            lookup.add_orders(db, [self.order(3, 8_800, "4002", side="request")], "stream", (self.now - timedelta(hours=3)).isoformat())
        lookup.public.cache["T7_RELIC"] = {"fetched": time.time(), "error": "", "history": [], "prices": [
            {"city": "Thetford", "quality": 1, "sell_price_min": 9_000, "sell_price_min_date": "2026-11-01T06:00:00", "buy_price_max": 0, "buy_price_max_date": "0001-01-01T00:00:00"}]}
        self.store.set_watch({"action": "add", "item": "T7_RELIC", "quality": 0})
        self.store.set_watch({"action": "add", "item": "T7_RELIC", "quality": 0})
        w = self.store.watches()["watches"]
        self.assertEqual(len(w), 1)
        wid = w[0]["id"]
        self.store.set_watch({"action": "update", "id": wid, "buy_below": 9_300, "sell_above": 9_500})
        w = self.store.watches()["watches"][0]
        # Thetford's public price is 30 hours old, so Martlock's live 9,200 is the cheapest.
        self.assertEqual((w["sell"]["price"], w["sell"]["city"], w["sell"]["source"]), (9_200, "3008", "stream"))
        self.assertTrue(w["buy_hit"] and w["buy_live"])
        # The sell alert reads the city with the highest cheapest sell (Lymhurst), not the 8,800 buy order.
        self.assertEqual((w["list"]["price"], w["list"]["city"], w["buy"]["price"]), (9_500, "1002", 8_800))
        self.assertTrue(w["sell_hit"] and w["sell_live"])
        self.store.set_watch({"action": "update", "id": wid, "buy_below": 9_300, "sell_above": 9_501})
        self.assertFalse(self.store.watches()["watches"][0]["sell_hit"])
        # A listing heard 3 hours ago still reaches the price, but is too old to alert on.
        with closing(lookup.connect(self.root)) as db:
            lookup.add_orders(db, [self.order(4, 12_000, "7")], "stream", (self.now - timedelta(hours=3)).isoformat())
        w = self.store.watches()["watches"][0]
        self.assertEqual(w["list"]["price"], 12_000)
        self.assertTrue(w["sell_hit"])
        self.assertFalse(w["sell_live"])
        # A lone absurd listing is an outlier and never reaches the alert.
        with closing(lookup.connect(self.root)) as db:
            lookup.add_orders(db, [self.order(5, 100_000_000, "2004")], "stream", (self.now - timedelta(minutes=1)).isoformat())
        w = self.store.watches()["watches"][0]
        self.assertEqual((w["list"]["price"], w["list_skipped"]["outlier"]), (12_000, 1))
        # A city whose floor is my own open sell order says nothing about the market.
        self.store.db.execute("INSERT INTO my_orders(item,city,side,price,amount,created,remaining) VALUES('T7_RELIC','7','sell',12000,3,?,3)", (self.now.isoformat(),))
        w = self.store.watches()["watches"][0]
        self.assertEqual((w["list"]["price"], w["list_skipped"]["own"]), (9_500, 1))
        self.store.db.execute("DELETE FROM my_orders")
        # The stream heard only an expensive Lymhurst order; the cheaper public price from today wins for that city.
        lookup.public.cache["T7_RELIC"]["prices"].append(
            {"city": "Lymhurst", "quality": 1, "sell_price_min": 8_000, "sell_price_min_date": "2026-11-02T10:00:00", "buy_price_max": 0, "buy_price_max_date": "0001-01-01T00:00:00"})
        floors = self.store.watches()["watches"][0]
        self.assertEqual((floors["sell"]["city"], floors["list"]["price"], floors["list"]["city"]), ("3008", 12_000, "7"))
        with self.assertRaises(ValueError):
            self.store.set_watch({"action": "update", "id": wid, "buy_below": 0, "sell_above": None})
        with self.assertRaises(ValueError):
            self.store.set_watch({"action": "add", "item": "../x"})
        self.store.set_watch({"action": "remove", "id": wid})
        self.assertEqual(self.store.watches()["watches"], [])
