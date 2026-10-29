"""Synthetic markets only: no game interaction and no production DB writes."""
import json
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from market import Store

ROOT = Path(__file__).resolve().parents[1]
UTC = timezone.utc


class TradingPlanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        shutil.copy(ROOT / "config.json", self.root / "config.json")
        cfg = json.loads((self.root / "config.json").read_text(encoding="utf-8-sig"))
        cfg.update(sales_tax=.08, setup_fee=.025, minimum_margin=.1, transport_per_unit=0, timezone="Europe/Paris", cycle_share=.2, weekday_cycles=2)
        (self.root / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
        self.now = datetime(2026, 10, 27, 12, tzinfo=UTC)  # a Tuesday
        self.store = Store(self.root, clock=lambda: self.now)

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def sold(self, city, day, amount, avg, item="T7_RELIC"):
        # One hourly bucket ending at 11:00 UTC on 2026-10-<day> (Mon 19 ... Sun 25).
        end = datetime(2026, 10, day, 11, tzinfo=UTC)
        self.store.db.execute("INSERT INTO history VALUES(?,?,?,?,?,?,?)", (item, city, 1, end.isoformat(), amount, amount * avg, end.isoformat()))

    def best(self, city, side, day, price):
        at = datetime(2026, 10, day, 9, tzinfo=UTC)
        self.store.db.execute("INSERT OR REPLACE INTO book_stats VALUES(?,?,?,?,?,?,?)", ("T7_RELIC", city, side, at.isoformat(), price, 50, 3))

    def test_weekday_cycle_and_weekend_sweep_sell_across_cities(self):
        for day in range(19, 26):
            self.sold("3008", day, 300, 100)         # busy Martlock: 7,200 a day
            for city in ("1002", "5003"):            # Brecilien (5003) is not a trading city
                self.sold(city, day, 100, 100)       # 2,400 a day
            self.sold("2004", day, 5, 100)           # too quiet to pick
        for day in (19, 20, 21):                     # weekday quotes
            self.best("3008", "buy", day, 80); self.best("3008", "sell", day, 95)
            self.best("1002", "buy", day, 85); self.best("1002", "sell", day, 120)
            self.best("5003", "buy", day, 10); self.best("5003", "sell", day, 20)
        for day in (23, 24, 25):                     # weekend asks
            self.best("1002", "sell", day, 150); self.best("3008", "sell", day, 110)
        row = next(r for r in self.store.state()["portfolio"]["rows"] if r["item"] == "T7_RELIC")
        keep = 1 - .08 - .025
        w = row["weekday"]
        # Buy orders at Martlock's bid take 20% of the half of its 7,200 a day (720). Lymhurst takes
        # 20% of the half of its 2,400 (240) at the best margin; the rest is sold in Martlock.
        self.assertEqual((w["method"], w["buy_city"], w["buy"], w["buy_basis"]), ("order", "3008", 80, "호가 기록"))
        self.assertEqual([(l["sell_city"], l["sell"], l["units"]) for l in w["legs"]], [("1002", 120, 240), ("3008", 95, 480)])
        self.assertAlmostEqual(w["legs"][0]["margin"], 120 * keep - 80 * 1.025)
        self.assertAlmostEqual(w["profit"], 240 * (120 * keep - 82) + 480 * (95 * keep - 82))
        self.assertEqual((w["units"], w["turn_units"], w["turns"]), (720, 360, 2))
        self.assertAlmostEqual(w["profit_week"], w["profit"] * 4)
        other = row["weekday_other"]
        self.assertEqual((other["method"], other["buy_city"], [l["sell_city"] for l in other["legs"]]), ("instant", "3008", ["1002"]))
        # Weekend: buy at Martlock's weekday ask, sell at the weekend asks over 2.25 days in both cities.
        k = row["weekend"]
        self.assertEqual((k["buy_city"], k["buy"]), ("3008", 95))
        self.assertEqual([(l["sell_city"], l["sell"], l["units"]) for l in k["legs"]], [("1002", 150, 540), ("3008", 110, 1620)])
        self.assertEqual(k["units"], 2160)

    def test_transport_cost_keeps_sales_in_the_buy_city(self):
        self.store.save_config({"transport_per_unit": 30})
        for day in range(19, 26):
            for city in ("3008", "1002"):
                self.sold(city, day, 100, 100)
        for day in (19, 20, 21):
            self.best("3008", "buy", day, 80); self.best("3008", "sell", day, 110)
            self.best("1002", "buy", day, 85); self.best("1002", "sell", day, 120)
        w = next(r for r in self.store.state()["portfolio"]["rows"] if r["item"] == "T7_RELIC")["weekday"]
        # Carrying costs 30 a unit, more than either price gap: every unit is sold where it was bought.
        self.assertEqual([l["sell_city"] for l in w["legs"]], [w["buy_city"]])
        self.store.save_config({"transport_per_unit": 0})
        self.store._plan_key = None
        w = next(r for r in self.store.state()["portfolio"]["rows"] if r["item"] == "T7_RELIC")["weekday"]
        self.assertEqual((w["buy_city"], [l["sell_city"] for l in w["legs"]]), ("3008", ["1002"]))   # free to carry: buy cheap, sell dear

    def test_silver_per_plan_fills_best_return_first_and_stock_goes_first(self):
        plan = lambda units, cost, margin: {"method": "order", "units": units, "cost": cost, "margin": margin, "profit": units * margin, "roi": margin / cost}
        none = {"weekday": None, "weekday_other": None, "weekend": None, "swing": None, "buy_hours": None, "sell_hours": None, "weekend_hours": None}
        self.store.plan_inputs = lambda cache, books: {
            "T7_RELIC": {**none, "weekday": plan(600, 100, 10), "weekend": plan(100.7, 100, 20)},
            "T8_SOUL": {**none, "weekday": plan(100, 200, 50)},
            "T8_RELIC": none, "T8_RUNE": none}
        held = lambda units: {"held": units, "value": units * 100.0, "orders_silver": 0.0}
        capital = {"total": 25000, "items": {"T7_RELIC": held(700), "T8_SOUL": held(20)}}
        pf = self.store.portfolio({}, {}, capital, {"summary": []})
        rows = {r["item"]: r for r in pf["rows"]}
        relic, soul = rows["T7_RELIC"]["weekday"], rows["T8_SOUL"]["weekday"]
        # A turn: half a day's units. Souls earn more per silver tied (2.0 vs 0.8 a week), so they fill first.
        self.assertEqual((relic["turn_units"], relic["turn_silver"], soul["turn_units"], soul["turn_silver"]), (300, 30000, 50, 10000))
        self.assertEqual((soul["silver"], relic["silver"]), (10000, 15000))
        self.assertEqual((pf["weekday_share"], pf["weekday_need"]), (1, 40000))
        # Units held go first: 700 relics cover more than two turns; 20 souls leave 30 to buy.
        self.assertAlmostEqual(relic["stock_turns"], 700 / 300)
        self.assertEqual((relic["to_buy"], soul["to_buy"]), (0, 30))
        weekend = rows["T7_RELIC"]["weekend"]
        self.assertEqual((weekend["units"], weekend["need"], weekend["silver"], weekend["share"]), (100, 10000, 10000, .4))
        self.assertIsNone(rows["T8_RELIC"]["weekday"])

    def test_plan_settings_are_checked(self):
        self.store.save_config({"cycle_share": .35, "weekday_cycles": 3})
        self.assertEqual((self.store.cfg["cycle_share"], self.store.cfg["weekday_cycles"]), (.35, 3))
        for bad in ({"cycle_share": 0}, {"cycle_share": 1.5}, {"weekday_cycles": 0}, {"weekday_cycles": 2.5}, {"weekday_cycles": 13}):
            with self.assertRaises(ValueError, msg=bad):
                self.store.save_config(bad)

    def test_hours_to_buy_and_sell(self):
        zone = ZoneInfo("Europe/Paris")
        def day(date):
            for hour in range(24):
                start = datetime(2026, 10, date, hour, tzinfo=zone).astimezone(UTC)
                price, amount = (90, 10) if hour in (3, 4, 5) else (110, 30) if hour in (17, 18, 19) else (100, 10)
                end = start + timedelta(hours=1)
                self.store.db.execute("INSERT INTO history VALUES(?,?,?,?,?,?,?)", ("T7_RELIC", "1002", 1, end.isoformat(), amount, amount * price, end.isoformat()))
        day(20); day(21)
        self.assertEqual(self.store.portfolio_hours({}, "T7_RELIC", "1002", True), {"hours": [], "days": 2, "enough": False, "clear": False})
        day(22)
        average = (18 * 10 * 100 + 3 * 10 * 90 + 3 * 30 * 110) / (21 * 10 + 3 * 30)
        cheap = self.store.portfolio_hours({}, "T7_RELIC", "1002", True)
        self.assertEqual([h["hour"] for h in cheap["hours"]], [3, 4, 5])
        self.assertAlmostEqual(cheap["hours"][0]["vs_average"], 90 / average - 1)
        self.assertTrue(cheap["enough"] and cheap["clear"])
        dear = self.store.portfolio_hours({}, "T7_RELIC", "1002", False)
        self.assertEqual([(h["hour"], h["per_hour"]) for h in dear["hours"]], [(17, 30), (18, 30), (19, 30)])

    def test_my_sale_mails_are_shown_beside_the_plan(self):
        none = {"weekday": None, "weekday_other": None, "weekend": None, "swing": None, "buy_hours": None, "sell_hours": None, "weekend_hours": None}
        self.store.plan_inputs = lambda cache, books: {item: none for item in ("T7_RELIC", "T8_SOUL", "T8_RELIC", "T8_RUNE")}
        purchases = {"summary": [{"item": "T7_RELIC", "all": {"cost_average": 100}}]}
        for mail, amount, price, days in (("1", 60, 130, 2), ("2", 50, 120, 40)):   # the second is older than 28 days
            at = (self.now - timedelta(days=days)).isoformat()
            self.store.db.execute("INSERT INTO personal_sales(identity,mail_id,item,city,price,amount,first_seen,last_seen,identity_known,mailed) VALUES(?,?,?,?,?,?,?,?,1,?)",
                                  ("me", mail, "T7_RELIC", "1002", price, amount, at, at, at))
        mine = self.store.portfolio({}, {}, {"total": 0, "items": {}}, purchases)["rows"][0]["mine"]
        self.assertEqual(mine["units"], 60)
        self.assertAlmostEqual(mine["roi"], 130 * (1 - .08 - .025) / 100 - 1)


    def test_units_under_the_buy_target_per_city(self):
        seen = self.now.isoformat()
        def book(*levels, single=False):
            return {"levels": [{"price": p, "amount": a} for p, a in levels], "single_only": single, "seen": seen if levels else None, "fresh": bool(levels)}
        empty = book()
        books = {item: {c: {"sell": empty, "buy": empty} for c in ("7", "4002", "1002", "3008", "2004", "5003")} for item in self.store.cfg["items"]}
        soul = books["T8_SOUL"]
        soul["4002"]["sell"] = book((14000, 3), (15000, 9))            # the dearest lowest ask: sell here
        soul["1002"]["sell"] = book((9500, 4), (10000, 6), (10500, 20))
        soul["7"]["sell"] = book((9000, 1), single=True)                # a clicked order: not a list
        soul["5003"]["sell"] = book((5000, 99))                         # Brecilien is not a trading city
        self.store.save_config({"buy_targets": {"T8_SOUL": 10000}, "transport_per_unit": 50})
        u = next(r for r in self.store.buy_under(books) if r["item"] == "T8_SOUL")
        keep = 1 - .08 - .025
        self.assertEqual((u["target"], u["chosen"], u["sell_city"], u["sell"]), (10000, 10000, "4002", 14000))
        self.assertEqual(u["suggested"], int(14000 * keep / 1.1 - 50))
        rows = {r["city"]: r for r in u["rows"]}
        self.assertNotIn("5003", rows)
        self.assertEqual((rows["1002"]["units"], rows["1002"]["cost"], rows["1002"]["lowest"]), (10, 9500 * 4 + 10000 * 6, 9500))
        self.assertAlmostEqual(rows["1002"]["profit"], 10 * (14000 * keep - 50) - (9500 * 4 + 10000 * 6))
        self.assertFalse(rows["7"]["known"])
        self.assertEqual((rows["4002"]["units"], u["units"]), (0, 10))
        # No target set: the suggestion is used.
        self.store.save_config({"buy_targets": {}})
        u = next(r for r in self.store.buy_under(books) if r["item"] == "T8_SOUL")
        self.assertEqual((u["chosen"], u["target"]), (None, u["suggested"]))
        for bad in ({"buy_targets": {"NOPE": 1}}, {"buy_targets": {"T8_SOUL": 0}}, {"buy_targets": {"T8_SOUL": 1.5}}, {"buy_targets": []}):
            with self.assertRaises(ValueError, msg=bad):
                self.store.save_config(bad)


if __name__ == "__main__":
    unittest.main()
