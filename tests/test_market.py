"""Synthetic fixtures only: tests never write into production data/."""
import copy
import json
import os
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from http.server import ThreadingHTTPServer

from market import Store, handler_class, parse_time, describe_depth, DEPTH_DEFAULTS, capture_status

ROOT = Path(__file__).resolve().parents[1]
UTC = timezone.utc


class MarketTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        shutil.copy(ROOT / "config.json", self.root / "config.json")
        cfg = json.loads((self.root / "config.json").read_text(encoding="utf-8"))
        cfg.update(sales_tax=.08, setup_fee=.025, fees_confirmed=False, minimum_margin=.03, fresh_minutes=15)
        (self.root / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
        self.now = datetime(2026, 10, 24, 12, tzinfo=UTC)
        self.store = Store(self.root, clock=lambda: self.now)

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def order(self, oid=1, price=9042, amount=1, side="offer"):
        return {"Id": oid, "ItemTypeId": "T7_RELIC", "LocationId": "1002", "QualityLevel": 1,
                "EnchantmentLevel": 0, "UnitPriceSilver": int(price * 10000), "Amount": amount,
                "AuctionType": side, "Expires": (self.now + timedelta(days=1)).isoformat()}

    def capture(self, *orders):
        return self.store.ingest("marketorders.ingest", {"Orders": list(orders)})

    def history(self, hours=1, end=None, amount=10, avg=100, scale=None):
        end = end or self.now
        ticks = int(end.timestamp()) * 10000000 + 621355968000000000
        return {"AlbionId": 2031, "LocationId": "1002", "QualityLevel": 1, "Timescale": scale if scale is not None else (0 if hours == 1 else 1),
                "Histories": [{"ItemAmount": amount, "SilverAmount": int(amount * avg * 10000), "Timestamp": ticks}]}

    def save_history(self, **kwargs):
        return self.store.ingest("markethistories.ingest", self.history(**kwargs))

    def test_order_prices_and_same_price_aggregation(self):
        self.capture(self.order(1), self.order(2), self.order(3))
        level = self.store.book("T7_RELIC", "1002", "sell")["levels"][0]
        self.assertEqual((level["price"], level["amount"], level["orders"]), (9042, 3, 3))

    def test_capture_status_missing_or_invalid_pid_is_off(self):
        self.assertFalse(capture_status(self.root)['running'])
        (self.root/'data/capture.pid').write_text('invalid',encoding='utf-8')
        self.assertFalse(capture_status(self.root)['running'])

    def test_reused_pid_of_another_program_is_not_capture(self):
        if os.name != 'nt':
            self.skipTest('Windows process identity check')
        (self.root/'data/capture.pid').write_text(str(os.getpid()),encoding='utf-8')
        self.assertFalse(capture_status(self.root)['running'])

    def test_rereading_order_does_not_double_count(self):
        self.capture(self.order(amount=50))
        self.capture(self.order(amount=20))
        self.assertEqual(self.store.book("T7_RELIC", "1002", "sell")["levels"][0]["amount"], 20)

    def test_new_scan_excludes_old_orders(self):
        self.capture(self.order(1, amount=50))
        self.now += timedelta(seconds=121)
        self.capture(self.order(2, amount=3))
        self.assertEqual(self.store.book("T7_RELIC", "1002", "sell")["levels"][0]["amount"], 3)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM sessions WHERE side='sell'").fetchone()[0], 2)

    def test_reopened_page_removes_missing_cheapest_order(self):
        self.capture(*[self.order(i, 100+i, 10) for i in range(1, 51)])
        self.now += timedelta(seconds=5)
        self.capture(*[self.order(i, 100+i, 10) for i in range(2, 52)])
        book = self.store.book('T7_RELIC', '1002', 'sell')
        self.assertEqual(book['levels'][0]['price'], 102)
        self.assertEqual(len(book['levels']), 50)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM packets").fetchone()[0], 2)

    def test_reopened_buy_page_removes_missing_highest_bid(self):
        self.capture(*[self.order(i, 1000-i, 10, 'request') for i in range(1, 51)])
        self.capture(*[self.order(i, 1000-i, 10, 'request') for i in range(2, 52)])
        self.assertEqual(self.store.book('T7_RELIC', '1002', 'buy')['levels'][0]['price'], 998)

    def test_pagination_is_retained_and_single_order_does_not_clear_page(self):
        self.capture(*[self.order(i, 100+i, 10) for i in range(1, 51)])
        self.capture(*[self.order(i, 100+i, 10) for i in range(51, 101)])
        self.capture(self.order(7, 107, 15))
        book = self.store.book('T7_RELIC', '1002', 'sell')
        self.assertEqual(len(book['levels']), 100)
        self.capture(*[self.order(i, 100+i, 10) for i in range(2, 52)])
        book = self.store.book('T7_RELIC', '1002', 'sell')
        self.assertEqual(book['levels'][0]['price'], 102)
        self.assertEqual(book['levels'][-1]['price'], 200)
        self.assertEqual(len(book['levels']), 99)

    def test_market_switch_starts_new_scan_without_manual_reset(self):
        self.capture(self.order(1, 100, 500))
        other = self.order(2, 300, 10)
        other['ItemTypeId'] = 'T8_SOUL'
        self.capture(other)
        self.capture(self.order(3, 110, 200))
        book = self.store.book('T7_RELIC', '1002', 'sell')
        self.assertEqual([r['price'] for r in book['levels']], [110])

    def test_page_refresh_after_restart_preserves_page_evidence(self):
        self.capture(*[self.order(i, 100+i, 10) for i in range(1, 51)])
        other = Store(self.root, clock=lambda: self.now)
        try:
            other.ingest('marketorders.ingest', {'Orders':[self.order(i,100+i,10) for i in range(2,52)]})
            self.assertEqual(other.book('T7_RELIC','1002','sell')['levels'][0]['price'],102)
        finally:
            other.db.close()

    def test_tiny_partial_response_cannot_delete_a_full_page(self):
        self.capture(*[self.order(i, 100+i, 10) for i in range(1, 51)])
        self.capture(self.order(2,102,10), self.order(3,103,10))
        self.assertEqual(len(self.store.book('T7_RELIC','1002','sell')['levels']),50)

    def test_old_version_page_evidence_is_recovered_from_raw_packets(self):
        self.capture(*[self.order(i, 100+i, 10) for i in range(1, 51)])
        with self.store.db:
            self.store.db.execute('DELETE FROM observed_pages')
        self.now += timedelta(seconds=1)
        self.capture(*[self.order(i, 100+i, 10) for i in range(2, 52)])
        self.assertEqual(self.store.book('T7_RELIC','1002','sell')['levels'][0]['price'],102)

    def test_selected_expensive_order_does_not_replace_previous_market_minimum(self):
        self.capture(*[self.order(i, 100+i, 10) for i in range(1, 51)])
        self.now += timedelta(minutes=3)
        self.capture(self.order(999, 10000, 1))
        book = self.store.book('T7_RELIC','1002','sell')
        self.assertEqual(book['levels'][0]['price'],101)
        self.assertTrue(book['single_only'])
        self.assertFalse(book['fresh'])
        self.assertEqual(book['fresh_levels'],[])

    def test_bulk_after_selected_order_establishes_new_snapshot(self):
        self.capture(*[self.order(i, 100+i, 10) for i in range(1, 51)])
        self.now += timedelta(minutes=3)
        self.capture(self.order(999, 10000, 1))
        self.capture(*[self.order(i, 100+i, 10) for i in range(2, 52)])
        book = self.store.book('T7_RELIC','1002','sell')
        self.assertFalse(book['single_only'])
        self.assertEqual(book['levels'][0]['price'],102)

    def test_expired_and_stale_orders_are_not_current(self):
        expired = self.order(2)
        expired["Expires"] = (self.now - timedelta(seconds=1)).isoformat()
        self.capture(self.order(), expired)
        book = self.store.book("T7_RELIC", "1002", "sell")
        self.assertEqual(book["levels"][0]["amount"], 2)
        # Same-price quantities include expired observations, so this mixed level
        # must not be treated as a fresh aggregate.
        self.assertEqual(book["fresh_levels"], [])
        self.now += timedelta(minutes=16)
        self.assertFalse(self.store.book("T7_RELIC", "1002", "sell")["fresh"])

    def test_explicit_reset(self):
        self.capture(self.order())
        self.store.reset_book("T7_RELIC", "1002")
        book = self.store.book("T7_RELIC", "1002", "sell")
        self.assertEqual(book["levels"][0]["price"], 9042)
        self.assertTrue(book["awaiting_scan"])
        self.assertFalse(book["fresh"])

    def test_expired_snapshot_remains_visible_after_restart(self):
        self.capture(self.order(amount=432))
        self.now += timedelta(days=3)
        other = Store(self.root, clock=lambda: self.now)
        try:
            book = other.book('T7_RELIC', '1002', 'sell')
            self.assertEqual(book['levels'][0]['amount'], 432)
            self.assertTrue(book['stale'])
            self.assertEqual(book['fresh_levels'], [])
            self.assertEqual(other.state()['candidates'], [])
        finally:
            other.db.close()

    def test_new_capture_replaces_reset_fallback_without_merging(self):
        self.capture(self.order(1, 100, 500))
        self.store.reset_book('T7_RELIC', '1002')
        self.capture(self.order(2, 110, 200))
        book = self.store.book('T7_RELIC', '1002', 'sell')
        self.assertEqual([r['price'] for r in book['levels']], [110])
        self.assertFalse(book['awaiting_scan'])

    def test_ignores_other_items_quality_and_unknown_location(self):
        wrong = self.order()
        wrong["ItemTypeId"] = "T4_BAG"
        quality = self.order(2)
        quality["QualityLevel"] = 2
        loc = self.order(3)
        loc["LocationId"] = "unknown"
        self.assertEqual(self.capture(wrong, quality, loc)["accepted"], 0)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM packets").fetchone()[0], 0)

    def test_stale_books_remain_visible_but_not_actionable(self):
        self.capture(self.order(amount=500))
        self.now += timedelta(minutes=16)
        book = self.store.book("T7_RELIC", "1002", "sell")
        self.assertEqual(book["levels"][0]["amount"], 500)
        self.assertTrue(book["stale"])
        self.assertFalse(book["fresh"])
        self.assertEqual(book["fresh_levels"], [])

    def test_small_prefix_and_large_reference(self):
        self.capture(self.order(1,100,1),self.order(2,101,8),self.order(3,110,1000))
        book=self.store.book("T7_RELIC","1002","sell")
        depth=book["depth"]
        self.assertEqual(depth["leading_small_amount"],9)
        self.assertEqual(depth["leading_small_levels"],2)
        self.assertTrue(depth["small_dominated"])
        self.assertEqual(depth["reference"]["price"],110)
        self.assertTrue(book["levels"][-1]["wall"])

    def test_single_unit_maximum_is_not_a_wall(self):
        self.capture(self.order(amount=1))
        book=self.store.book("T7_RELIC","1002","sell")
        self.assertFalse(book["levels"][0]["wall"])
        self.assertIsNone(book["depth"]["reference"])

    def test_huge_distant_bid_does_not_define_market_reference(self):
        self.capture(self.order(1,100,2,'request'),self.order(2,99,100,'request'),self.order(3,1,99999,'request'))
        depth=self.store.book('T7_RELIC','1002','buy')['depth']
        self.assertEqual(depth['reference']['price'],99)
        self.assertEqual(depth['dominant']['price'],99)
        self.assertEqual(depth['distant_large_levels'][0]['price'],1)

    def test_quantity_at_price_sums_small_individual_orders(self):
        self.capture(*[self.order(i,100,5) for i in range(25)])
        level=self.store.book('T7_RELIC','1002','sell')['levels'][0]
        self.assertFalse(level['small'])
        self.assertTrue(level['large'])
        self.assertEqual(level['small_orders'],25)

    def test_depth_candidates_show_queue_and_condition(self):
        self.capture(self.order(1,100,2,'request'),self.order(2,99,1000,'request'),self.order(3,130,3),self.order(4,140,2000))
        candidates=self.store.state()['candidates']
        self.assertEqual(len(candidates),2)
        conditional=next(c for c in candidates if c['mode']=='depth')
        self.assertEqual((conditional['buy_price'],conditional['sell_price']),(100,139))
        self.assertEqual((conditional['buy_ahead'],conditional['sell_ahead']),(2,3))
        self.assertIn('조건부',conditional['status'])
        self.assertIsNone(conditional['quantity_recommendation'])

    def test_settings_persist_and_reject_nonfinite_or_reversed_thresholds(self):
        self.store.save_config({'large_quantity_min':200,'wall_quantity_min':600})
        other=Store(self.root,clock=lambda:self.now)
        try: self.assertEqual(other.cfg['large_quantity_min'],200)
        finally: other.db.close()
        for payload in ({'transport_per_unit':float('nan')},{'large_quantity_min':5},{'wall_quantity_min':100}):
            with self.assertRaises(ValueError): self.store.save_config(payload)

    def test_mixed_freshness_excludes_old_reference_from_analysis(self):
        self.capture(self.order(1, 100, 1000), self.order(2, 110, 200))
        # Revisit one order regularly so the same scan contains both old and new rows.
        for _ in range(16):
            self.now += timedelta(minutes=1)
            self.capture(self.order(2, 110, 200))
        book = self.store.book('T7_RELIC', '1002', 'sell')
        self.assertTrue(book['mixed_freshness'])
        self.assertEqual(book['depth']['reference']['price'], 100)
        self.assertEqual(book['analysis_depth']['reference']['price'], 110)

    def test_candidates_flag_prices_above_observed_history(self):
        self.capture(self.order(1, 100, 100, 'request'), self.order(2, 200, 100), self.order(3, 100, 1, 'request'), self.order(4, 200, 1))
        self.save_history(hours=6, avg=130, amount=600)
        row = self.store.state()['candidates'][0]
        self.assertEqual(row['history_highest_bucket_average'], 130)
        self.assertTrue(any('최고 구간 평균가보다 높음' in r for r in row['reasons']))
        self.assertFalse(row['history_stale'])

    def test_old_history_is_flagged_even_when_just_received(self):
        self.save_history(hours=6, end=self.now - timedelta(hours=13))
        h = self.store.history_summary('T7_RELIC', '1002', 7)
        self.assertTrue(h['stale'])
        self.assertEqual(h['age_hours'], 13)
        self.assertEqual(h['last_observed'], self.now.isoformat())

    def test_crossed_local_orders_are_not_resting_order_candidates(self):
        self.capture(self.order(1, 100, 100, 'request'), self.order(2, 101, 100), self.order(11,100,1,'request'), self.order(12,101,1))
        other = self.order(3, 200, 100)
        other['LocationId'] = '3008'
        other2 = dict(other, Id=13)
        self.capture(other, other2)
        self.assertTrue(self.store.book('T7_RELIC','1002','buy')['fresh'])
        self.assertTrue(self.store.book('T7_RELIC','3008','sell')['fresh'])
        self.assertEqual(self.store.state()['candidates'], [])
        # A sale that would hit the destination bid also requires another model.
        self.store.reset_book('T7_RELIC', '1002')
        self.capture(self.order(4, 100, 100, 'request'), self.order(5, 140, 100), self.order(14,100,1,'request'), self.order(15,140,1))
        bid = self.order(6, 200, 100, 'request')
        bid['LocationId'] = '3008'
        self.capture(bid, dict(bid,Id=16), other, other2)
        self.assertTrue(self.store.book('T7_RELIC','3008','sell')['fresh'])
        self.assertFalse(any(r['sell_city'] == '3008' for r in self.store.state()['candidates']))

    def test_single_order_without_market_list_is_not_a_trade_candidate(self):
        self.capture(self.order(1,100,100,'request'),self.order(2,200,100))
        self.assertTrue(self.store.book('T7_RELIC','1002','sell')['single_only'])
        self.assertEqual(self.store.state()['candidates'],[])

    def test_malformed_packet_is_atomic(self):
        broken = self.order(2)
        broken["Amount"] = -1
        with self.assertRaises(ValueError):
            self.capture(self.order(), broken)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM orders").fetchone()[0], 0)

    def test_history_units_and_weighted_average(self):
        self.save_history(amount=10, avg=100)
        self.save_history(end=self.now-timedelta(hours=1), amount=90, avg=200)
        h = self.store.history_summary("T7_RELIC", "1002", 1)
        self.assertEqual(h["amount"], 100)
        self.assertEqual(h["weighted_average"], 190)
        self.assertEqual(h["highest_bucket_average"], 200)
        self.assertEqual(h["near_high_amount"], 90)

    def test_history_revision_replaces_instead_of_summing(self):
        self.save_history(amount=10)
        self.save_history(amount=25)
        self.assertEqual(self.store.history_summary("T7_RELIC", "1002", 1)["amount"], 25)

    def test_hourly_and_six_hourly_not_double_counted(self):
        self.save_history(hours=6, amount=600)
        for h in range(6):
            self.save_history(end=self.now - timedelta(hours=h), amount=100)
        summary = self.store.history_summary("T7_RELIC", "1002", 7)
        self.assertEqual(summary["amount"], 600)
        self.assertEqual(summary["covered_hours"], 6)
        self.assertEqual(summary["resolution_hours"], 6)
        self.assertEqual(self.store.history_summary("T7_RELIC", "1002", 1)["resolution_hours"], 1)

    def test_7day_and_28day_same_bucket_not_double_counted(self):
        self.save_history(hours=6, scale=1, amount=600)
        self.save_history(hours=6, scale=2, amount=650)
        self.assertEqual(self.store.history_summary("T7_RELIC", "1002", 7)["amount"], 650)

    def test_no_invented_hourly_distribution(self):
        self.save_history(hours=6, amount=600)
        patterns = self.store.patterns("T7_RELIC", "1002")
        self.assertEqual(patterns["observed_hours"], 0)
        self.assertEqual(patterns["hourly"], [])

    def test_weekdays_include_safe_six_hour_buckets_and_all_seven_days(self):
        self.save_history(hours=6, end=datetime(2026,10,20,12,tzinfo=UTC), amount=600)
        p = self.store.patterns('T7_RELIC','1002')
        self.assertEqual(len(p['weekdays']),7)
        tuesday = p['weekdays'][1]
        self.assertEqual((tuesday['total'],tuesday['observed_hours'],tuesday['per_observed_hour']),(600,6,100))
        self.assertIsNone(p['weekdays'][0]['total'])
        self.assertEqual(p['hourly'],[])

    def test_cross_midnight_six_hour_bucket_is_not_assigned_to_one_weekday(self):
        self.save_history(hours=6, end=datetime(2026,10,20,0,tzinfo=UTC), amount=600)
        p = self.store.patterns('T7_RELIC','1002')
        self.assertEqual(p['cross_day_amount'],600)
        self.assertEqual(p['weekday_observed_hours'],0)

    def test_weekday_hourly_and_coarse_overlap_not_double_counted(self):
        end = datetime(2026,10,20,12,tzinfo=UTC)
        self.save_history(hours=6,end=end,amount=600)
        for h in range(6): self.save_history(end=end-timedelta(hours=h),amount=100)
        p = self.store.patterns('T7_RELIC','1002')
        self.assertEqual(p['weekdays'][1]['total'],600)
        self.assertEqual(p['weekdays'][1]['observed_hours'],6)

    def test_partial_overlap_does_not_split_or_estimate_coarse_quantity(self):
        end = datetime(2026,10,20,12,tzinfo=UTC)
        self.save_history(hours=6,end=end,amount=600)
        self.save_history(end=end,amount=100)
        p = self.store.patterns('T7_RELIC','1002')
        self.assertEqual((p['weekdays'][1]['total'],p['weekdays'][1]['observed_hours']),(100,1))

    def test_six_hour_weekday_bucket_counts_actual_hours_across_dst(self):
        self.now = datetime(2026,10,25,12,tzinfo=UTC)
        self.save_history(hours=6,end=datetime(2026,10,25,5,tzinfo=UTC),amount=600)
        p = self.store.patterns('T7_RELIC','1002')
        self.assertEqual((p['weekdays'][6]['total'],p['weekdays'][6]['observed_hours']),(600,6))
        self.assertEqual(p['hourly'],[])

    def test_weekday_zero_sales_distinct_from_missing(self):
        self.save_history(hours=6,end=datetime(2026,10,20,12,tzinfo=UTC),amount=0,avg=0)
        p = self.store.patterns('T7_RELIC','1002')
        self.assertEqual(p['weekdays'][1]['total'],0)
        self.assertIsNone(p['weekdays'][0]['total'])

    def test_city_extremes_fresh_first_and_ties(self):
        def batch(city, bid, ask, base):
            rows = [self.order(base,bid,10,'request'),self.order(base+1,bid,20,'request'),self.order(base+2,ask,10),self.order(base+3,ask,20)]
            for row in rows: row['LocationId']=city
            self.capture(*rows)
        batch('1002',100,200,10)
        self.now += timedelta(minutes=16)
        batch('3008',90,210,20)
        batch('7',90,210,30)
        e=self.store.state()['city_extremes']['T7_RELIC']
        self.assertEqual(e['buy']['price'],90)
        self.assertEqual({r['city'] for r in e['buy']['observations']},{'3008','7'})
        self.assertEqual(e['sell']['price'],210)
        self.now += timedelta(minutes=16)
        e=self.store.state()['city_extremes']['T7_RELIC']
        self.assertFalse(e['buy']['fresh'])
        self.assertEqual(e['buy']['price'],100)
        self.assertEqual(e['sell']['price'],200)

    def test_city_extremes_exclude_single_order_quotes(self):
        self.capture(self.order(1,100,100,'request'),self.order(2,200,100))
        e=self.store.state()['city_extremes']['T7_RELIC']
        self.assertIsNone(e['buy']['price'])
        self.assertIsNone(e['sell']['price'])

    def test_missing_not_zero_and_zero_bucket_retained(self):
        self.assertIsNone(self.store.history_summary("T7_RELIC", "1002", 1)["amount"])
        self.save_history(amount=0, avg=0)
        h = self.store.history_summary("T7_RELIC", "1002", 1)
        self.assertEqual(h["amount"], 0)
        self.assertEqual(h["covered_hours"], 1)
        self.assertIsNone(h["weighted_average"])

    def test_window_edges_not_prorated(self):
        self.now += timedelta(minutes=30)
        self.save_history(end=self.now-timedelta(hours=23), hours=6, amount=600)
        self.assertIsNone(self.store.history_summary("T7_RELIC", "1002", 1)["amount"])

    def test_dst_repeated_hour_counted_as_two_observations(self):
        self.now = datetime(2026, 10, 25, 4, tzinfo=UTC)
        self.save_history(end=datetime(2026, 10, 25, 1, tzinfo=UTC), amount=100)
        self.save_history(end=datetime(2026, 10, 25, 2, tzinfo=UTC), amount=200)
        rows = self.store.patterns("T7_RELIC", "1002")["hourly"]
        repeated = next(r for r in rows if r["key"] == 2)
        self.assertEqual(repeated["observed_hours"], 2)
        self.assertEqual(repeated["per_observed_hour"], 150)

    def test_candidates_include_both_setup_fees(self):
        self.capture(self.order(side="request", price=100, amount=100), self.order(2, price=130), self.order(3,100,1,'request'), self.order(4,130,1))
        candidates = self.store.state()["candidates"]
        self.assertEqual(len(candidates), 1)
        expected = 129 * (1 - .08 - .025) - 101 * 1.025
        self.assertAlmostEqual(candidates[0]["net_per_unit"], round(expected, 2))
        self.assertIsNone(candidates[0]["quantity_recommendation"])
        self.assertIn("수수료 설정 미확인", candidates[0]["reasons"])
        self.now += timedelta(minutes=16)
        self.assertEqual(self.store.state()["candidates"], [])

    def test_original_payload_retained_and_settings_validate(self):
        self.capture(self.order())
        raw = json.loads(self.store.db.execute("SELECT payload FROM packets").fetchone()[0])
        self.assertEqual(raw["Orders"][0]["UnitPriceSilver"], 90420000)
        with self.assertRaises(ValueError):
            self.store.save_config({"sales_tax": 1.5})
        with self.assertRaises(ValueError):
            self.store.save_config({"fees_confirmed": "false"})

    def test_http_ingestion_and_origin_protection(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler_class(self.store, "test-token"))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{server.server_port}"
        try:
            req = urllib.request.Request(url + "/ingest/europe/marketorders.ingest", data=json.dumps({"Orders": [self.order()]}).encode(), headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req) as r:
                self.assertEqual(json.load(r)["accepted"], 1)
            bad = urllib.request.Request(url + "/ingest/europe/marketorders.ingest", data=b'{}', headers={"Origin": "https://evil.example"})
            with self.assertRaises(urllib.error.HTTPError) as result:
                urllib.request.urlopen(bad)
            self.assertEqual(result.exception.code, 403)
            result.exception.close()
            no_token = urllib.request.Request(url + "/api/reset", data=b'{"item":"T7_RELIC","city":"1002"}')
            with self.assertRaises(urllib.error.HTTPError) as result:
                urllib.request.urlopen(no_token)
            self.assertEqual(result.exception.code, 403)
            result.exception.close()
            with urllib.request.urlopen(url + "/api/state") as r:
                self.assertEqual(json.load(r)["packets"], 1)
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
