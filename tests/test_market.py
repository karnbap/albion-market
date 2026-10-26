"""Synthetic fixtures only: tests never write into production data/."""
import copy
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from collections import Counter
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from http.server import ThreadingHTTPServer
from zoneinfo import ZoneInfo

from market import Store, handler_class, parse_time, describe_depth, DEPTH_DEFAULTS, capture_status, CANDIDATES_PER_ITEM, volume_risk, CITIES

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
        resting = lambda: [r for r in self.store.state()['candidates'] if r['mode'] in ('best', 'depth')]
        self.assertEqual(resting(), [])
        # A sale that would hit the destination bid is the instant model, not a queue.
        self.store.reset_book('T7_RELIC', '1002')
        self.capture(self.order(4, 100, 100, 'request'), self.order(5, 140, 100), self.order(14,100,1,'request'), self.order(15,140,1))
        bid = self.order(6, 200, 100, 'request')
        bid['LocationId'] = '3008'
        self.capture(bid, dict(bid,Id=16), other, other2)
        self.assertTrue(self.store.book('T7_RELIC','3008','sell')['fresh'])
        self.assertFalse(any(r['sell_city'] == '3008' for r in resting()))
        self.assertTrue(any(r['sell_city'] == '3008' and r['mode'] == 'instant' for r in self.store.state()['candidates']))

    def market(self, city, base, asks=(), bids=(), item='T7_RELIC'):
        # Two orders per side: a single order is a selected-order click, not a list.
        rows = [dict(self.order(base+i, price, 10, side), LocationId=city, ItemTypeId=item)
                for i, (price, side) in enumerate([(p, 'offer') for p in asks for _ in range(2)] + [(p, 'request') for p in bids for _ in range(2)])]
        self.capture(*rows)

    def test_instant_route_pays_sales_tax_only(self):
        self.market('1002', 10, asks=[100])
        self.market('3008', 20, bids=[130])
        row = next(r for r in self.store.state()['candidates'] if r['mode'] == 'instant')
        self.assertEqual((row['buy_city'], row['sell_city'], row['buy_price'], row['sell_price']), ('1002', '3008', 100, 130))
        self.assertAlmostEqual(row['net_per_unit'], round(130 * (1 - .08) - 100, 2))
        self.assertEqual((row['buy_available'], row['sell_available']), (20, 20))
        self.assertEqual(row['status'], '바로 매입 · 바로 판매')

    def test_instant_listing_only_above_destination_bid(self):
        self.market('1002', 10, asks=[100])
        self.market('3008', 20, asks=[140], bids=[130])
        row = next(r for r in self.store.state()['candidates'] if r['mode'] == 'instant_list')
        self.assertEqual(row['sell_price'], 139)
        self.assertAlmostEqual(row['net_per_unit'], round(139 * (1 - .08 - .025) - 100, 2))
        self.store.reset_book('T7_RELIC', '3008')
        self.market('3008', 40, asks=[131], bids=[130])
        self.assertFalse(any(r['mode'] == 'instant_list' for r in self.store.state()['candidates']))

    def test_candidates_trimmed_per_item_so_later_items_stay_visible(self):
        for item, base in (('T7_RELIC', 1000), ('T8_RUNE', 5000)):
            for k, city in enumerate(['1002', '2004', '3005', '3008', '4002', '7', '5003']):
                self.market(city, base + 10*k, asks=[100+k], bids=[200+k], item=item)
        counts = Counter(r['item'] for r in self.store.state()['candidates'])
        self.assertEqual(counts, {'T7_RELIC': CANDIDATES_PER_ITEM, 'T8_RUNE': CANDIDATES_PER_ITEM})

    def test_city_board_routes_best_quotes_and_markets_to_reopen(self):
        self.market('1002', 10, asks=[100], bids=[90])
        self.market('3008', 20, asks=[140], bids=[130])
        board = {c['city']: c for c in self.store.state()['city_board']}
        route = board['1002']['routes'][0]
        self.assertEqual((route['item'], route['sell_city'], route['mode']), ('T7_RELIC', '3008', 'best'))
        self.assertEqual(route['alternative']['mode'], 'instant_list')
        self.assertNotIn('수수료 설정 미확인', route['warnings'])
        self.assertEqual(board['3008']['sell_here'][0], {'item': 'T7_RELIC', 'price': 130, 'amount': 20, 'compared': 2, 'lead': 40})
        self.assertEqual(board['1002']['buy_here'][0]['price'], 100)
        self.assertEqual(board['1002']['fresh_items'], 1)
        self.assertEqual({m['item']: m['reason'] for m in board['1002']['missing']}, {'T8_SOUL': '미조회', 'T8_RELIC': '미조회', 'T8_RUNE': '미조회'})
        self.assertEqual(board['3008']['routes'], [])
        self.now += timedelta(minutes=16)
        board = {c['city']: c for c in self.store.state()['city_board']}
        self.assertEqual((board['1002']['routes'], board['1002']['buy_here']), ([], []))
        self.assertEqual(board['1002']['missing'][0], {'item': 'T7_RELIC', 'reason': '지난 자료'})

    def six_hour_history(self, *amounts, avg=120, city='1002'):
        for k, amount in enumerate(amounts):
            payload = self.history(hours=6, end=self.now - timedelta(hours=6*k), amount=amount, avg=avg)
            payload['LocationId'] = city
            self.store.ingest('markethistories.ingest', payload)

    def test_volume_guide_uses_slowest_recent_daily_pace(self):
        self.six_hour_history(30, 30, 30, 30)
        guide = self.store.volume_guide({}, 'T7_RELIC', '1002')
        self.assertEqual([w['per_day'] for w in guide['windows']], [None, 120, 120])
        for h in range(6):
            self.save_history(end=self.now - timedelta(hours=h), amount=2)
        guide = self.store.volume_guide({}, 'T7_RELIC', '1002')
        self.assertEqual(guide['windows'][0]['per_day'], 48)
        self.assertEqual((guide['per_day'], guide['caution_above'], guide['danger_above']), (48, 48, 144))
        self.assertEqual([volume_risk(n, guide) for n in (48, 49, 145)], ['ok', 'caution', 'danger'])
        self.assertEqual(volume_risk(1, self.store.volume_guide({}, 'T8_RUNE', '1002')), 'unknown')

    def test_buyout_lifts_floor_to_next_seen_price_and_relists_under_it(self):
        self.market('1002', 10, asks=[100, 101, 150, 160])
        self.six_hour_history(25, 25, 25, 25)
        state = self.store.state()
        row = state['buyouts'][0]
        best = row['best']
        self.assertEqual((row['item'], row['city'], row['floor_now']), ('T7_RELIC', '1002', 100))
        self.assertEqual((best['units'], best['cost'], best['floor'], best['relist'], best['risk']), (60, 7020, 160, 159, 'ok'))
        self.assertAlmostEqual(best['profit'], round(60 * 159 * (1 - .08 - .025) - 7020, 2))
        self.assertAlmostEqual(best['lift'], .6)
        self.assertEqual([s['units'] for s in row['steps']], [20, 40, 60])
        self.assertTrue(any('7일 최고 구간 평균가보다 높음' in r for r in row['reasons']))
        self.assertIn('T7_RELIC/1002', state['volume_guides'])
        self.assertEqual(next(c for c in state['city_board'] if c['city'] == '1002')['buyout']['best']['units'], 60)

    def test_buyout_never_exceeds_three_days_of_slowest_sales(self):
        self.market('1002', 10, asks=[100, 130, 131, 200])
        self.six_hour_history(3, 3, 2, 2)
        best = self.store.state()['buyouts'][0]['best']
        self.assertEqual((best['units'], best['risk']), (20, 'caution'))
        self.assertTrue(any('다 팔려면 약 2.0일' in r for r in self.store.state()['buyouts'][0]['reasons']))

    def test_buyout_without_history_is_flagged_and_stale_list_ignored(self):
        self.market('1002', 10, asks=[100, 150])
        row = self.store.state()['buyouts'][0]
        self.assertEqual(row['best']['risk'], 'unknown')
        self.assertIn('24시간·7일·28일 판매 이력 부족 · 다 팔린다는 근거 없음', row['reasons'])
        self.now += timedelta(minutes=16)
        self.assertEqual(self.store.state()['buyouts'], [])

    def test_now_walks_both_books_for_an_instant_round_trip(self):
        self.market('1002', 10, asks=[100, 104, 150])
        self.market('3008', 20, bids=[130, 120, 101])
        pick = self.store.state()['now'][0]
        self.assertEqual((pick['kind'], pick['buy_city'], pick['sell_city']), ('instant', '1002', '3008'))
        # 20@100 and 20@104 against bids 130 then 120; the 150 ask and 101 bid do not pay.
        self.assertEqual((pick['units'], pick['buy_low'], pick['buy_high'], pick['sell_high'], pick['sell_low']), (40, 100, 104, 130, 120))
        self.assertEqual(pick['cost'], 4080)
        self.assertEqual(pick['profit'], round(20 * 130 * .92 + 20 * 120 * .92 - 4080))

    def test_now_stays_within_the_trade_budget(self):
        self.store.save_config({'trade_budget': 2500})
        self.market('1002', 10, asks=[100, 104])
        self.market('3008', 20, bids=[130, 129])
        pick = self.store.state()['now'][0]
        # 20 at 100, then only 4 more at 104 fit in 2,500 silver.
        self.assertEqual((pick['units'], pick['cost']), (24, 2416))
        for bad in (0, 1.5, float('inf')):
            with self.assertRaises(ValueError): self.store.save_config({'trade_budget': bad})

    def test_now_caps_listing_by_one_day_of_sales_and_never_reuses_a_sell_list(self):
        self.market('1002', 10, asks=[100, 101])
        self.market('3008', 20, asks=[200])
        self.six_hour_history(3, 3, 2, 2, city='3008')
        picks = self.store.state()['now']
        self.assertEqual([(p['kind'], p['units']) for p in picks], [('instant_list', 10)])

    def test_now_never_sends_two_picks_to_the_same_buyers(self):
        self.market('1002', 10, asks=[100])
        self.market('2004', 20, asks=[101])
        self.market('3008', 30, bids=[130])
        picks = self.store.state()['now']
        self.assertEqual([(p['buy_city'], p['sell_city']) for p in picks], [('1002', '3008')])

    def test_caerleon_is_left_out_of_trading_unless_re_enabled(self):
        self.market('1002', 10, asks=[100])
        self.market('3005', 20, asks=[90, 160], bids=[130])
        state = self.store.state()
        self.assertEqual(state['now'], [])
        self.assertFalse(any('3005' in (c['buy_city'], c['sell_city']) for c in state['candidates']))
        self.assertEqual(state['buyouts'], [])
        self.assertEqual([c['city'] for c in state['city_board'] if c.get('excluded')], ['3005', '5003'])
        self.assertEqual(next(c for c in state['city_board'] if c['city'] == '1002')['buy_here'], [])
        self.store.save_config({'excluded_cities': []})
        pick = next(p for p in self.store.state()['now'] if p['sell_city'] == '3005')
        self.assertIn('Caerleon 이동은 레드존 통과 · 운반 중 약탈 위험', pick['warnings'])
        for bad in (['9999'], '3005', list(CITIES)):
            with self.assertRaises(ValueError): self.store.save_config({'excluded_cities': bad})

    def test_order_book_stats_kept_per_ten_minutes_and_averaged_by_weekday_and_hour(self):
        self.market('1002', 10, asks=[100, 110], bids=[90])
        self.now += timedelta(minutes=3)
        self.market('1002', 20, asks=[104, 110], bids=[92])
        self.capture(self.order(99, 50, 1))  # a selected single order is not a list
        rows = self.store.db.execute("SELECT side,best,amount FROM book_stats ORDER BY side").fetchall()
        self.assertEqual([tuple(r) for r in rows], [('buy', 92, 20), ('sell', 104, 40)])
        self.now += timedelta(days=7)
        bp = self.store.book_patterns('T7_RELIC', '1002')
        local = (self.now - timedelta(days=7)).astimezone(ZoneInfo('Europe/Paris'))
        self.assertEqual(bp['sell']['weekdays'], [{'key': local.weekday(), 'samples': 1, 'best': 104, 'amount': 40}])
        self.assertEqual(bp['buy']['hourly'][0]['key'], local.hour)

    def test_timing_compares_now_with_usual_pace_and_price(self):
        for days in range(1, 15):
            # Buckets starting at the current hour sell 10; two other hours sell 30 and 20.
            end = self.now - timedelta(days=days) + timedelta(hours=1)
            for back, amount in ((0, 10), (3, 30), (6, 20)):
                self.save_history(end=end - timedelta(hours=back), amount=amount)
        for week in (1, 2):
            self.now -= timedelta(days=7 * week)
            self.market('1002', 100 * week, bids=[100])
            self.now += timedelta(days=7 * week)
        self.market('1002', 999, bids=[120])
        cache = {}
        books = {'T7_RELIC': {'1002': {s: self.store.book('T7_RELIC', '1002', s) for s in ('buy', 'sell')}}}
        t = self.store.timing(cache, 'T7_RELIC', '1002', books)
        self.assertAlmostEqual(t['sales_hour'], 10 / 20)
        self.assertAlmostEqual(t['bid_vs_usual'], 120 / ((100 + 100 + 120) / 3) - 1)
        self.assertIsNone(t['ask_vs_usual'])

    def test_picks_are_logged_and_stay_readable_after_prices_go_stale(self):
        self.market('1002', 10, asks=[100])
        self.market('3008', 20, bids=[130])
        first = self.store.state()['now']
        self.store.state()
        self.now += timedelta(minutes=16)
        state = self.store.state()
        self.assertEqual(state['now'], [])
        self.assertEqual(len(state['pick_history']), 1)
        self.assertEqual(state['pick_history'][0]['picks'][0]['units'], first[0]['units'])
        self.assertIn('timing', first[0])

    def hourly_profile(self, rate, city='3008'):
        # 28 days of 1-hour buckets; rate(local_start) gives units sold in that hour.
        zone = ZoneInfo('Europe/Paris')
        rows = []
        for back in range(1, 28 * 24):
            end = self.now.replace(minute=0) - timedelta(hours=back - 1)
            start = (end - timedelta(hours=1)).astimezone(zone)
            amount = rate(start)
            rows.append({"ItemAmount": amount, "SilverAmount": amount * 100 * 10000,
                         "Timestamp": int(end.timestamp()) * 10000000 + 621355968000000000})
        self.store.ingest('markethistories.ingest', {"AlbionId": 2031, "LocationId": city, "QualityLevel": 1, "Timescale": 0, "Histories": rows})

    def test_demand_outlook_uses_weekday_or_weekend_hours_and_flags_quiet_stretch(self):
        # Saturday 14:00 Paris now. Weekend afternoons 14-19h sell 1/h, other weekend hours 10/h, weekdays 50/h.
        self.hourly_profile(lambda t: 50 if t.weekday() < 5 else 1 if 14 <= t.hour < 20 else 10)
        o = self.store.demand_outlook({}, 'T7_RELIC', '3008')
        self.assertTrue(o['weekend'])
        self.assertEqual((o['next6'], o['next24']), (6, 186))
        self.assertTrue(o['low_window'])
        self.assertEqual(o['quiet_hours'], [14, 15, 16, 17, 18, 19])
        self.assertIsNone(self.store.demand_outlook({}, 'T7_RELIC', '1002'))

    def test_waiting_trades_are_capped_by_coming_demand_and_split_from_instant(self):
        self.hourly_profile(lambda t: 1)
        self.market('1002', 10, asks=[100, 101])
        self.market('3008', 20, asks=[200], bids=[130])
        groups = self.store.state()['now_groups']
        self.assertEqual([p['kind'] for p in groups['instant']], ['instant'])
        listing = groups['wait'][0]
        self.assertEqual((listing['kind'], listing['units']), ('instant_list', 24))
        self.assertEqual(listing['outlook']['next24'], 24)

    def test_quiet_hours_warn_waiting_trades(self):
        self.hourly_profile(lambda t: 1 if 14 <= t.hour < 20 else 10)
        self.market('1002', 10, asks=[100, 101])
        self.market('3008', 20, asks=[200])
        pick = self.store.state()['now_groups']['wait'][0]
        self.assertTrue(any('수요가 적은 시간대' in w for w in pick['warnings']))

    def test_shrinking_order_is_recorded_as_a_fill_but_repricing_is_not(self):
        self.capture(self.order(1, 100, 10), self.order(2, 101, 10))
        self.now += timedelta(minutes=5)
        self.capture(self.order(1, 100, 6), self.order(2, 103, 4))
        fills = [tuple(r) for r in self.store.db.execute("SELECT side,order_id,price,amount FROM order_fills")]
        self.assertEqual(fills, [('sell', '1', 100, 4)])

    def stat(self, side, when, best, city='1002'):
        self.store.db.execute("INSERT OR REPLACE INTO book_stats VALUES(?,?,?,?,?,?,?)", ('T7_RELIC', city, side, when.isoformat(), best, 50, 3))

    def test_market_study_estimates_instant_buy_share_undercuts_and_weekday_weekend_trade(self):
        tuesday = datetime(2026, 10, 20, 9, tzinfo=UTC)
        for h in range(3):
            end = tuesday + timedelta(hours=h + 1)
            self.save_history(end=end, amount=10, avg=105)  # bid 100, ask averages 109 within the hour
            for minute in (0, 10, 20, 30, 40, 50):
                at = end - timedelta(hours=1) + timedelta(minutes=minute)
                self.stat('buy', at, 100)
                self.stat('sell', at, 110 if minute < 30 else 108)
        saturday = datetime(2026, 10, 24, 9, tzinfo=UTC)
        self.stat('sell', saturday, 140)
        self.save_history(end=saturday + timedelta(hours=1), amount=20, avg=150)
        study = self.store.market_study('T7_RELIC', '1002', {})
        self.assertAlmostEqual(study['instant_buy_share'], (105 - 100) / (109 - 100))
        self.assertEqual(study['share_samples'], 3)
        # 18 readings 10 minutes apart: watched 170 minutes, the ask dropped once inside each hour.
        self.assertEqual(study['undercuts'], 3)
        self.assertAlmostEqual(study['undercut_hours'], 170 / 60 / 3)
        self.assertEqual((study['weekday']['price'], study['weekend']['price']), (105, 150))
        a = study['plan_a']
        self.assertEqual((a['buy'], a['sell'], a['basis']), (101, 139, '호가 기록'))
        self.assertAlmostEqual(a['margin'], 139 * (1 - .08 - .025) - 101 * 1.025)

    def test_own_order_links_to_scanned_order_and_tracks_fills_and_queue(self):
        mine = self.store.add_my_order({'item': 'T7_RELIC', 'city': '1002', 'side': 'sell', 'price': 120, 'amount': 30})
        self.capture(self.order(1, 100, 5), self.order(2, 120, 30), self.order(3, 120, 30))
        self.now += timedelta(minutes=5)
        self.capture(self.order(1, 100, 5), self.order(2, 120, 25), self.order(3, 120, 30))
        state = self.store.state()
        row = next(r for r in state['my_orders'] if r['id'] == mine)
        self.assertEqual((row['order_id'], row['remaining'], row['filled'], row['ahead'], row['is_best']), ('2', 25, 5, 5, False))
        self.store.close_my_order(mine)
        self.assertEqual(self.store.state()['my_orders'], [])
        for bad in ({'item': 'T7_RELIC', 'city': '1002', 'side': 'sell', 'price': 0, 'amount': 1},
                    {'item': 'T7_RELIC', 'city': '1002', 'side': 'up', 'price': 1, 'amount': 1}):
            with self.assertRaises(ValueError): self.store.add_my_order(bad)

    def test_own_open_sell_orders_cut_waiting_quantity_and_warn_on_undercutting_myself(self):
        self.hourly_profile(lambda t: 1)
        self.market('1002', 10, asks=[100, 101])
        self.market('3008', 20, asks=[200], bids=[130])
        self.store.add_my_order({'item': 'T7_RELIC', 'city': '3008', 'side': 'sell', 'price': 250, 'amount': 10})
        pick = self.store.state()['now_groups']['wait'][0]
        self.assertEqual((pick['kind'], pick['units']), ('instant_list', 14))
        self.assertTrue(any('내 기존 매도 주문 10개가 250에 남아 있음' in w and '뒤로 밀림' in w for w in pick['warnings']))

    def test_buyout_skips_my_own_listings(self):
        self.market('1002', 10, asks=[100, 150])  # two orders of 10 at each price
        self.store.add_my_order({'item': 'T7_RELIC', 'city': '1002', 'side': 'sell', 'price': 100, 'amount': 10})
        best = self.store.state()['buyouts'][0]['best']
        self.assertEqual((best['units'], best['cost']), (10, 1000))
        self.store.add_my_order({'item': 'T7_RELIC', 'city': '1002', 'side': 'sell', 'price': 100, 'amount': 10})
        self.assertEqual(self.store.state()['buyouts'], [])

    def client_log(self, *lines):
        """Append lines in the capture client's log format (ANSI colours included, as on stdout)."""
        path = self.root/'vendor/albiondata-client.log'
        path.parent.mkdir(exist_ok=True)
        with path.open('a', encoding='utf-8') as handle:
            for seconds, text in lines:
                stamp = (self.now - timedelta(seconds=seconds)).astimezone(timezone(timedelta(hours=1))).isoformat(timespec='seconds')
                handle.write(f'\x1b[37mDEBU\x1b[0m[{stamp}] {text}\n')

    def test_my_orders_list_from_client_log_registers_updates_and_closes_own_orders(self):
        self.store.add_my_order({'item': 'T7_RELIC', 'city': '1002', 'side': 'sell', 'price': 120, 'amount': 30})
        self.client_log((2, 'OperationRequest: [94]opAuctionGetMyOpenAuctions - 0:6 9:2 253:94'))
        result = self.capture(self.order(7, 120, 25), self.order(8, 300, 4))
        self.assertEqual(result['accepted'], 2)
        rows = {r['order_id']: r for r in self.store.state()['my_orders']}
        self.assertEqual((rows['7']['source'], rows['7']['amount'], rows['7']['remaining'], rows['7']['filled']), ('manual', 30, 25, 5))
        self.assertEqual((rows['8']['source'], rows['8']['amount'], rows['8']['remaining']), ('auto', 4, 4))
        # The "my orders" answer is not a market page: the order book stays empty.
        self.assertEqual(self.store.book('T7_RELIC', '1002', 'sell')['levels'], [])
        # A normal market search afterwards is a market page again.
        self.now += timedelta(minutes=1)
        self.client_log((1, 'OperationRequest: [81]opAuctionGetOffers - 253:81'))
        self.capture(self.order(1, 100, 5), self.order(7, 120, 20))
        self.assertEqual(len(self.store.book('T7_RELIC', '1002', 'sell')['levels']), 2)
        self.assertEqual(next(r for r in self.store.state()['my_orders'] if r['order_id'] == '7')['remaining'], 20)
        # Order 8 sold out: missing from the next "my orders" list of that city and side.
        self.now += timedelta(minutes=1)
        self.client_log((1, 'OperationRequest: [94]opAuctionGetMyOpenAuctions - 0:6 9:2 253:94'))
        self.capture(self.order(7, 120, 20))
        self.assertEqual([r['order_id'] for r in self.store.state()['my_orders']], ['7'])
        debug = self.store.state()['client_debug']
        self.assertTrue(debug['enabled'])
        self.assertEqual(debug['lines'][0]['op'], 94)

    def test_fork_my_orders_upload_registers_updates_and_closes_own_orders(self):
        self.store.add_my_order({'item': 'T7_RELIC', 'city': '1002', 'side': 'sell', 'price': 120, 'amount': 30})
        stray = self.order(9, 50, 3, 'request')  # a buy order can never be in the sell list
        result = self.store.ingest_my_orders({'Kind': 'sell', 'LocationId': '1002', 'Orders': [self.order(7, 120, 25), self.order(8, 300, 4), stray]})
        self.assertEqual(result['accepted'], 2)
        rows = {r['order_id']: r for r in self.store.state()['my_orders']}
        self.assertEqual(set(rows), {'7', '8'})
        self.assertEqual((rows['7']['source'], rows['7']['remaining'], rows['7']['filled']), ('manual', 25, 5))
        self.assertEqual(rows['8']['source'], 'auto')
        # Own orders are not a market page.
        self.assertEqual(self.store.book('T7_RELIC', '1002', 'sell')['levels'], [])
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM packets WHERE topic='marketorders.ingest'").fetchone()[0], 0)
        self.now += timedelta(minutes=1)
        self.store.ingest_my_orders({'Kind': 'sell', 'LocationId': '1002', 'Orders': [self.order(7, 120, 20)]})
        self.assertEqual([(r['order_id'], r['remaining']) for r in self.store.state()['my_orders']], [('7', 20)])
        with self.assertRaises(ValueError):
            self.store.ingest_my_orders({'Kind': 'mine', 'Orders': []})

    def test_empty_fork_list_closes_that_market_and_side_only(self):
        buy = self.order(20, 90, 5, 'request')
        self.store.ingest_my_orders({'Kind': 'buy', 'LocationId': '1002', 'Orders': [buy]})
        self.store.ingest_my_orders({'Kind': 'sell', 'LocationId': '1002', 'Orders': [self.order(21, 130, 2)]})
        elsewhere = dict(self.order(22, 95, 1, 'request'), LocationId='3008')
        self.store.ingest_my_orders({'Kind': 'buy', 'LocationId': '3008', 'Orders': [elsewhere]})
        self.now += timedelta(minutes=1)
        # Every buy order in Lymhurst filled: the game sends an empty buy list there.
        self.store.ingest_my_orders({'Kind': 'buy', 'LocationId': '1002', 'Orders': []})
        self.assertEqual(sorted(r['order_id'] for r in self.store.state()['my_orders']), ['21', '22'])

    def test_full_fork_page_never_closes_orders_it_may_not_show(self):
        self.store.ingest_my_orders({'Kind': 'sell', 'LocationId': '1002', 'Orders': [self.order(1, 500, 1)]})
        page = [dict(self.order(100 + i, 130, 1), ItemTypeId='T4_BAG') for i in range(50)]
        self.store.ingest_my_orders({'Kind': 'sell', 'LocationId': '1002', 'Orders': page})
        self.assertEqual([r['order_id'] for r in self.store.state()['my_orders']], ['1'])

    def test_fork_log_never_turns_a_market_page_into_own_orders(self):
        self.client_log((5, 'Starting Albion Data Client, version: 0.1.61-local.1'),
                        (2, 'OperationRequest: [94]opAuctionGetMyOpenAuctions - 0:6 9:2 253:94'))
        self.capture(self.order(1, 100, 5), self.order(2, 120, 3))
        self.assertEqual(self.store.state()['my_orders'], [])
        self.assertEqual(len(self.store.book('T7_RELIC', '1002', 'sell')['levels']), 2)
        self.assertTrue(self.store.state()['client_debug']['fork'])

    def test_stale_my_orders_request_does_not_claim_market_pages(self):
        self.client_log((300, 'OperationRequest: [94]opAuctionGetMyOpenAuctions - 0:6 9:2 253:94'))
        self.capture(self.order(1, 100, 5), self.order(2, 120, 3))
        self.assertEqual(self.store.state()['my_orders'], [])
        self.assertEqual(len(self.store.book('T7_RELIC', '1002', 'sell')['levels']), 2)

    def test_capture_debug_operations_match_start_script(self):
        from market import CAPTURE_DEBUG_OPERATIONS
        script = (ROOT/'start.ps1').read_text(encoding='utf-8-sig')
        self.assertIn(f"'-debug', '-operations', '{CAPTURE_DEBUG_OPERATIONS}'", script)

    def test_game_watch_heartbeat_marks_watcher_active(self):
        self.assertFalse(self.store.state()['game_watch']['active'])
        path = self.root/'data/game-watch-state.json'
        path.write_text(json.dumps({'checked_at': time.time(), 'game_running': False, 'capture_running': False, 'status': '게임 실행 대기'}), encoding='utf-8')
        watch = self.store.state()['game_watch']
        self.assertTrue(watch['active'])
        self.assertFalse(watch['game_running'])
        path.write_text(json.dumps({'checked_at': time.time() - 120, 'game_running': False}), encoding='utf-8')
        self.assertFalse(self.store.state()['game_watch']['active'])

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
            private = urllib.request.Request(url + "/private/europe/marketorders.ingest", data=json.dumps({"Orders": []}).encode())
            with urllib.request.urlopen(private) as r:
                self.assertTrue(json.load(r)["ignored"])
            self.assertEqual(self.store.mail_diagnostics()["private_topics"][0]["topic"], "marketorders.ingest")
            mine = urllib.request.Request(url + "/private/europe/marketorders.mine", data=json.dumps({"Kind": "sell", "Orders": [self.order(5, 130, 2)]}).encode())
            with urllib.request.urlopen(mine) as r:
                self.assertEqual(json.load(r)["accepted"], 1)
            self.assertEqual([r["order_id"] for r in self.store.state()["my_orders"]], ["5"])
            # The own-order topic is private only: the public path ignores it.
            public_mine = urllib.request.Request(url + "/ingest/europe/marketorders.mine", data=json.dumps({"Kind": "sell", "Orders": [self.order(6, 130, 2)]}).encode())
            with urllib.request.urlopen(public_mine) as r:
                self.assertTrue(json.load(r)["ignored"])
            with self.assertLogs(level="INFO") as logs:
                urllib.request.urlopen(url + "/api/health").close()
                with self.assertRaises(urllib.error.HTTPError) as result:
                    urllib.request.urlopen(url + "/missing")
                result.exception.close()
            self.assertFalse(any("/api/health" in line for line in logs.output))
            self.assertTrue(any("/missing" in line for line in logs.output))
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
