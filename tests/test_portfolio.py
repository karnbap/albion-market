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


class PortfolioTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        shutil.copy(ROOT / "config.json", self.root / "config.json")
        cfg = json.loads((self.root / "config.json").read_text(encoding="utf-8-sig"))
        cfg.update(sales_tax=.08, setup_fee=.025, transport_per_unit=0, timezone="Europe/Paris")
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

    def best(self, city, side, day, price, minute=0):
        at = datetime(2026, 10, day, 9, minute, tzinfo=UTC)
        self.store.db.execute("INSERT OR REPLACE INTO book_stats VALUES(?,?,?,?,?,?,?)", ("T7_RELIC", city, side, at.isoformat(), price, 50, 3))

    def test_buy_and_sell_city_prices_and_weekdays(self):
        for day in (19, 20, 21, 22):            # Mon-Thu
            self.sold("3008", day, 100, 90)     # cheapest busy market on buy days
            self.sold("1002", day, 100, 100)
            self.sold("5003", day, 100, 50)     # Brecilien: not a trading city
            self.sold("2004", day, 5, 10)       # too quiet to pick
        for day in (23, 24, 25):                # Fri-Sun
            self.sold("1002", day, 100, 130)    # dearest busy market on sell days
            self.sold("3008", day, 100, 110)
            self.sold("2004", day, 5, 500)
        for day in (19, 20, 21):
            self.best("3008", "buy", day, 85 + day - 19)
        for day in (23, 24, 25):
            self.best("1002", "sell", day, 140 + day - 23)
        plan = next(r for r in self.store.state()["portfolio"]["rows"] if r["item"] == "T7_RELIC")["plan"]
        self.assertEqual((plan["buy_city"], plan["sell_city"]), ("3008", "1002"))
        self.assertEqual((plan["buy"], plan["sell"], plan["buy_basis"], plan["sell_basis"]), (86, 141, "호가 기록", "호가 기록"))
        self.assertAlmostEqual(plan["roi"], 141 * (1 - .08 - .025) / (86 * 1.025) - 1)
        self.assertEqual((plan["cheapest_day"], plan["dearest_day"]), (0, 4))   # Monday at Martlock, Friday at Lymhurst
        self.assertEqual((plan["sell_per_day"], plan["swing_days"]), (2400, 7))
        # Without sell-day asks: the asks of other days, scaled by the city's sell-day price over its all-day price.
        self.store.db.execute("DELETE FROM book_stats WHERE side='sell'")
        for minute in (0, 10, 20):
            self.best("1002", "sell", 20, 120, minute)
        self.store._portfolio_key = None
        plan = next(r for r in self.store.state()["portfolio"]["rows"] if r["item"] == "T7_RELIC")["plan"]
        self.assertEqual(plan["sell_basis"], "호가 기록 · 요일 보정")
        self.assertAlmostEqual(plan["sell"], 120 * 130 / ((400 * 100 + 300 * 130) / 700))

    def test_weights_follow_return_over_swing_within_caps(self):
        plan = lambda roi, swing, per_day: {"roi": roi, "swing": swing, "sell_per_day": per_day, "cost": 100}
        self.store.portfolio_inputs = lambda cache, books: {
            "T7_RELIC": plan(.10, .05, 1000),       # steady: weight 2, capped by its market at 15%
            "T8_SOUL": plan(.30, .30, 10000),       # jumpy: weight 1, capped at the 50% per item
            "T8_RELIC": plan(-.05, .05, 1000),      # loses money: 0%
            "T8_RUNE": None}                        # no data
        capital = {"total": 100000, "items": {"T7_RELIC": {"value": 50000, "orders_silver": 10000}, "T8_RELIC": {"value": 5000, "orders_silver": 0}}}
        pf = self.store.portfolio({}, {}, capital, {"summary": []})
        rows = {r["item"]: r for r in pf["rows"]}
        self.assertAlmostEqual(rows["T7_RELIC"]["target"], .05 * 1000 * 3 * 100 / 100000)
        self.assertTrue(rows["T7_RELIC"]["capped"])
        self.assertAlmostEqual(rows["T8_SOUL"]["target"], .5)
        self.assertEqual((rows["T8_RELIC"]["target"], rows["T8_RUNE"]["target"]), (0, 0))
        self.assertAlmostEqual(pf["cash"], .35)
        self.assertAlmostEqual(rows["T7_RELIC"]["gap_silver"], 15000 - 60000)
        self.assertAlmostEqual(rows["T8_RELIC"]["current"], .05)
        # Uncapped weights split in proportion to return over swing.
        self.store.portfolio_inputs = lambda cache, books: {"T7_RELIC": plan(.10, .10, 10**6), "T8_SOUL": plan(.10, .10, 10**6), "T8_RELIC": plan(.10, .20, 10**6), "T8_RUNE": None}
        pf = self.store.portfolio({}, {}, capital, {"summary": []})
        self.assertEqual([round(r["target"], 9) for r in pf["rows"]], [.4, .4, .2, 0])
        self.assertAlmostEqual(pf["cash"], 0)

    def test_units_to_buy_this_week(self):
        self.store.portfolio_inputs = lambda cache, books: {
            "T7_RELIC": {"roi": .10, "swing": .05, "sell_per_day": 1000, "cost": 100}, "T8_SOUL": None, "T8_RELIC": None, "T8_RUNE": None}
        capital = {"total": 100000, "items": {"T7_RELIC": {"value": 2000, "orders_silver": 1000}}}
        week = self.store.portfolio({}, {}, capital, {"summary": []})["rows"][0]["week"]
        # Target 15% of 100,000 less 3,000 held, at 100 each, over Tuesday to Thursday.
        self.assertEqual(week, {"units": 120, "per_day": 40, "days_left": 3, "next_week": False, "sellable": 150})
        self.now = datetime(2026, 10, 30, 12, tzinfo=UTC)   # Friday: next week's four buy days
        week = self.store.portfolio({}, {}, capital, {"summary": []})["rows"][0]["week"]
        self.assertEqual((week["per_day"], week["days_left"], week["next_week"]), (30, 4, True))

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

    def test_my_sales_replace_the_market_return_once_there_are_enough(self):
        self.store.portfolio_inputs = lambda cache, books: {
            "T7_RELIC": {"roi": -.02, "swing": .05, "sell_per_day": 10**6, "cost": 100}, "T8_SOUL": None, "T8_RELIC": None, "T8_RUNE": None}
        purchases = {"summary": [{"item": "T7_RELIC", "all": {"cost_average": 100}}]}
        def sale(mail, amount, price, at):
            self.store.db.execute("INSERT INTO personal_sales(identity,mail_id,item,city,price,amount,first_seen,last_seen,identity_known,mailed) VALUES(?,?,?,?,?,?,?,?,1,?)",
                                  ("me", mail, "T7_RELIC", "1002", price, amount, at.isoformat(), at.isoformat(), at.isoformat()))
        sale("1", 60, 130, self.now - timedelta(days=2))
        sale("2", 50, 120, self.now - timedelta(days=40))   # older than 28 days: left out
        capital = {"total": 1000, "items": {}}
        row = self.store.portfolio({}, {}, capital, purchases)["rows"][0]
        self.assertEqual((row["roi_basis"], row["target"], row["mine"]["units"]), ("market", 0, 60))
        sale("3", 40, 120, self.now - timedelta(days=1))
        row = self.store.portfolio({}, {}, capital, purchases)["rows"][0]
        self.assertEqual(row["roi_basis"], "mine")
        self.assertAlmostEqual(row["roi"], (60 * 130 + 40 * 120) / 100 * (1 - .08 - .025) / 100 - 1)
        self.assertAlmostEqual(row["target"], .5)


if __name__ == "__main__":
    unittest.main()
