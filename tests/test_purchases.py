"""Synthetic purchases only: no game interaction and no production DB writes."""
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


class PurchaseTests(unittest.TestCase):
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

    def mail(self, kind, mail_id, received, **fields):
        return {"CharacterId": "character-a", "NotificationType": kind,
                "Notification": {"Id": mail_id, "ItemTypeId": "T8_RUNE", "LocationId": "1002", "Received": received, **fields}}

    def order(self, oid, price, amount):
        return {"Id": oid, "ItemTypeId": "T8_RUNE", "LocationId": "1002", "QualityLevel": 1, "EnchantmentLevel": 0,
                "UnitPriceSilver": int(price * 10000), "Amount": amount, "AuctionType": "request",
                "Expires": (self.now + timedelta(days=20)).isoformat()}

    def purchases(self):
        return self.store.state()["purchases"]

    def test_buy_mails_record_what_was_paid_and_the_setup_fee(self):
        # A completed order can cost less than amount x its unit price; the mail total is what was paid.
        self.store.ingest_sale(self.mail("BuyNotification", 1, "2026-10-26T10:00:00Z", Amount=40, UnitPriceSilver=250, TotalSilver=9600))
        self.store.ingest_sale(self.mail("BuyExpiryNotification", 2, "2026-10-26T11:00:00Z", Amount=100, Bought=20, RefundSilver=20000, UnitPriceSilver=250))
        self.store.ingest_sale(self.mail("BuyExpiryNotification", 3, "2026-10-26T12:00:00Z", Amount=10, Bought=0, RefundSilver=2500, UnitPriceSilver=250))
        p = self.purchases()
        self.assertEqual([(r["source"], r["amount"], r["silver"]) for r in p["recent"]], [("expired_mail", 20, 5000), ("mail", 40, 9600)])
        rune = p["summary"][0]
        self.assertEqual((rune["item"], rune["recent"]["units"], rune["recent"]["silver"]), ("T8_RUNE", 60, 14600))
        self.assertAlmostEqual(rune["recent"]["average"], 14600 / 60)
        self.assertAlmostEqual(rune["recent"]["cost_average"], 14600 * 1.025 / 60)

    def test_open_buy_order_counts_its_filled_units_until_its_mail_arrives(self):
        self.store.ingest_my_orders({"Kind": "buy", "LocationId": "1002", "Orders": [self.order(7, 250, 100)]})
        self.assertEqual(self.purchases()["recent"], [])
        self.now += timedelta(hours=1)
        self.store.ingest_my_orders({"Kind": "buy", "LocationId": "1002", "Orders": [self.order(7, 250, 60)]})
        rows = self.purchases()["recent"]
        self.assertEqual([(r["source"], r["amount"], r["silver"]) for r in rows], [("open_order", 40, 10000)])
        # The order fills up; its mail now covers all 100 units, the open order no longer counts.
        self.store.ingest_sale(self.mail("BuyNotification", 4, "2026-10-27T13:30:00Z", Amount=100, UnitPriceSilver=250, TotalSilver=25000))
        rows = self.purchases()["recent"]
        self.assertEqual([(r["source"], r["amount"]) for r in rows], [("mail", 100)])
        # An older mail at the same price does not hide a newer open order.
        self.now += timedelta(hours=1)
        self.store.ingest_my_orders({"Kind": "buy", "LocationId": "1002", "Orders": [self.order(8, 250, 50)]})
        self.now += timedelta(hours=1)
        self.store.ingest_my_orders({"Kind": "buy", "LocationId": "1002", "Orders": [self.order(8, 250, 45)]})
        self.assertIn(("open_order", 5), [(r["source"], r["amount"]) for r in self.purchases()["recent"]])

    def test_hand_entered_purchases(self):
        instant = self.store.add_purchase({"item": "T7_RELIC", "city": "4002", "kind": "instant", "price": 7000, "amount": 3, "bought": "2026-10-27T10:30"})
        self.store.add_purchase({"item": "T7_RELIC", "city": "4002", "kind": "order", "price": 6000, "amount": 1})
        old = self.store.add_purchase({"item": "T7_RELIC", "city": "4002", "kind": "instant", "price": 9000, "amount": 1, "bought": "2026-09-01"})
        p = self.purchases()
        # Local Paris time (UTC+1 in late October) is stored in UTC; newest first.
        self.assertEqual([(r["source"], r["at"]) for r in p["recent"]][:2], [("manual", "2026-10-27T12:00:00+00:00"), ("manual", "2026-10-27T09:30:00+00:00")])
        relic = p["summary"][0]
        self.assertEqual((relic["recent"]["units"], relic["all"]["units"]), (4, 5))
        self.assertAlmostEqual(relic["recent"]["average"], 27000 / 4)
        self.assertAlmostEqual(relic["recent"]["cost_average"], (21000 + 6000 * 1.025) / 4)
        self.store.remove_purchase(old)
        self.assertEqual(self.purchases()["summary"][0]["all"]["units"], 4)
        with self.assertRaises(ValueError):
            self.store.remove_purchase(old)
        for bad in ({"kind": "gift"}, {"price": 0}, {"amount": 1.5}, {"bought": "2026-10-28T10:00"}, {"item": "T4_BAG"}):
            with self.assertRaises(ValueError, msg=bad):
                self.store.add_purchase({"item": "T7_RELIC", "city": "4002", "kind": "instant", "price": 7000, "amount": 3, **bad})
        self.assertTrue(instant)

    def test_purchases_api_needs_the_local_token(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler_class(self.store, "test-token"))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{server.server_port}/api/purchases"
        body = json.dumps({"item": "T8_SOUL", "city": "3008", "kind": "instant", "price": 8000, "amount": 2}).encode()
        try:
            with self.assertRaises(urllib.error.HTTPError) as result:
                urllib.request.urlopen(urllib.request.Request(url, data=body))
            self.assertEqual(result.exception.code, 403)
            result.exception.close()
            with urllib.request.urlopen(urllib.request.Request(url, data=body, headers={"X-Local-Token": "test-token"})) as r:
                added = json.load(r)["added"]
            remove = json.dumps({"action": "remove", "id": added}).encode()
            with urllib.request.urlopen(urllib.request.Request(url, data=remove, headers={"X-Local-Token": "test-token"})) as r:
                self.assertTrue(json.load(r)["removed"])
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
