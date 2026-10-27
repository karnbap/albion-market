"""Synthetic holdings only: no game interaction and no production DB writes."""
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

    def test_finished_tab_lists_are_kept_but_not_counted(self):
        entry = {"Id": 5, "Amount": 2, "IsFinished": True, "AuctionType": "request", "HasBuyerFetched": False,
                 "HasSellerFetched": True, "ItemTypeId": "T7_RELIC", "LocationId": None}
        other = dict(entry, Id=6, ItemTypeId="T4_BAG")
        self.assertEqual(self.store.ingest_finished({"LocationId": "1000-HellDen", "Entries": [entry, other]})["accepted"], 2)
        h = self.holdings()
        self.assertEqual(h["rows"], [])
        self.assertEqual([(f["location"], f["city"], f["entries"], f["tracked"]) for f in h["finished"]], [("1000-HellDen", "1002", 2, 1)])
        # Only the latest list per market is kept; an empty list says the tab is empty.
        self.now += timedelta(minutes=5)
        self.store.ingest_finished({"LocationId": "1000-HellDen", "Entries": []})
        self.assertEqual([(f["entries"], f["received"]) for f in self.holdings()["finished"]], [(0, self.now.isoformat())])
        for bad in ([], {"LocationId": "1002"}, {"LocationId": "1002", "Entries": ["x"]}, {"Entries": []}):
            with self.assertRaises(ValueError, msg=bad):
                self.store.ingest_finished(bad)

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
