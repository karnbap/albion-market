"""Synthetic fixtures only: the any-item lookup never reaches the network in tests."""
import json
import shutil
import tempfile
import time
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

import lookup
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
