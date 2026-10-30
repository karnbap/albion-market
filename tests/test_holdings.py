"""Synthetic holdings only: no game interaction and no production DB writes."""
import math
import json
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from market import Store, handler_class

ROOT = Path(__file__).resolve().parents[1]
UTC = timezone.utc


class HoldingsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        shutil.copy(ROOT / "config.json", self.root / "config.json")
        cfg = json.loads((self.root / "config.json").read_text(encoding="utf-8-sig"))
        cfg.update(setup_fee=.025, timezone="Europe/Paris")
        (self.root / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
        self.now = datetime(2026, 10, 27, 12, tzinfo=UTC)
        self.store = Store(self.root, clock=lambda: self.now)

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def order(self, oid, price, amount, item="T7_RELIC"):
        return {"Id": oid, "ItemTypeId": item, "LocationId": "4002", "QualityLevel": 1, "EnchantmentLevel": 0,
                "UnitPriceSilver": int(price * 10000), "Amount": amount, "AuctionType": "offer",
                "Expires": (self.now + timedelta(days=20)).isoformat()}

    def holdings(self):
        return self.store.state()["holdings"]

    def test_listed_plus_bank_per_city_valued_at_cost(self):
        self.store.ingest_my_orders({"Kind": "sell", "LocationId": "4002", "Orders": [self.order(1, 150, 10), self.order(2, 160, 5)]})
        self.store.add_purchase({"item": "T7_RELIC", "city": "4002", "kind": "instant", "price": 100, "amount": 10})
        self.store.set_bank({"item": "T7_RELIC", "city": "4002", "amount": 7})
        self.store.set_bank({"item": "T8_SOUL", "city": "3008", "amount": 3})
        rows = {(r["city"], r["item"]): r for r in self.holdings()["rows"]}
        relic = rows[("4002", "T7_RELIC")]
        self.assertEqual((relic["listed"], relic["bank"], relic["total"], relic["cost"], relic["value"]), (15, 7, 22, 100, 2200))
        self.assertEqual(relic["bank_updated"], self.now.isoformat())
        soul = rows[("3008", "T8_SOUL")]
        self.assertEqual((soul["listed"], soul["bank"], soul["total"], soul["cost"], soul["value"]), (0, 3, 3, None, None))
        # Cities come in the receiver's city order: Martlock (3008) before Fort Sterling (4002).
        self.assertEqual([r["city"] for r in self.holdings()["rows"]], ["3008", "4002"])
        self.store.set_bank({"item": "T8_SOUL", "city": "3008", "amount": 0})
        self.assertNotIn(("3008", "T8_SOUL"), {(r["city"], r["item"]) for r in self.holdings()["rows"]})
        for bad in ({"item": "T4_BAG"}, {"city": "9999"}, {"amount": -1}, {"amount": 1.5}, {"amount": True}):
            with self.assertRaises(ValueError, msg=bad):
                self.store.set_bank({"item": "T7_RELIC", "city": "4002", "amount": 1, **bad})

    def test_value_if_sold_now_to_the_best_buy_order(self):
        self.store.ingest("marketorders.ingest", {"Orders": [dict(self.order(11, 120, 4), AuctionType="request"),
                                                             dict(self.order(12, 110, 9), AuctionType="request")]})
        self.store.add_purchase({"item": "T7_RELIC", "city": "4002", "kind": "instant", "price": 100, "amount": 10})
        self.store.set_bank({"item": "T7_RELIC", "city": "4002", "amount": 10})
        self.store.set_bank({"item": "T8_SOUL", "city": "4002", "amount": 2})    # nobody buying souls there
        rows = {r["item"]: r for r in self.holdings()["rows"]}
        relic, net = rows["T7_RELIC"], 120 * (1 - self.store.cfg["sales_tax"])
        self.assertEqual((relic["sell_now"], relic["sell_now_amount"]), (120, 4))
        self.assertAlmostEqual(relic["net_now"], net)
        self.assertAlmostEqual(relic["value_now"], 10 * net)
        self.assertAlmostEqual(relic["profit_now"], 10 * (net - relic["cost"]))
        self.assertEqual((rows["T8_SOUL"]["sell_now"], rows["T8_SOUL"]["value_now"], rows["T8_SOUL"]["profit_now"]), (None, None, None))

    def test_priced_at_the_dearest_sell_order_i_could_place(self):
        self.store.save_config({"transport_per_unit": 5, "excluded_cities": ["5003"]})
        martlock = lambda oid, price, amount: dict(self.order(oid, price, amount), LocationId="3008")
        self.store.ingest("marketorders.ingest", {"Orders": [self.order(21, 150, 2), self.order(22, 170, 3),
                                                             martlock(23, 200, 1), martlock(24, 210, 4),
                                                             dict(self.order(25, 300, 6), LocationId="5003"), dict(self.order(26, 310, 6), LocationId="5003")]})
        self.store.ingest_my_orders({"Kind": "sell", "LocationId": "4002", "Orders": [self.order(21, 150, 2)]})
        self.store.add_purchase({"item": "T7_RELIC", "city": "4002", "kind": "instant", "price": 100, "amount": 10})
        self.store.set_bank({"item": "T7_RELIC", "city": "4002", "amount": 10})
        h = self.holdings()
        keep = 1 - self.store.cfg["sales_tax"] - self.store.cfg["setup_fee"]
        # With buying capped at 100 silver: Fort Sterling's lowest ask is mine (150), the next
        # seller asks 170, so 169 there; Martlock's lowest is someone's 200, so 199 there is the
        # dearest. Brecilien (300) is not a trading city.
        self.store.save_config({"trade_budget": 100})
        best = self.holdings()["best_listing"]["T7_RELIC"]
        self.assertEqual((best["city"], best["price"], best["own"], best["sweep"]), ("3008", 199, False, None))
        self.assertAlmostEqual(best["net"], 199 * keep)
        fs = self.store.listing_options([{"price": 150, "amount": 2}, {"price": 170, "amount": 3}], {150: 2}, 12, keep)
        self.assertEqual([(o["price"], o["own"]) for o in fs], [(150, True), (169, False)])
        self.assertEqual(fs[1]["sweep"]["units"], 0)                      # only my own listing is raised
        # With the default budget, buying Martlock's single 200 and listing everything at 209 pays more.
        self.store.save_config({"trade_budget": 10_000_000})
        h = self.holdings()
        best = h["best_listing"]["T7_RELIC"]
        self.assertEqual((best["city"], best["price"], best["held"]), ("3008", 209, 12))
        self.assertEqual({k: best["sweep"][k] for k in ("units", "cost", "levels", "plain_price")}, {"units": 1, "cost": 200, "levels": 1, "plain_price": 199})
        self.assertAlmostEqual(best["net"], (13 * 209 * keep - 200) / 12)
        row = next(r for r in h["rows"] if r["city"] == "4002" and r["item"] == "T7_RELIC")
        self.assertEqual((row["total"], row["list_city"]), (12, "3008"))
        self.assertAlmostEqual(row["list_net"], best["net"] - 5)          # carried to Martlock
        # Cheap single listings below: buying all three (140, 145, 200) and listing at 209 is best.
        self.store.ingest("marketorders.ingest", {"Orders": [martlock(27, 140, 1), martlock(28, 145, 1), martlock(23, 200, 1), martlock(24, 210, 4)]})
        best = self.holdings()["best_listing"]["T7_RELIC"]
        self.assertEqual((best["city"], best["price"], best["sweep"]["units"], best["sweep"]["cost"], best["sweep"]["plain_price"]), ("3008", 209, 3, 485, 139))

    def test_sell_target_counts_other_sellers_under_it_and_the_silver_to_clear_them(self):
        self.store.save_config({"excluded_cities": ["5003"]})
        martlock = lambda oid, price, amount: dict(self.order(oid, price, amount), LocationId="3008")
        self.store.ingest("marketorders.ingest", {"Orders": [self.order(21, 150, 2), self.order(22, 170, 3), self.order(29, 300, 5),
                                                             martlock(23, 200, 1), martlock(24, 210, 4)]})
        self.store.ingest_my_orders({"Kind": "sell", "LocationId": "4002", "Orders": [self.order(21, 150, 2)]})
        self.store.add_purchase({"item": "T7_RELIC", "city": "4002", "kind": "instant", "price": 100, "amount": 10})
        self.store.set_bank({"item": "T7_RELIC", "city": "4002", "amount": 10})
        keep = 1 - self.store.cfg["sales_tax"] - self.store.cfg["setup_fee"]
        relic = next(t for t in self.store.state()["sell_targets"] if t["item"] == "T7_RELIC")
        # No target set: average cost plus minimum margin after fees.
        self.assertEqual((relic["chosen"], relic["held"]), (None, 12))
        self.assertEqual(relic["target"], math.ceil(100 * (1 + self.store.cfg["minimum_margin"]) / keep))
        self.store.save_config({"sell_targets": {"T7_RELIC": 205}})
        relic = next(t for t in self.store.state()["sell_targets"] if t["item"] == "T7_RELIC")
        rows = {r["city"]: r for r in relic["rows"]}
        # Fort Sterling: my own 150s are not counted, someone's three at 170 are; 300 is above the target.
        self.assertEqual((rows["4002"]["units"], rows["4002"]["cost"], rows["4002"]["lowest"]), (3, 510, 150))
        self.assertAlmostEqual(rows["4002"]["value"], 15 * 204 * keep - 510)
        self.assertEqual((rows["3008"]["units"], rows["3008"]["cost"]), (1, 200))
        self.assertFalse(rows["1002"]["known"])
        with self.assertRaises(ValueError):
            self.store.save_config({"sell_targets": {"T7_RELIC": 0}})

    def test_finished_tab_counts_what_i_bought_and_have_not_collected(self):
        me, them = "00000000-0000-0000-0000-00000000000a", "00000000-0000-0000-0000-00000000000b"
        bought = {"Id": 5, "Amount": 2, "IsFinished": True, "AuctionType": "request", "HasBuyerFetched": False, "HasSellerFetched": True,
                  "BuyerCharacterId": me, "SellerCharacterId": them, "ItemTypeId": "T7_RELIC", "QualityLevel": 1, "EnchantmentLevel": 0, "LocationId": None}
        entries = [bought,
                   dict(bought, Id=6, Amount=3, AuctionType="offer"),                         # an instant buy: still mine to collect
                   dict(bought, Id=7, HasBuyerFetched=True),                                  # already collected
                   dict(bought, Id=8, BuyerCharacterId=them, SellerCharacterId=me),           # I sold it
                   dict(bought, Id=9, ItemTypeId="T4_BAG")]                                   # not tracked
        self.assertEqual(self.store.ingest_finished({"CharacterId": me, "LocationId": "1000-HellDen", "Entries": entries})["accepted"], 5)
        h = self.holdings()
        self.assertEqual([(r["city"], r["item"], r["finished"], r["total"]) for r in h["rows"]], [("1002", "T7_RELIC", 5, 5)])
        self.assertEqual([(f["location"], f["city"], f["entries"], f["tracked"], f["waiting"]) for f in h["finished"]], [("1000-HellDen", "1002", 5, 4, 5)])
        # Raw character IDs are not stored beside the list.
        self.assertNotIn(me, self.store.db.execute("SELECT identity FROM finished_lists").fetchone()[0])
        # Without the uploader's identity nothing can be told apart, so nothing counts.
        self.store.ingest_finished({"LocationId": "4002", "Entries": [bought]})
        self.assertEqual({f["location"]: (f["waiting"], f["identity_known"]) for f in self.holdings()["finished"]}["4002"], (0, False))
        # Only the latest list per market is kept; an empty list says the tab is empty.
        self.now += timedelta(minutes=5)
        self.store.ingest_finished({"CharacterId": me, "LocationId": "1000-HellDen", "Entries": []})
        self.assertEqual(self.holdings()["rows"], [])
        self.assertEqual({f["location"]: (f["entries"], f["received"]) for f in self.holdings()["finished"]}["1000-HellDen"], (0, self.now.isoformat()))
        for bad in ([], {"LocationId": "1002"}, {"LocationId": "1002", "Entries": ["x"]}, {"Entries": []}):
            with self.assertRaises(ValueError, msg=bad):
                self.store.ingest_finished(bad)

    def test_banks_and_inventory_the_fork_reads(self):
        # Item indexes as the game sends them (config maps 2031 to T7_RELIC, 2038 to T8_SOUL).
        self.store.set_bank({"item": "T7_RELIC", "city": "7", "amount": 999})      # typed in, about to be replaced
        self.store.set_bank({"item": "T8_SOUL", "city": "3008", "amount": 4})      # typed in, no bank read there
        self.store.add_purchase({"item": "T7_RELIC", "city": "7", "kind": "instant", "price": 100, "amount": 1})
        result = self.store.ingest_holdings({"LocationId": "0006", "Inventory": {"2031": 12, "2038": 10, "9999": 3},
                                             "Banks": [{"LocationId": "0006", "PreviousLocationId": "0007", "Items": {"2031": 1, "2038": 169, "77": 2}},
                                                       {"LocationId": "4001", "PreviousLocationId": "4002", "Items": {"2031": 580}},
                                                       {"LocationId": "9001", "PreviousLocationId": "", "Items": {"2031": 5}}]})
        self.assertEqual((result["accepted"], result["carried"]), (3, 22))
        h = self.holdings()
        rows = {(r["city"], r["item"]): r for r in h["rows"]}
        # A bank cluster's city: the place the player came from (0006 from Thetford 0007).
        self.assertEqual((rows[("7", "T7_RELIC")]["bank"], rows[("7", "T7_RELIC")]["bank_source"]), (1, "auto"))
        self.assertEqual(rows[("7", "T8_SOUL")]["bank"], 169)
        self.assertEqual(rows[("4002", "T7_RELIC")]["bank"], 580)
        self.assertEqual((rows[("3008", "T8_SOUL")]["bank"], rows[("3008", "T8_SOUL")]["bank_source"]), (4, "manual"))
        self.assertEqual((rows[("carried", "T7_RELIC")]["carried"], rows[("carried", "T8_SOUL")]["carried"]), (12, 10))
        self.assertEqual(rows[("carried", "T7_RELIC")]["total"], 12)
        self.assertEqual([(b["location"], b["items"]) for b in h["unmapped_banks"]], [("9001", {"T7_RELIC": 5})])
        # Carried units are part of the capital.
        capital = self.store.state()["capital"]
        self.assertEqual(capital["items"]["T7_RELIC"]["held"], 1 + 580 + 12)
        # Next visit: 0006 entered from outside still belongs to Thetford (learned).
        self.now += timedelta(minutes=10)
        self.store.ingest_holdings({"LocationId": "0006", "Inventory": {}, "Banks": [{"LocationId": "0006", "PreviousLocationId": "0000", "Items": {"2031": 3}}]})
        rows = {(r["city"], r["item"]): r for r in self.holdings()["rows"]}
        self.assertEqual((rows[("7", "T7_RELIC")]["bank"], rows[("7", "T7_RELIC")]["bank_updated"]), (3, self.now.isoformat()))
        self.assertNotIn(("7", "T8_SOUL"), rows)          # the bank no longer holds souls
        self.assertNotIn(("carried", "T7_RELIC"), rows)   # nothing carried now
        for bad in ({}, {"Inventory": {}, "Banks": [{}]}, {"Inventory": {"2031": -1}, "Banks": []}, {"Inventory": [], "Banks": []}):
            with self.assertRaises(ValueError, msg=bad):
                self.store.ingest_holdings(bad)

    def test_bank_entered_from_a_travel_hub_uses_known_or_picked_city(self):
        # Entered from 4000 (a travel hub, no city): 4001 is a known Fort Sterling bank; 9001 is not.
        self.store.ingest_holdings({"LocationId": "4001", "Inventory": {},
                                    "Banks": [{"LocationId": "4001", "PreviousLocationId": "4000", "Items": {"2031": 6}},
                                              {"LocationId": "1001", "PreviousLocationId": "1000", "Items": {"2031": 4}},
                                              {"LocationId": "9001", "PreviousLocationId": "9000", "Items": {"2038": 2}}]})
        h = self.holdings()
        self.assertEqual([(r["city"], r["item"], r["bank"]) for r in h["rows"]], [("1002", "T7_RELIC", 4), ("4002", "T7_RELIC", 6)])
        self.assertEqual([b["location"] for b in h["unmapped_banks"]], ["9001"])
        self.store.set_bank_city({"location": "9001", "city": "3008"})
        rows = {(r["city"], r["item"]): r["bank"] for r in self.holdings()["rows"]}
        self.assertEqual(rows[("3008", "T8_SOUL")], 2)
        self.assertEqual(self.holdings()["unmapped_banks"], [])
        # Remembered for the next visit, even when entered from the hub again.
        self.store.ingest_holdings({"LocationId": "9001", "Inventory": {}, "Banks": [{"LocationId": "9001", "PreviousLocationId": "9000", "Items": {"2038": 5}}]})
        self.assertEqual({(r["city"], r["item"]): r["bank"] for r in self.holdings()["rows"]}[("3008", "T8_SOUL")], 5)
        # A bank stored before its city was known is placed once the city is known.
        self.store.db.execute("UPDATE bank_auto SET city=NULL WHERE location='4001'")
        self.assertIn(("4002", "T7_RELIC"), {(r["city"], r["item"]) for r in self.holdings()["rows"]})
        for bad in ({"location": "9001", "city": "9999"}, {"location": "1234", "city": "3008"}):
            with self.assertRaises(ValueError, msg=bad):
                self.store.set_bank_city(bad)

    def test_finished_lists_only_on_the_private_path_and_bank_needs_the_token(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler_class(self.store, "test-token"))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{server.server_port}"
        body = json.dumps({"LocationId": "4002", "Entries": [{"Id": 1, "ItemTypeId": "T7_RELIC"}]}).encode()
        try:
            with urllib.request.urlopen(urllib.request.Request(url + "/private/europe/marketorders.finished", data=body)) as r:
                self.assertEqual(json.load(r)["accepted"], 1)
            with urllib.request.urlopen(urllib.request.Request(url + "/ingest/europe/marketorders.finished", data=body)) as r:
                self.assertEqual(json.load(r)["accepted"], 0)
            self.assertEqual(len(self.holdings()["finished"]), 1)
            held = json.dumps({"LocationId": "4001", "Inventory": {"2031": 2}, "Banks": []}).encode()
            with urllib.request.urlopen(urllib.request.Request(url + "/ingest/europe/holdings.containers", data=held)) as r:
                self.assertEqual(json.load(r)["accepted"], 0)
            self.assertEqual(self.holdings()["rows"], [])
            with urllib.request.urlopen(urllib.request.Request(url + "/private/europe/holdings.containers", data=held)) as r:
                self.assertEqual(json.load(r)["carried"], 2)
            bank = json.dumps({"item": "T7_RELIC", "city": "4002", "amount": 4}).encode()
            with self.assertRaises(urllib.error.HTTPError) as result:
                urllib.request.urlopen(urllib.request.Request(url + "/api/bank", data=bank))
            self.assertEqual(result.exception.code, 403)
            result.exception.close()
            with urllib.request.urlopen(urllib.request.Request(url + "/api/bank", data=bank, headers={"X-Local-Token": "test-token"})) as r:
                self.assertTrue(json.load(r)["saved"])
            self.assertEqual(self.holdings()["rows"][0]["bank"], 4)
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
