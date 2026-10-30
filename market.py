"""Local-only AODP raw-ingest receiver. No game input, no public-feed uploads."""
from __future__ import annotations

import argparse
import bisect
import csv
import hashlib
import io
import json
import logging
import math
import os
import re
import secrets
import smtplib
import sqlite3
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from statistics import median
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
UTC = timezone.utc
CITIES = {"1002": "Lymhurst", "2004": "Bridgewatch", "3005": "Caerleon", "3008": "Martlock", "4002": "Fort Sterling", "7": "Thetford", "5003": "Brecilien"}
# Portal markets are merged by the source project; keep these explicit and sourced.
# 1000-HellDen: its market listed the same order IDs as Lymhurst's, public buy orders and the
# player's own (live server, 2026-10-27), so it is Lymhurst's order book.
PORTALS = {"1301": "1002", "2301": "2004", "3301": "3008", "4301": "4002", "301": "7", "1000-HellDen": "1002"}
WEEKDAYS = ["월", "화", "수", "목", "금", "토", "일"]
DEPTH_DEFAULTS = {"small_quantity_max": 9, "large_quantity_min": 100, "wall_quantity_min": 500,
                  "wall_multiple": 3.0, "depth_price_band": 0.20}
# Silver one recommended trade may spend; keeps the top picks within a player's means.
# Caerleon and Brecilien are left out of trading by default: reaching them means crossing
# full-loot red zones or the Mists, and their price gaps are mostly that risk priced in,
# which would always top the rankings.
# inventory_heavy_days: once units held and on the way (open buy orders) reach this many days of
# the item's market-wide daily sales, picks that add stock buy less.
# cycle_share / weekday_cycles: the trading plan's part of a market's daily sales and weekday turns.
SETTING_DEFAULTS = {**DEPTH_DEFAULTS, "trade_budget": 10_000_000, "excluded_cities": ["3005", "5003"], "inventory_heavy_days": 3,
                    "cycle_share": 0.2, "weekday_cycles": 2, "buy_targets": {}}
# Plans that leave units to sell later; an instant round trip sells what it buys at once.
STOCKING_KINDS = ("instant_list", "best", "buyout")
BUILD = "20261030.2"
# Holdings rows for what the player carries (the inventory) use this key in place of a city.
CARRIED = "carried"
# Bank clusters seen joined to their city on 2026-10-27 (recordings and the zone log: 0007 <-> 0006,
# 4002 <-> 4001, 1002 -> 1001 with the Lymhurst bank's stock as the player stated it). A bank can
# also be entered from a travel hub (4000), which names no city; other banks are learned when
# entered from the city, or picked on the page.
BANK_CITIES = {"0006": "7", "4001": "4002", "1001": "1002"}
# Average purchase prices are shown for this many recent days next to all-time ones.
PURCHASE_WINDOW_DAYS = 30
# The same sell order and amount again within this many seconds is one instant buy heard twice.
INSTANT_BUY_REPEAT = 5
# An own sell order that sold nothing for STALE_DAYS gets wait-or-relist advice (the user's
# rule); waiting is advised when the queue ahead should clear within SOON_DAYS.
STALE_DAYS, SOON_DAYS = 3, 3
# Trading plan (the user's routine, 2026-10-27): on weekdays (BUY_DAYS) buy a little and sell it the
# same day, turning the silver over weekday_cycles times a day; in the weekend hot time (Friday
# evening to Sunday, HOT_DAYS long) sweep cheap listings and sell high. My part of a market is the
# cycle_share setting of what it sells a day.
BUY_DAYS, SELL_DAYS, HOT_DAYS = (0, 1, 2, 3), (4, 5, 6), 2.25
# Without a measured instant-buy share, half of a market's sales are taken as sell orders filling.
PLAN_INSTANT_SHARE = 0.5
# An hour of day is compared once it has this many days of hourly history; a gap under
# PORTFOLIO_HOUR_GAP from the day's average is called no clear pattern.
PORTFOLIO_HOUR_DAYS, PORTFOLIO_HOUR_GAP = 3, 0.03
# Trade scenarios. "바로" crosses an existing order now; "주문" waits in the queue.
MODE_LABELS = {"instant": "바로 매입 · 바로 판매", "instant_list": "바로 매입 · 주문 판매",
               "best": "주문 매입 · 주문 판매", "depth": "주문 매입 · 주문 판매 · 물량 기준 조건부"}
# Caveats true of every candidate; shown once instead of on every row.
COMMON_NOTES = ["주문 목록 일부 · 전체 깊이 미확인", "내 재고·기존 주문 미연결 · 수량 추천 보류",
                "바로 매입·판매 가격은 조회 뒤 다른 사람이 먼저 거래하면 바뀜", "주문 매입·판매는 체결 대기"]
FEES_UNCONFIRMED, TRANSPORT_UNCONFIRMED = "수수료 설정 미확인", "운송비 설정 미확인"
CANDIDATES_PER_ITEM = 15
# How much of an expected profit a plan keeps when ranked: an instant round trip settles now,
# every waiting order can be undercut or never fill.
# Which "지금 하세요" column a plan belongs to: settles at once, or waits for orders to fill.
INSTANT_KINDS = ("instant",)
WAITING_KINDS = ("instant_list", "best", "buyout")
CERTAINTY = {"instant": (1.0, "바로 체결 · 확실성 높음"), "instant_list": (0.7, "매도 주문 대기 · 확실성 보통"),
             "buyout": (0.5, "되팔기 대기 · 확실성 보통"), "best": (0.4, "매수·매도 주문 모두 대기 · 확실성 낮음")}
# Routes into these cities cross hostile zones; the cargo can be lost on the way.
DANGEROUS_ROUTES = {"3005": "Caerleon 이동은 레드존 통과 · 운반 중 약탈 위험", "5003": "Brecilien은 안개 지역 경유 · 운반 중 약탈 위험"}
BUYOUT_LEVELS = 30
BUYOUT_NOTES = ["본 매도 목록 범위 안에서만 계산", "조회 뒤 다른 사람이 먼저 사거나 더 싸게 올릴 수 있음",
                "다시 올릴 때 설정 수수료와 판매세 반영 · 판매를 보장하지 않음"]


def volume_risk(units, guide):
    """Reselling risk of holding `units`, judged by the market's slowest recent daily pace."""
    if guide["per_day"] is None:
        return "unknown"
    if units > guide["danger_above"]:
        return "danger"
    return "caution" if units > guide["caution_above"] else "ok"


def describe_depth(levels, side, cfg):
    """Outstanding quantity, never completed trade volume. Far-away spoof-like levels
    remain visible, but do not define the near-market reference price."""
    if not levels:
        return {"total_amount": 0, "small_levels": 0, "small_amount": 0, "small_level_fraction": 0,
                "leading_small_levels": 0, "leading_small_amount": 0, "reference": None,
                "large_levels": [], "distant_large_levels": [], "dominant": None, "small_dominated": False}
    small_max = cfg["small_quantity_max"]
    best = levels[0]["price"]
    band = cfg["depth_price_band"]
    for r in levels:
        r["small"] = r["amount"] <= small_max
        r["large"] = r["amount"] >= cfg["large_quantity_min"]
        r["outside_band"] = abs(r["price"] / best - 1) > band + 1e-12
    meaningful = [r for r in levels if not r["small"] and not r["outside_band"]]
    typical = median([r["amount"] for r in meaningful]) if meaningful else None
    for r in levels:
        r["wall"] = r["large"] and (r["amount"] >= cfg["wall_quantity_min"] or (typical is not None and r["amount"] >= typical * cfg["wall_multiple"]))
    large = [r for r in levels if r["large"] and not r["outside_band"]]
    leading = []
    for r in levels:
        if not r["small"]:
            break
        leading.append(r)
    small = [r for r in levels if r["small"]]
    reference = next(iter(large), None) or next(iter(meaningful), None)
    return {"total_amount": sum(r["amount"] for r in levels), "small_levels": len(small),
            "small_amount": sum(r["amount"] for r in small), "small_level_fraction": len(small) / len(levels),
            "leading_small_levels": len(leading), "leading_small_amount": sum(r["amount"] for r in leading),
            "reference": reference, "large_levels": sorted(large, key=lambda r: -r["amount"])[:5],
            "distant_large_levels": sorted([r for r in levels if r["large"] and r["outside_band"]], key=lambda r: -r["amount"])[:5],
            "dominant": max(meaningful, key=lambda r: r["amount"]) if meaningful else None,
            "small_dominated": len(small) / len(levels) >= 0.5}


def now_iso():
    return datetime.now(UTC).isoformat()


def parse_time(value):
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
        n = int(value)
        # C# ticks. Use integer subtraction to preserve precision.
        if n > 100_000_000_000_000:
            return datetime.fromtimestamp((n - 621355968000000000) / 10_000_000, UTC)
        return datetime.fromtimestamp(n, UTC)
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def location(value):
    value = str(value)
    if value.isdigit():
        value = str(int(value))
    return PORTALS.get(value, value)


def character_identity(character):
    """A character's stored identity: a hash, so raw character IDs stay out of the tables."""
    return hashlib.sha256(("Europe|" + character).encode()).hexdigest()


# The local fork of the capture client (README: 전용 수집기) runs instead of the official
# one when start.ps1 finds it. It reads mails and the in-game "my orders" lists itself.
CAPTURE_CLIENTS = ("vendor/albiondata-client-fork.exe", "vendor/albiondata-client.exe")


def capture_status(root):
    """Check only the owned capture PID; running is not proof of new market data."""
    path = Path(root)/'data/capture.pid'
    if not path.exists():
        return {'running': False, 'status': '수집기 시작 기록 없음'}
    try:
        pid = int(path.read_text(encoding='utf-8-sig').strip())
        if pid <= 0 or pid > 0xFFFFFFFF:
            raise ValueError('invalid PID')
    except (ValueError, OSError):
        return {'running': False, 'status': '수집기 실행 정보 없음'}
    if os.name != 'nt':
        return {'running': None, 'status': '이 기기에서 수집기 실행 확인 불가'}
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = (wintypes.DWORD,wintypes.BOOL,wintypes.DWORD)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.GetExitCodeProcess.argtypes = (wintypes.HANDLE,ctypes.POINTER(wintypes.DWORD))
    kernel.QueryFullProcessImageNameW.argtypes = (wintypes.HANDLE,wintypes.DWORD,wintypes.LPWSTR,ctypes.POINTER(wintypes.DWORD))
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        error = ctypes.get_last_error()
        return {'running': None if error == 5 else False, 'status': '수집기 실행 확인 불가' if error == 5 else '수집기 꺼짐'}
    try:
        code = wintypes.DWORD()
        if not kernel.GetExitCodeProcess(handle,ctypes.byref(code)):
            return {'running': None,'status':'수집기 실행 확인 불가'}
        image = ctypes.create_unicode_buffer(32768); size = wintypes.DWORD(len(image))
        if not kernel.QueryFullProcessImageNameW(handle,0,image,ctypes.byref(size)):
            return {'running': None,'status':'수집기 경로 확인 불가'}
        expected = {os.path.normcase(str((Path(root)/p).resolve())) for p in CAPTURE_CLIENTS}
        alive = code.value == 259 and os.path.normcase(image.value) in expected
        return {'running':alive,'status':'수집기 실행 중' if alive else '수집기 꺼짐'}
    finally:
        kernel.CloseHandle(handle)


ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
# The official capture client never says which orders are the player's own, and in 0.1.61 it
# drops opened sale mails when its mailbox list was never cached. Started with -debug and this
# operation list (start.ps1) it writes those game messages to its own log, which the receiver
# follows: 81/82 = market list requests, 94 = the in-game "my orders" tab (asked twice, once
# per sell / buy list), 174-176 = mailbox list, mail count and an opened mail. The live game
# never sent 92/93 (named GetMyOpenOffers/Requests in the client) for that tab (2026-10-26).
CAPTURE_DEBUG_OPERATIONS = "81,82,94,174,175,176"
LOG_LINE = re.compile(r"^[A-Z]{4}\[(?P<ts>[^\]]+)\]\s*(?P<msg>.*)$")
LOG_OPERATION = re.compile(r"Operation(?P<kind>Request|Response): \[(?P<op>\d+)\]")
MY_ORDER_OPS = {94}
MARKET_LIST_OPS = {81, 82}
RAW_LOG_OPS = {94, 174, 175, 176}
# One page of the "my orders" list; a full page may continue on the next one.
MY_ORDERS_PAGE = 50
# The fork's version carries this tag. It uploads the "my orders" lists on the private topic
# marketorders.mine itself, so its log must never turn a market page into own orders.
LOG_VERSION = re.compile(r"Starting Albion Data Client, version: (?P<version>\S+)")
FORK_VERSION_TAG = "-local"
# Mails other than a completed sale, as the fork names them. They are stored apart and never
# enter sale statistics: a bought unit is a cost, and an expiry mail is not a completed sale.
MAIL_KINDS = {"BuyNotification": "buy", "BuyExpiryNotification": "buy_expired", "ExpiryNotification": "sell_expired"}


def client_mail_log(root):
    """Mail-related lines from the capture client's own log (vendor/albiondata-client.log).
    It says whether the mailbox list was cached and whether uploads were refused."""
    for path in (Path(root)/"vendor/albiondata-client.log", Path(root)/"data/capture-stdout.log"):
        try:
            with path.open("rb") as handle:
                handle.seek(max(0, path.stat().st_size - 262_144))
                text = handle.read().decode("utf-8", "replace")
        except OSError:
            continue
        lines = [ANSI.sub("", line).strip() for line in text.splitlines()]
        return {"file": path.name, "lines": [l[:240] for l in lines if re.search(r"mail|bad response|notification", l, re.I)][-8:]}
    return {"file": None, "lines": []}


def watch_status(root):
    """Game watcher heartbeat (rewritten every 3 s). Missing or old means it is not running,
    so nothing will start capture when the game launches."""
    try:
        value = json.loads((Path(root)/'data/game-watch-state.json').read_text(encoding='utf-8'))
        age = time.time() - float(value['checked_at'])
    except (OSError, ValueError, KeyError, TypeError):
        return {'active': False, 'status': '자동 감시 기록 없음'}
    return {'active': abs(age) < 20, 'age_seconds': round(age), 'game_running': value.get('game_running'),
            'capture_running': value.get('capture_running'), 'status': value.get('status')}


class Store:
    def __init__(self, directory=ROOT, clock=None):
        self.root = Path(directory)
        self.cfg = json.loads((self.root / "config.json").read_text(encoding="utf-8-sig"))
        for key, value in SETTING_DEFAULTS.items():
            self.cfg.setdefault(key, value)
        # Trial items (테스트·보류): collected and shown, but kept out of the picks and the plan.
        self.cfg["trial_items"] = [i for i in self.cfg.get("trial_items", []) if i in self.cfg["items"]]
        # Per item, excluded cities still traded for that item (e.g. Brecilien for siphoned energy).
        extra = self.cfg.get("item_cities", {})
        self.cfg["item_cities"] = {i: [c for c in cs if c in CITIES] for i, cs in (extra if isinstance(extra, dict) else {}).items()
                                   if i in self.cfg["items"] and isinstance(cs, list)}
        ZoneInfo(self.cfg["timezone"])
        self.clock = clock or (lambda: datetime.now(UTC))
        self.lock = threading.RLock()
        (self.root / "data").mkdir(exist_ok=True)
        self.db = sqlite3.connect(self.root / "data" / "market.sqlite3", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
        PRAGMA journal_mode=WAL;
        PRAGMA foreign_keys=ON;
        CREATE TABLE IF NOT EXISTS packets(id INTEGER PRIMARY KEY, received TEXT NOT NULL, topic TEXT NOT NULL,
          sha256 TEXT NOT NULL, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS sessions(id INTEGER PRIMARY KEY, item TEXT NOT NULL, city TEXT NOT NULL,
          side TEXT NOT NULL, started TEXT NOT NULL, last_seen TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS orders(session INTEGER NOT NULL REFERENCES sessions(id), order_id TEXT NOT NULL,
          price REAL NOT NULL, amount INTEGER NOT NULL, seen TEXT NOT NULL, expires TEXT NOT NULL,
          PRIMARY KEY(session,order_id));
        CREATE TABLE IF NOT EXISTS history(item TEXT NOT NULL, city TEXT NOT NULL, hours INTEGER NOT NULL,
          stamp TEXT NOT NULL, amount INTEGER NOT NULL, silver REAL NOT NULL, seen TEXT NOT NULL,
          PRIMARY KEY(item,city,hours,stamp));
        CREATE TABLE IF NOT EXISTS mail_receipts(slot TEXT PRIMARY KEY, sent TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS personal_sales(identity TEXT NOT NULL, mail_id TEXT NOT NULL,
          item TEXT NOT NULL, city TEXT NOT NULL, price INTEGER NOT NULL, amount INTEGER NOT NULL,
          first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, source_expires TEXT,
          identity_known INTEGER NOT NULL, conflict INTEGER NOT NULL DEFAULT 0,
          PRIMARY KEY(identity,mail_id));
        CREATE TABLE IF NOT EXISTS personal_mails(identity TEXT NOT NULL, mail_id TEXT NOT NULL, kind TEXT NOT NULL,
          item TEXT NOT NULL, city TEXT NOT NULL, price INTEGER, amount INTEGER NOT NULL, order_amount INTEGER NOT NULL,
          silver INTEGER NOT NULL, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
          identity_known INTEGER NOT NULL, conflict INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(identity,mail_id));
        CREATE INDEX IF NOT EXISTS session_lookup ON sessions(item,city,side,id);
        CREATE INDEX IF NOT EXISTS history_lookup ON history(item,city,stamp);
        CREATE INDEX IF NOT EXISTS packet_topic_received ON packets(topic,received);
        CREATE INDEX IF NOT EXISTS order_lookup ON orders(order_id,seen);
        CREATE TABLE IF NOT EXISTS my_orders(id INTEGER PRIMARY KEY, item TEXT NOT NULL, city TEXT NOT NULL, side TEXT NOT NULL,
          price REAL NOT NULL, amount INTEGER NOT NULL, created TEXT NOT NULL, order_id TEXT, closed TEXT,
          remaining INTEGER, checked TEXT, source TEXT NOT NULL DEFAULT 'manual');
        CREATE TABLE IF NOT EXISTS client_lines(id INTEGER PRIMARY KEY, logged TEXT NOT NULL, op INTEGER, text TEXT NOT NULL,
          UNIQUE(logged,text));
        CREATE TABLE IF NOT EXISTS order_fills(item TEXT NOT NULL, city TEXT NOT NULL, side TEXT NOT NULL, order_id TEXT NOT NULL,
          price REAL NOT NULL, amount INTEGER NOT NULL, before TEXT NOT NULL, after TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS mail_events(id INTEGER PRIMARY KEY, received TEXT NOT NULL, kind TEXT,
          item TEXT, location TEXT, outcome TEXT NOT NULL, detail TEXT);
        CREATE TABLE IF NOT EXISTS private_topics(topic TEXT PRIMARY KEY, count INTEGER NOT NULL, last TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS book_stats(item TEXT NOT NULL, city TEXT NOT NULL, side TEXT NOT NULL, bucket TEXT NOT NULL,
          best REAL NOT NULL, amount INTEGER NOT NULL, levels INTEGER NOT NULL, PRIMARY KEY(item,city,side,bucket));
        CREATE TABLE IF NOT EXISTS pick_log(id INTEGER PRIMARY KEY, created TEXT NOT NULL, digest TEXT NOT NULL, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS observed_pages(session INTEGER NOT NULL REFERENCES sessions(id),
          ids TEXT NOT NULL, seen TEXT NOT NULL, PRIMARY KEY(session,ids));
        CREATE TABLE IF NOT EXISTS purchases(id INTEGER PRIMARY KEY, item TEXT NOT NULL, city TEXT NOT NULL, kind TEXT NOT NULL,
          price REAL NOT NULL, amount INTEGER NOT NULL, bought TEXT NOT NULL, created TEXT NOT NULL, removed TEXT);
        CREATE TABLE IF NOT EXISTS bank_stock(item TEXT NOT NULL, city TEXT NOT NULL, amount INTEGER NOT NULL, updated TEXT NOT NULL,
          PRIMARY KEY(item,city));
        CREATE TABLE IF NOT EXISTS finished_lists(location TEXT PRIMARY KEY, city TEXT NOT NULL, received TEXT NOT NULL, entries TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS last_results(kind TEXT PRIMARY KEY, created TEXT NOT NULL, seen TEXT NOT NULL, digest TEXT NOT NULL, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS bank_auto(location TEXT PRIMARY KEY, city TEXT, items TEXT NOT NULL, seen TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS bank_cities(location TEXT PRIMARY KEY, city TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS carried(id INTEGER PRIMARY KEY CHECK(id=1), items TEXT NOT NULL, seen TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS capital_log(at TEXT PRIMARY KEY, total REAL NOT NULL, holdings REAL NOT NULL, orders REAL NOT NULL,
          unpriced INTEGER NOT NULL, units TEXT NOT NULL);
        """)
        # mailed: when the game sent the mail (the fork reads it from the mailbox list).
        # moved: when an own order's units left last went down in the "my orders" list.
        for table, name, kind in (("my_orders", "remaining", "INTEGER"), ("my_orders", "checked", "TEXT"),
                                  ("my_orders", "source", "TEXT NOT NULL DEFAULT 'manual'"), ("my_orders", "moved", "TEXT"),
                                  ("personal_sales", "mailed", "TEXT"), ("personal_mails", "mailed", "TEXT"),
                                  ("finished_lists", "identity", "TEXT"), ("purchases", "auction", "TEXT")):
            if name not in {r[1] for r in self.db.execute(f"PRAGMA table_info({table})")}:
                self.db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {kind}")
        self.db.commit()
        self.log_state, self.pending_mine, self.log_debug_seen, self.log_fork = {"ident": None, "offset": 0}, [], False, False
        last = self.db.execute("SELECT payload FROM packets WHERE topic='marketorders.ingest' ORDER BY id DESC LIMIT 1").fetchone()
        orders = json.loads(last[0]).get("Orders", []) if last else []
        self.active_market = (orders[0]["ItemTypeId"], location(orders[0]["LocationId"])) if orders else None

    def save_config(self, values):
        with self.lock:
            cfg = dict(self.cfg)
            for k in ("timezone", "sales_tax", "setup_fee", "fees_confirmed", "minimum_margin", "near_high_fraction", "fresh_minutes", "transport_per_unit", "transport_confirmed", *SETTING_DEFAULTS):
                if k in values:
                    cfg[k] = values[k]
            ZoneInfo(cfg["timezone"])
            for key in ("sales_tax", "setup_fee", "minimum_margin"):
                cfg[key] = float(cfg[key])
                if not 0 <= cfg[key] < 1:
                    raise ValueError(key + " must be between 0 and 1")
            cfg["near_high_fraction"] = float(cfg["near_high_fraction"])
            cfg["fresh_minutes"] = int(cfg["fresh_minutes"])
            cfg["transport_per_unit"] = float(cfg["transport_per_unit"])
            if not 0 < cfg["near_high_fraction"] <= 1 or not 1 <= cfg["fresh_minutes"] <= 60 or cfg["transport_per_unit"] < 0:
                raise ValueError("Invalid freshness / high-price threshold / transport cost")
            for key in ("fees_confirmed", "transport_confirmed"):
                if not isinstance(cfg[key], bool):
                    raise ValueError(key + " must be boolean")
            for key in ("small_quantity_max", "large_quantity_min", "wall_quantity_min"):
                value = float(cfg[key])
                if not math.isfinite(value) or not value.is_integer() or value < 1 or value > 10**9:
                    raise ValueError(key + " must be a positive integer")
                cfg[key] = int(value)
            excluded = cfg["excluded_cities"]
            if not isinstance(excluded, list) or any(c not in CITIES for c in excluded) or len(set(excluded)) >= len(CITIES):
                raise ValueError("excluded_cities must list known cities and leave at least one")
            cfg["excluded_cities"] = sorted(set(excluded), key=list(CITIES).index)
            budget = float(cfg["trade_budget"])
            if not math.isfinite(budget) or not budget.is_integer() or not 1 <= budget <= 10**12:
                raise ValueError("trade_budget must be a positive integer")
            cfg["trade_budget"] = int(budget)
            cfg["inventory_heavy_days"] = float(cfg["inventory_heavy_days"])
            if not math.isfinite(cfg["inventory_heavy_days"]) or not 0.5 <= cfg["inventory_heavy_days"] <= 60:
                raise ValueError("inventory_heavy_days must be between 0.5 and 60")
            cfg["cycle_share"] = float(cfg["cycle_share"])
            if not math.isfinite(cfg["cycle_share"]) or not 0 < cfg["cycle_share"] <= 1:
                raise ValueError("cycle_share must be above 0 and at most 1")
            turns = float(cfg["weekday_cycles"])
            if not math.isfinite(turns) or not turns.is_integer() or not 1 <= turns <= 12:
                raise ValueError("weekday_cycles must be a whole number from 1 to 12")
            cfg["weekday_cycles"] = int(turns)
            targets = cfg["buy_targets"]
            if not isinstance(targets, dict) or any(item not in cfg["items"] for item in targets):
                raise ValueError("buy_targets must map tracked items to prices")
            cfg["buy_targets"] = {}
            for item, price in targets.items():
                price = float(price) if not isinstance(price, bool) else math.nan
                if not math.isfinite(price) or not price.is_integer() or not 1 <= price <= 10**9:
                    raise ValueError("buy_targets prices must be whole numbers from 1")
                cfg["buy_targets"][item] = int(price)
            for key in ("wall_multiple", "depth_price_band"):
                cfg[key] = float(cfg[key])
            if cfg["large_quantity_min"] <= cfg["small_quantity_max"] or cfg["wall_quantity_min"] < cfg["large_quantity_min"]:
                raise ValueError("소량 < 대량 <= 물량 벽 기준으로 입력해 주세요")
            if not 1 <= cfg["wall_multiple"] <= 1000 or not 0 < cfg["depth_price_band"] < 1:
                raise ValueError("Invalid wall multiple / price band")
            if any(not math.isfinite(cfg[k]) for k in ("sales_tax", "setup_fee", "minimum_margin", "near_high_fraction", "transport_per_unit", "wall_multiple", "depth_price_band")):
                raise ValueError("유한한 숫자만 입력할 수 있습니다")
            if cfg["sales_tax"] + cfg["setup_fee"] >= 1:
                raise ValueError("판매세와 설정 수수료 합계는 100% 미만이어야 합니다")
            target = self.root / "config.json"
            tmp = target.with_suffix(".tmp")
            tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(target)
            self.cfg = cfg

    def normalize_orders(self, rows):
        """Tracked items, quality 1 without enchantment, known cities: (row, city, price, amount, expiry)."""
        if not isinstance(rows, list):
            raise ValueError("Orders array required")
        normalized = []
        for r in rows:
            if not isinstance(r, dict) or r.get("ItemTypeId") not in self.cfg["items"]:
                continue
            if int(r.get("QualityLevel", 1)) != 1 or int(r.get("EnchantmentLevel", 0)) != 0:
                continue
            city = location(r.get("LocationId", ""))
            if city not in CITIES or r.get("AuctionType") not in ("offer", "request"):
                continue
            price, amount = int(r["UnitPriceSilver"]) / 10_000, int(r["Amount"])
            if price <= 0 or amount < 0 or not str(r.get("Id", "")):
                raise ValueError("Invalid order price / quantity / id")
            expiry = parse_time(r["Expires"]).isoformat()
            normalized.append((r, city, price, amount, expiry))
        return normalized

    def ingest(self, topic, payload):
        if topic == "marketnotifications":
            return self.ingest_sale(payload)
        if topic not in ("marketorders.ingest", "markethistories.ingest"):
            # Gold and other topics from the client are deliberately not retained.
            return {"accepted": 0, "ignored": True}
        if not isinstance(payload, dict):
            raise ValueError("JSON object required")
        received = self.clock().isoformat()
        with self.lock, self.db:
            if topic == "marketorders.ingest":
                normalized = self.normalize_orders(payload.get("Orders"))
                # An answer to the in-game "my orders" list is the player's own orders, not a
                # market page: it updates my_orders and never touches the order book.
                mine = self.claim_my_order_list()
                if mine:
                    self.sync_my_orders(normalized, received)
                    topic = "marketorders.mine"
                sessions, groups = {}, {}
                for row in ([] if mine else normalized):
                    r, city, price, amount, expiry = row
                    side = "sell" if r["AuctionType"] == "offer" else "buy"
                    key = (r["ItemTypeId"], city, side)
                    groups.setdefault(key, []).append(row)
                for key, group in groups.items():
                    market = key[:2]
                    if market != self.active_market:
                        # Incoming game data, never browser filters, identifies a market switch.
                        for direction in ("sell", "buy"):
                            self.db.execute("INSERT INTO sessions(item,city,side,started,last_seen) VALUES(?,?,?,?,?)", (*market, direction, received, received))
                        self.active_market = market
                    if key not in sessions:
                        previous = self.db.execute("SELECT * FROM sessions WHERE item=? AND city=? AND side=? ORDER BY id DESC LIMIT 1", key).fetchone()
                        if previous is None or (self.clock() - parse_time(previous["last_seen"])).total_seconds() > self.cfg["page_gap_seconds"]:
                            sid = self.db.execute("INSERT INTO sessions(item,city,side,started,last_seen) VALUES(?,?,?,?,?)", (*key, received, received)).lastrowid
                        else:
                            sid = previous["id"]
                            self.db.execute("UPDATE sessions SET last_seen=? WHERE id=?", (received, sid))
                        sessions[key] = sid
                    ids = {str(row[0]["Id"]) for row in group}
                    # The client omits page offset/query IDs. Replace a previously
                    # observed page only with strong shared-ID evidence; a single
                    # selected-order response is never evidence of deletions.
                    if len(ids) >= 2:
                        if not self.db.execute("SELECT 1 FROM observed_pages WHERE session=? LIMIT 1", (sid,)).fetchone():
                            # Older app versions retained original packets but no page
                            # index. Seed evidence for this existing scan on first update.
                            start = self.db.execute("SELECT started FROM sessions WHERE id=?", (sid,)).fetchone()[0]
                            for packet in self.db.execute("SELECT received,payload FROM packets WHERE topic='marketorders.ingest' AND received>=? ORDER BY id DESC LIMIT 100", (start,)).fetchall():
                                old_rows = json.loads(packet["payload"]).get("Orders", [])
                                old_ids = {str(o['Id']) for o in old_rows if o.get('ItemTypeId') == key[0] and location(o.get('LocationId','')) == key[1] and ('sell' if o.get('AuctionType') == 'offer' else 'buy') == key[2]}
                                if len(old_ids) >= 2:
                                    self.db.execute("INSERT OR IGNORE INTO observed_pages VALUES(?,?,?)", (sid, json.dumps(sorted(old_ids), separators=(",", ":")), packet["received"]))
                        matches = []
                        for page in self.db.execute("SELECT ids,seen FROM observed_pages WHERE session=?", (sid,)).fetchall():
                            old = set(json.loads(page["ids"]))
                            shared = len(ids & old)
                            score = shared / max(len(ids), len(old))
                            if shared >= 2 and score >= .6:
                                matches.append((score, shared, page, old))
                        if matches:
                            _, _, page, old = max(matches, key=lambda x: (x[0], x[1], x[2]["seen"]))
                            for removed in old - ids:
                                self.db.execute("DELETE FROM orders WHERE session=? AND order_id=? AND seen<=?", (sid, removed, page["seen"]))
                            self.db.execute("DELETE FROM observed_pages WHERE session=? AND ids=?", (sid, page["ids"]))
                        encoded_ids = json.dumps(sorted(ids), separators=(",", ":"))
                        self.db.execute("INSERT INTO observed_pages VALUES(?,?,?) ON CONFLICT(session,ids) DO UPDATE SET seen=excluded.seen", (sid, encoded_ids, received))
                    for r, city, price, amount, expiry in group:
                        # A known order showing fewer units than last time was partly filled:
                        # direct evidence of who traded (sell order shrank = someone bought instantly).
                        # A changed price is the owner editing the order, not a trade.
                        prev = self.db.execute("SELECT amount,seen,price FROM orders WHERE order_id=? ORDER BY seen DESC LIMIT 1", (str(r["Id"]),)).fetchone()
                        if prev and amount < prev["amount"] and prev["price"] == price:
                            self.db.execute("INSERT INTO order_fills VALUES(?,?,?,?,?,?,?,?)", (*key, str(r["Id"]), price, prev["amount"] - amount, prev["seen"], received))
                        self.db.execute("INSERT INTO orders VALUES(?,?,?,?,?,?) ON CONFLICT(session,order_id) DO UPDATE SET price=excluded.price,amount=excluded.amount,seen=excluded.seen,expires=excluded.expires", (sid, str(r["Id"]), price, amount, received, expiry))
                    if len(group) >= 2:
                        self.record_book_stats(key, sid, received)
                self.link_my_orders()
                kept = {"Orders": [r[0] for r in normalized]}
                count = len(normalized)
            else:
                # Numeric IDs change with patches: refresh via setup.py; retain unknown mapping error.
                item = self.cfg["item_numeric_ids"].get(str(payload.get("AlbionId")))
                if item is None:
                    return {"accepted": 0, "ignored": True, "reason": "untracked numeric item id"}
                city = location(payload.get("LocationId", ""))
                if city not in CITIES or int(payload.get("QualityLevel", 1)) != 1:
                    return {"accepted": 0, "ignored": True}
                scale = int(payload["Timescale"])
                if scale not in (0, 1, 2):
                    raise ValueError("Unknown Timescale")
                hours = 1 if scale == 0 else 6
                histories = payload.get("Histories", payload.get("MarketHistories"))
                if not isinstance(histories, list):
                    raise ValueError("Histories array required")
                for r in histories:
                    stamp = parse_time(r["Timestamp"])
                    amount, silver = int(r["ItemAmount"]), int(r["SilverAmount"]) / 10_000
                    if amount < 0 or silver < 0 or stamp > self.clock() + timedelta(hours=6):
                        raise ValueError("Invalid history amount / timestamp")
                    if (amount == 0 and silver != 0) or amount > 10**12:
                        raise ValueError("Invalid history quantity")
                    self.db.execute("INSERT INTO history VALUES(?,?,?,?,?,?,?) ON CONFLICT(item,city,hours,stamp) DO UPDATE SET amount=excluded.amount,silver=excluded.silver,seen=excluded.seen", (item, city, hours, stamp.isoformat(), amount, silver, received))
                kept, count = payload, len(histories)
            if count:
                encoded = json.dumps(kept, ensure_ascii=False, separators=(",", ":"))
                self.db.execute("INSERT INTO packets(received,topic,sha256,payload) VALUES(?,?,?,?)", (received, topic, hashlib.sha256(encoded.encode()).hexdigest(), encoded))
        return {"accepted": count, "received_at": received}

    def record_book_stats(self, key, sid, received):
        """One row per market side per 10 minutes: best price, visible quantity and price
        levels of the current scan. These outlive the 15-minute freshness window and feed
        the weekday/hour averages. Called only for list pages, never a selected single order."""
        rows = self.db.execute("SELECT price,amount FROM orders WHERE session=? AND amount>0", (sid,)).fetchall()
        moment = parse_time(received)
        bucket = moment.replace(minute=moment.minute - moment.minute % 10, second=0, microsecond=0).isoformat()
        best = (min if key[2] == "sell" else max)(r["price"] for r in rows)
        self.db.execute("INSERT OR REPLACE INTO book_stats VALUES(?,?,?,?,?,?,?)",
                        (*key, bucket, best, sum(r["amount"] for r in rows), len({r["price"] for r in rows})))

    def market_study(self, item, city, cache):
        """Inputs for choosing how to trade a market (weekday accumulate and weekend sell, or
        steady small trades): weekday vs weekend price and pace from 28 days of history; the
        share of sales that were instant buys, placing each history bucket's average price
        between that bucket's recorded best bid and ask; partial fills seen on re-scans; and
        how often the lowest ask was undercut while the market was being watched."""
        cfg = self.cfg
        zone = ZoneInfo(cfg["timezone"])
        now = self.clock()
        since = (now - timedelta(days=28)).isoformat()
        if ("patterns", item, city) not in cache:
            cache["patterns", item, city] = self.patterns(item, city)
        split = {}
        for r in cache["patterns", item, city]["weekdays"]:
            if r["observed_hours"]:
                g = split.setdefault("weekend" if r["key"] >= 5 else "weekday", [0, 0, 0.0])
                g[0] += r["total"]; g[1] += r["observed_hours"]; g[2] += r["silver"]
        days = {k: {"per_day": t / h * 24, "price": silver / t if t else None, "observed_hours": h} for k, (t, h, silver) in split.items()}
        book = {"sell": ([], []), "buy": ([], [])}
        by_part = {}
        for r in self.db.execute("SELECT side,bucket,best FROM book_stats WHERE item=? AND city=? AND bucket>? ORDER BY bucket", (item, city, since)):
            moment = parse_time(r["bucket"])
            book[r["side"]][0].append(moment); book[r["side"]][1].append(r["best"])
            by_part.setdefault((r["side"], "weekend" if moment.astimezone(zone).weekday() >= 5 else "weekday"), []).append(r["best"])
        def mean_in(side, a, b):
            times, values = book[side]
            chunk = values[bisect.bisect_left(times, a):bisect.bisect_left(times, b)]
            return sum(chunk) / len(chunk) if chunk else None
        weighted = weight = matched = 0
        for r in self.db.execute("SELECT hours,stamp,amount,silver FROM history WHERE item=? AND city=? AND stamp>? AND amount>0", (item, city, since)):
            end = parse_time(r["stamp"]); start = end - timedelta(hours=r["hours"])
            ask, bid = mean_in("sell", start, end), mean_in("buy", start, end)
            if ask is None or bid is None or ask <= bid:
                continue
            share = min(1, max(0, (r["silver"] / r["amount"] - bid) / (ask - bid)))
            weighted += share * r["amount"]; weight += r["amount"]; matched += 1
        observed = cuts = 0
        times, values = book["sell"]
        for t0, b0, t1, b1 in zip(times, values, times[1:], values[1:]):
            gap = (t1 - t0).total_seconds() / 3600
            if gap <= 0.5:
                observed += gap; cuts += b1 < b0
        fills = dict(self.db.execute("SELECT side,SUM(amount) FROM order_fills WHERE item=? AND city=? AND after>? GROUP BY side", (item, city, since)).fetchall())
        def avg(values):
            return sum(values) / len(values) if values else None
        # Strategy A, roughly: buy order just above the usual weekday bid, sell order just under the
        # usual weekend ask. Falls back to average traded prices when no quotes were recorded.
        bid_weekday, ask_weekend = avg(by_part.get(("buy", "weekday"), [])), avg(by_part.get(("sell", "weekend"), []))
        if bid_weekday is not None and ask_weekend is not None:
            buy, sell, basis = bid_weekday + 1, ask_weekend - 1, "호가 기록"
        elif "weekday" in days and "weekend" in days and days["weekday"]["price"] and days["weekend"]["price"]:
            buy, sell, basis = days["weekday"]["price"], days["weekend"]["price"], "평균 체결가"
        else:
            buy = sell = basis = None
        plan_a = None
        if buy:
            cost = buy * (1 + cfg["setup_fee"])
            margin = sell * (1 - cfg["sales_tax"] - cfg["setup_fee"]) - cost
            plan_a = {"buy": buy, "sell": sell, "margin": margin, "roi": margin / cost, "basis": basis}
        price_gap = (days["weekend"]["price"] / days["weekday"]["price"] - 1
                     if days.get("weekday", {}).get("price") and days.get("weekend", {}).get("price") else None)
        return {"item": item, "city": city, "weekday": days.get("weekday"), "weekend": days.get("weekend"), "price_gap": price_gap,
                "instant_buy_share": weighted / weight if matched >= 3 else None, "share_samples": matched,
                "fills_sell": fills.get("sell", 0), "fills_buy": fills.get("buy", 0),
                "undercut_hours": observed / cuts if cuts else None, "watched_hours": observed, "undercuts": cuts, "plan_a": plan_a}

    def market_studies(self, cache):
        # Heavy per market and slow-moving; rebuilt once a minute or when new data arrives.
        last = self.db.execute("SELECT MAX(id) FROM packets").fetchone()[0]
        key = (last, self.clock().replace(second=0, microsecond=0), json.dumps(self.cfg["excluded_cities"]), self.cfg["sales_tax"], self.cfg["setup_fee"])
        if getattr(self, "_study_key", None) != key:
            self._studies = [self.market_study(i, c, cache) for i in self.cfg["items"] for c in self.trade_cities(i)]
            self._study_key = key
        return self._studies

    def plan_quote(self, item, city, side, keys, days, books, since):
        """Typical best bid (side buy) or ask (side sell) in a market on those weekdays (keys):
        the recorded bests on those days, else the recorded bests of any day, else the latest
        book, each scaled by the city's average traded price on those days over all days; else
        that average traded price."""
        zone = ZoneInfo(self.cfg["timezone"])
        rows = [(parse_time(r["bucket"]).astimezone(zone).weekday(), r["best"]) for r in self.db.execute(
            "SELECT bucket,best FROM book_stats WHERE item=? AND city=? AND side=? AND bucket>?", (item, city, side, since))]
        on_days = [best for day, best in rows if day in keys]
        if len(on_days) >= 3:
            return sum(on_days) / len(on_days), "호가 기록"
        group = days["weekday" if keys == BUY_DAYS else "weekend"]
        if not group:
            return None, None
        scale = group["price"] / days["all"]["price"]
        if len(rows) >= 3:
            return sum(best for _, best in rows) / len(rows) * scale, "호가 기록 · 요일 보정"
        book = books[item][city][side]
        if book["levels"] and not book["single_only"]:
            return book["levels"][0]["price"] * scale, "마지막 호가 · 요일 보정"
        return group["price"], "평균 체결가"

    def portfolio_hours(self, cache, item, city, cheap):
        """Hours of day (configured time zone) to buy (cheap) or sell in a market, from hourly
        history: the three hours with the lowest (highest) average traded price among hours seen
        on PORTFOLIO_HOUR_DAYS days or more; selling only counts hours at least as busy as the
        median hour. Each hour's price is against the average over all hours."""
        p = cache.setdefault(("patterns", item, city), self.patterns(item, city))
        rows = [r for r in p["hourly"] if r["total"]]
        days = median([r["observed_hours"] for r in rows]) if rows else 0
        seen = [r for r in rows if r["observed_hours"] >= PORTFOLIO_HOUR_DAYS]
        if len(seen) < 12:
            return {"hours": [], "days": days, "enough": False, "clear": False}
        average = sum(r["silver"] for r in rows) / sum(r["total"] for r in rows)
        if not cheap:
            busy = median([r["per_observed_hour"] for r in seen])
            seen = [r for r in seen if r["per_observed_hour"] >= busy]
        best = sorted(seen, key=lambda r: r["average_price"], reverse=not cheap)[:3]
        hours = [{"hour": r["key"], "vs_average": r["average_price"] / average - 1, "per_hour": r["per_observed_hour"]} for r in best]
        gap = sum(h["vs_average"] for h in hours) / len(hours)
        return {"hours": hours, "days": days, "enough": True, "clear": abs(gap) >= PORTFOLIO_HOUR_GAP}

    def plan_inputs(self, cache, books):
        """Per item, the weekday cycle and the weekend sweep of the trading plan, from the last 28
        days in the trading cities that sell at least a third of the busiest city's pace on those
        days. Weekday cycle, two ways: a buy order at the buy city's bid, or an instant buy at its
        ask, then sell orders at the asks of every city that makes money (the buy city too, for a
        buy order); each city takes the cycle_share setting of its sell-order sales (the
        instant-buy share of its sales), a buy order buys at most cycle_share of the rest of the
        buy city's. Weekend: an instant buy at a city's weekday ask, sold the same way at the
        weekend asks over HOT_DAYS. Each picks the buy city and way with the most profit. Prices
        from plan_quote; swing: spread of the main weekend sell city's daily average prices over
        their median."""
        cfg = self.cfg
        last = self.db.execute("SELECT MAX(id) FROM packets").fetchone()[0]
        key = (last, self.clock().replace(second=0, microsecond=0), json.dumps(cfg["excluded_cities"]),
               cfg["sales_tax"], cfg["setup_fee"], cfg["transport_per_unit"], cfg["cycle_share"])
        if getattr(self, "_plan_key", None) == key:
            return self._plan_inputs
        zone = ZoneInfo(cfg["timezone"])
        since = (self.clock() - timedelta(days=28)).isoformat()
        measured = {(s["item"], s["city"]): s["instant_buy_share"] for s in self.market_studies(cache)}
        keep = 1 - cfg["sales_tax"] - cfg["setup_fee"]
        out = {}
        for item in self.plan_items():
            days = {}
            for city in self.trade_cities(item):
                p = cache.setdefault(("patterns", item, city), self.patterns(item, city))
                g = {}
                for name, keys in (("weekday", BUY_DAYS), ("weekend", SELL_DAYS), ("all", range(7))):
                    seen = [r for r in p["weekdays"] if r["key"] in keys and r["observed_hours"] and r["total"]]
                    total, hours, silver = (sum(r[k] for r in seen) for k in ("total", "observed_hours", "silver"))
                    g[name] = {"per_day": total / hours * 24, "price": silver / total} if total else None
                days[city] = g
            def busy(name):
                pool = {c: g[name]["per_day"] for c, g in days.items() if g[name]}
                return [c for c, v in pool.items() if v >= max(pool.values()) / 3] if pool else []
            weekday, weekend = busy("weekday"), busy("weekend")
            quote = lambda city, side, keys: self.plan_quote(item, city, side, keys, days[city], books, since)
            bid = {c: quote(c, "buy", BUY_DAYS) for c in weekday}
            ask = {c: quote(c, "sell", BUY_DAYS) for c in weekday}
            ask_weekend = {c: quote(c, "sell", SELL_DAYS) for c in weekend}
            share = {c: PLAN_INSTANT_SHARE if measured.get((item, c)) is None else measured[(item, c)] for c in days}
            def plan(method, buy_city, buy, fee_in, limit, outlets):
                """Units bought in one city, sold across the outlets (city, quote, units it takes)
                that make money, best margin first, until limit (None: no limit) runs out. Selling
                in one city only would pile the units up there."""
                base = buy[0] * (1 + fee_in)
                legs, left = [], limit
                for city, sell, room in outlets:
                    cost = base + (cfg["transport_per_unit"] if city != buy_city else 0)
                    legs.append({"sell_city": city, "sell": sell[0], "sell_basis": sell[1], "cost": cost, "net": sell[0] * keep,
                                 "margin": sell[0] * keep - cost, "units": room, "share_measured": measured.get((item, city)) is not None})
                legs = sorted((leg for leg in legs if leg["margin"] > 0), key=lambda leg: -leg["margin"])
                for leg in legs:
                    leg["units"] = leg["units"] if left is None else min(leg["units"], left)
                    left = None if left is None else left - leg["units"]
                legs = [leg for leg in legs if leg["units"] > 0]
                if not legs:
                    return None
                units, profit = sum(leg["units"] for leg in legs), sum(leg["margin"] * leg["units"] for leg in legs)
                cost = sum(leg["cost"] * leg["units"] for leg in legs) / units
                for leg in legs:
                    leg["profit"] = leg["margin"] * leg["units"]
                main = legs[0]
                return {"method": method, "buy_city": buy_city, "buy": buy[0], "buy_basis": buy[1], "legs": legs,
                        "sell_city": main["sell_city"], "sell": main["sell"], "sell_basis": main["sell_basis"],
                        "cost": cost, "margin": profit / units, "roi": profit / (cost * units), "units": units, "profit": profit,
                        "share_measured": main["share_measured"]}
            weekday_outlets = [(c, ask[c], cfg["cycle_share"] * share[c] * days[c]["weekday"]["per_day"]) for c in weekday if ask[c][0] is not None]
            weekend_outlets = [(c, ask_weekend[c], cfg["cycle_share"] * share[c] * days[c]["weekend"]["per_day"] * HOT_DAYS)
                               for c in weekend if ask_weekend[c][0] is not None]
            cycles, sweeps = [], []
            for b in weekday:
                if bid[b][0] is not None:
                    cycles.append(plan("order", b, bid[b], cfg["setup_fee"], cfg["cycle_share"] * (1 - share[b]) * days[b]["weekday"]["per_day"], weekday_outlets))
                if ask[b][0] is not None:
                    cycles.append(plan("instant", b, ask[b], 0, None, [o for o in weekday_outlets if o[0] != b]))
                    sweeps.append(plan("instant", b, ask[b], 0, None, weekend_outlets))
            best = lambda options: max((o for o in options if o), key=lambda o: o["profit"], default=None)
            cycle, sweep = best(cycles), best(sweeps)
            other = best([o for o in cycles if o and cycle and o["method"] != cycle["method"]])
            swing = None
            if sweep:
                daily = defaultdict(lambda: [0, 0])
                for r in self.history_rows(item, sweep["sell_city"], 28):
                    d = daily[parse_time(r["start"]).astimezone(zone).date()]
                    d[0] += r["amount"]; d[1] += r["silver"]
                prices = [silver / amount for amount, silver in daily.values() if amount]
                # Robust spread (1.4826 x median absolute deviation): a one-day spike or a bad
                # print does not make a steady market look jumpy.
                mid = median(prices) if len(prices) >= 7 else None
                swing = 1.4826 * median(abs(x - mid) for x in prices) / mid if mid else None
            out[item] = {"weekday": cycle, "weekday_other": other, "weekend": sweep, "swing": swing,
                         "buy_hours": self.portfolio_hours(cache, item, cycle["buy_city"], True) if cycle else None,
                         "sell_hours": self.portfolio_hours(cache, item, cycle["sell_city"], False) if cycle else None,
                         "weekend_hours": self.portfolio_hours(cache, item, sweep["sell_city"], False) if sweep else None}
        self._plan_key, self._plan_inputs = key, out
        return out

    def portfolio(self, cache, books, capital, purchases):
        """Silver per item for the trading plan, as shares of the invested silver (capital total).
        Weekday: one turn (units a day over the weekday_cycles setting, at cost); weekend: the
        units to sell in the hot time, at cost. The weekday and the weekend each fill items in
        order of profit per silver tied (a weekday turn earns its margin weekday_cycles times a
        day over the four days) until the total runs out; the rest stays as silver. Units held now
        go first: a turn buys only what they do not cover. Beside it, my sale mails of the last 28
        days against the average purchase cost."""
        cfg = self.cfg
        inputs, total, turns = self.plan_inputs(cache, books), capital["total"], cfg["weekday_cycles"]
        def fill(needs):
            left, given = total, {}
            for item, (silver, _) in sorted(needs.items(), key=lambda kv: -kv[1][1]):
                given[item] = min(silver, max(0.0, left))
                left -= given[item]
            return given
        weekday_need, weekend_need = {}, {}
        for item, x in inputs.items():
            if x["weekday"]:
                w = x["weekday"]
                silver = math.ceil(w["units"] / turns) * w["cost"]
                weekday_need[item] = (silver, w["profit"] * len(BUY_DAYS) / silver if silver else 0)
            if x["weekend"]:
                weekend_need[item] = (math.floor(x["weekend"]["units"]) * x["weekend"]["cost"], x["weekend"]["roi"])
        weekday_given, weekend_given = fill(weekday_need), fill(weekend_need)
        since = (self.clock() - timedelta(days=28)).isoformat()
        sold = {r["item"]: r for r in self.db.execute(
            "SELECT item,SUM(amount) AS units,SUM(price*amount) AS silver FROM personal_sales WHERE conflict=0 AND COALESCE(mailed,first_seen)>? GROUP BY item", (since,))}
        costs = {s["item"]: s["all"]["cost_average"] for s in purchases["summary"]}
        share = lambda silver: silver / total if total else None
        rows = []
        for item in self.plan_items():
            x, held = inputs[item], capital["items"].get(item)
            units_held = held["held"] if held else 0
            weekday = weekend = mine = None
            if x["weekday"]:
                w = x["weekday"]
                turn = math.ceil(w["units"] / turns)
                weekday = {**w, "turn_units": turn, "turn_silver": weekday_need[item][0], "turns": turns,
                           "profit_week": w["profit"] * len(BUY_DAYS), "silver": weekday_given[item], "share": share(weekday_given[item]),
                           "stock_turns": units_held / turn if turn else None, "to_buy": max(0, turn - units_held)}
            if x["weekend"]:
                units = math.floor(x["weekend"]["units"])
                weekend = {**x["weekend"], "units": units, "need": weekend_need[item][0], "silver": weekend_given[item], "share": share(weekend_given[item])}
            if item in sold and sold[item]["units"]:
                price, cost = sold[item]["silver"] / sold[item]["units"], costs.get(item)
                mine = {"units": sold[item]["units"], "price": price, "roi": price * (1 - cfg["sales_tax"] - cfg["setup_fee"]) / cost - 1 if cost else None}
            rows.append({"item": item, "weekday": weekday, "weekday_other": x["weekday_other"], "weekend": weekend, "held": units_held,
                         "current_silver": held["value"] + held["orders_silver"] if held else 0.0,
                         "swing": x["swing"], "buy_hours": x["buy_hours"], "sell_hours": x["sell_hours"], "weekend_hours": x["weekend_hours"], "mine": mine})
        weekday_silver, weekend_silver = sum(weekday_given.values()), sum(weekend_given.values())
        return {"rows": rows, "total": total, "turns": turns, "cycle_share": cfg["cycle_share"], "hot_days": HOT_DAYS,
                "weekday_need": sum(n for n, _ in weekday_need.values()), "weekday_silver": weekday_silver, "weekday_share": share(weekday_silver),
                "weekend_need": sum(n for n, _ in weekend_need.values()), "weekend_silver": weekend_silver, "weekend_share": share(weekend_silver),
                "cities": self.trade_cities()}

    def book_patterns(self, item, city):
        """Average best price and visible quantity per weekday and hour (configured time zone)
        over the last 28 days of order-book observations."""
        zone = ZoneInfo(self.cfg["timezone"])
        since = (self.clock() - timedelta(days=28)).isoformat()
        groups = {}
        for r in self.db.execute("SELECT side,bucket,best,amount FROM book_stats WHERE item=? AND city=? AND bucket>?", (item, city, since)):
            local = parse_time(r["bucket"]).astimezone(zone)
            for scale, key in (("weekdays", local.weekday()), ("hourly", local.hour)):
                g = groups.setdefault((r["side"], scale, key), [0, 0.0, 0])
                g[0] += 1; g[1] += r["best"]; g[2] += r["amount"]
        out = {}
        for side in ("sell", "buy"):
            out[side] = {scale: [{"key": k, "samples": groups[side, scale, k][0], "best": groups[side, scale, k][1] / groups[side, scale, k][0],
                                  "amount": groups[side, scale, k][2] / groups[side, scale, k][0]}
                                 for k in range(7 if scale == "weekdays" else 24) if (side, scale, k) in groups]
                         for scale in ("weekdays", "hourly")}
        return out

    def timing(self, cache, item, city, books):
        """How this moment compares with this market's usual pace: sales per hour at the current
        local hour and weekday against its 28-day average, and current best bid/ask against the
        usual price at this weekday. Factors are None until enough history exists."""
        key = ("timing", item, city)
        if key in cache:
            return cache[key]
        zone = ZoneInfo(self.cfg["timezone"])
        local = self.clock().astimezone(zone)
        p = cache.setdefault(("patterns", item, city), self.patterns(item, city))
        def factor(rows, k):
            seen = [r for r in rows if r["observed_hours"]]
            if len(seen) < 3:
                return None
            usual = sum(r["total"] for r in seen) / sum(r["observed_hours"] for r in seen)
            here = next((r for r in seen if r["key"] == k), None)
            return here["per_observed_hour"] / usual if here and usual else None
        bp = self.book_patterns(item, city)
        def versus(side):
            usual = next((r for r in bp[side]["weekdays"] if r["key"] == local.weekday()), None)
            book = books[item][city][side]
            if not usual or usual["samples"] < 2 or not book["fresh"]:
                return None
            return book["fresh_levels"][0]["price"] / usual["best"] - 1
        cache[key] = result = {"weekday": local.weekday(), "hour": local.hour, "local_time": local.isoformat(),
                               "sales_hour": factor(p["hourly"], local.hour), "sales_weekday": factor(p["weekdays"], local.weekday()),
                               "bid_vs_usual": versus("buy"), "ask_vs_usual": versus("sell")}
        return result

    def add_my_order(self, values):
        """Register one of the player's own orders. The capture client does not say who placed an
        order, so the player enters price and quantity; the next scan of that market links it to
        the matching order id, after which remaining units and fills are tracked automatically."""
        item, city, side = values.get("item"), values.get("city"), values.get("side")
        if item not in self.cfg["items"] or city not in CITIES or side not in ("sell", "buy"):
            raise ValueError("Unknown item / city / side")
        price, amount = float(values.get("price")), values.get("amount")
        if not math.isfinite(price) or price <= 0 or isinstance(amount, bool) or not isinstance(amount, int) or not 0 < amount <= 10**9:
            raise ValueError("Price must be positive and amount a positive integer")
        with self.lock, self.db:
            row = self.db.execute("INSERT INTO my_orders(item,city,side,price,amount,created) VALUES(?,?,?,?,?,?)",
                                  (item, city, side, price, amount, self.clock().isoformat())).lastrowid
            self.link_my_orders()
        return row

    def close_my_order(self, order):
        with self.lock, self.db:
            if not self.db.execute("UPDATE my_orders SET closed=? WHERE id=? AND closed IS NULL", (self.clock().isoformat(), int(order))).rowcount:
                raise ValueError("Unknown or already closed order")

    def add_purchase(self, values):
        """A purchase the capture client cannot see, entered by the player: an instant buy from a
        sell order (kind instant, no fee) or buy-order units whose mail was never opened (kind
        order, setup fee). bought is an ISO date or date-time, local to the settings' timezone
        when it names none; now by default."""
        item, city, kind = values.get("item"), values.get("city"), values.get("kind")
        if item not in self.cfg["items"] or city not in CITIES or kind not in ("instant", "order"):
            raise ValueError("Unknown item / city / kind")
        price, amount = float(values.get("price")), values.get("amount")
        if not math.isfinite(price) or price <= 0 or isinstance(amount, bool) or not isinstance(amount, int) or not 0 < amount <= 10**9:
            raise ValueError("Price must be positive and amount a positive integer")
        now = bought = self.clock()
        if values.get("bought"):
            bought = datetime.fromisoformat(str(values["bought"]))
            bought = (bought.replace(tzinfo=ZoneInfo(self.cfg["timezone"])) if bought.tzinfo is None else bought).astimezone(UTC)
            if bought > now + timedelta(minutes=5):
                raise ValueError("Purchase time is in the future")
        with self.lock, self.db:
            return self.db.execute("INSERT INTO purchases(item,city,kind,price,amount,bought,created) VALUES(?,?,?,?,?,?,?)",
                                   (item, city, kind, price, amount, bought.isoformat(), now.isoformat())).lastrowid

    def ingest_instant_buy(self, payload):
        """One instant buy from a market sell order (the fork's private topic
        marketorders.instantbuy, local.5): the units bought and the order they took, its price
        x10000 as the game sends it. A tracked item bought at a city market goes into purchases
        as kind instant with the order's id, so it can be removed like a hand-entered one."""
        if not isinstance(payload, dict):
            raise ValueError("JSON object required")
        item, city, auction = payload.get("ItemTypeId"), location(payload.get("LocationId", "")), str(payload.get("AuctionId") or "")
        amount, price = payload.get("Amount"), payload.get("UnitPriceSilver")
        if not auction or isinstance(amount, bool) or not isinstance(amount, int) or not 0 < amount <= 10**9:
            raise ValueError("AuctionId and a positive Amount required")
        if isinstance(price, bool) or not isinstance(price, int) or price <= 0:
            raise ValueError("UnitPriceSilver must be a positive integer")
        if item not in self.cfg["items"] or city not in CITIES or int(payload.get("QualityLevel") or 1) != 1 or int(payload.get("EnchantmentLevel") or 0):
            return {"accepted": 0, "ignored": True}
        now = self.clock()
        with self.lock, self.db:
            # The capture client can listen on more than one network device.
            for r in self.db.execute("SELECT created FROM purchases WHERE auction=? AND amount=? AND removed IS NULL", (auction, amount)):
                if abs((now - parse_time(r["created"])).total_seconds()) <= INSTANT_BUY_REPEAT:
                    return {"accepted": 0, "duplicate": True}
            added = self.db.execute("INSERT INTO purchases(item,city,kind,price,amount,bought,created,auction) VALUES(?,?,?,?,?,?,?,?)",
                                    (item, city, "instant", price / 10000, amount, now.isoformat(), now.isoformat(), auction)).lastrowid
        return {"accepted": 1, "id": added}

    def remove_purchase(self, purchase):
        with self.lock, self.db:
            if not self.db.execute("UPDATE purchases SET removed=? WHERE id=? AND removed IS NULL", (self.clock().isoformat(), int(purchase))).rowcount:
                raise ValueError("Unknown or already removed purchase")

    def purchases(self, open_orders):
        """What the player paid for tracked items: completed buy orders (the mail's total),
        expired buy orders (units bought x the order price), units already filled on buy orders
        still open (x the order price), and purchases entered by hand. Buy-order units carry the
        setup fee in cost; instant buys do not. An open order counts until a buy mail of the same
        market and price arrives after it was placed: that mail then covers the order (two open
        orders at one price would both be taken as covered)."""
        fee, rows = self.cfg["setup_fee"], []
        with self.lock:
            for r in self.db.execute("""SELECT kind,item,city,price,amount,silver,COALESCE(mailed,first_seen) AS at FROM personal_mails
                    WHERE conflict=0 AND kind IN ('buy','buy_expired')"""):
                if r["kind"] == "buy":
                    rows.append({"source": "mail", "kind": "order", "item": r["item"], "city": r["city"], "amount": r["amount"],
                                 "silver": r["silver"], "order_price": r["price"], "at": r["at"]})
                elif r["amount"] and r["price"]:
                    rows.append({"source": "expired_mail", "kind": "order", "item": r["item"], "city": r["city"], "amount": r["amount"],
                                 "silver": r["price"] * r["amount"], "order_price": r["price"], "at": r["at"]})
            for r in self.db.execute("SELECT id,item,city,kind,price,amount,bought,auction FROM purchases WHERE removed IS NULL"):
                rows.append({"source": "instant_buy" if r["auction"] else "manual", "id": r["id"], "kind": r["kind"], "item": r["item"], "city": r["city"], "amount": r["amount"],
                             "silver": r["price"] * r["amount"], "order_price": None, "at": r["bought"]})
        mails = [(r["item"], r["city"], r["order_price"], parse_time(r["at"])) for r in rows if r["source"] not in ("manual", "instant_buy")]
        for o in open_orders:
            if o["side"] != "buy" or o["filled"] <= 0:
                continue
            placed = parse_time(o["created"])
            if any((item, city, price) == (o["item"], o["city"], o["price"]) and at >= placed for item, city, price, at in mails):
                continue
            rows.append({"source": "open_order", "kind": "order", "item": o["item"], "city": o["city"], "amount": o["filled"],
                         "silver": o["price"] * o["filled"], "order_price": o["price"], "at": o["seen"] or o["created"]})
        for r in rows:
            r["unit"] = r["silver"] / r["amount"]
            r["cost"] = r["silver"] * (1 + fee) if r["kind"] == "order" else r["silver"]
        cutoff = self.clock() - timedelta(days=PURCHASE_WINDOW_DAYS)
        def total(chosen):
            units, silver, cost = sum(r["amount"] for r in chosen), sum(r["silver"] for r in chosen), sum(r["cost"] for r in chosen)
            return {"units": units, "silver": round(silver), "average": silver / units if units else None, "cost_average": cost / units if units else None}
        summary = [{"item": item, "recent": total([r for r in mine if parse_time(r["at"]) >= cutoff]), "all": total(mine)}
                   for item in self.cfg["items"] if (mine := [r for r in rows if r["item"] == item])]
        rows.sort(key=lambda r: parse_time(r["at"]), reverse=True)
        return {"summary": summary, "recent": rows[:100], "window_days": PURCHASE_WINDOW_DAYS, "setup_fee": fee}

    def stale_listings(self, cache, books, open_orders, purchases):
        """Own sell orders that sold nothing for STALE_DAYS, with three ways out priced against
        what the units cost (purchases): wait at the current price; cancel and relist one silver
        under the cheapest other seller, paying the setup fee again on the new price; or sell now
        into the best buy orders. Days to sell = units ahead of mine (plus mine) over the
        market's slowest daily pace (volume_guide) times its instant-buy share when known (only
        instant buys take sell orders). Idle time runs from the last time units left
        went down, else from when this program first saw the order (not when it was placed).
        Advice: cheapest (already first in line, demand is the limit), soon (my turn within
        SOON_DAYS), below_cost (relisting would sell under cost), no_pace (no sales history to
        tell), else relist."""
        now, tax, fee = self.clock(), self.cfg["sales_tax"], self.cfg["setup_fee"]
        costs = {s["item"]: s["recent"]["cost_average"] or s["all"]["cost_average"] for s in purchases["summary"]}
        out = []
        for o in open_orders:
            if o["side"] != "sell" or not o["order_id"] or not o["remaining"]:
                continue
            filled = self.db.execute("SELECT MAX(after) FROM order_fills WHERE order_id=?", (o["order_id"],)).fetchone()[0]
            last = max((parse_time(t) for t in (o["moved"], filled) if t), default=None)
            idle = (now - (last or parse_time(o["created"]))).total_seconds() / 86400
            if idle < STALE_DAYS:
                continue
            units, cost = o["remaining"], costs.get(o["item"])
            # Only instant buys take sell orders; the rest of the volume hit buy orders.
            if ("study", o["item"], o["city"]) not in cache:
                cache["study", o["item"], o["city"]] = self.market_study(o["item"], o["city"], cache)
            share = cache["study", o["item"], o["city"]]["instant_buy_share"]
            per_day = self.volume_guide(cache, o["item"], o["city"])["per_day"]
            per_day = per_day * share if per_day is not None and share is not None else per_day
            def option(price, net, sold, queue, extra=0):
                return {"price": price, "net": net, "units": sold, "days": queue / per_day if per_day and queue else 0 if not queue else None,
                        "fee": extra, "margin": None if cost is None else net - cost}
            wait = option(o["price"], o["price"] * (1 - tax), units, o["ahead"] + units)
            relist = None
            if o["ahead"] and o["best"] is not None and o["best"] > 1:
                new = math.ceil(o["best"]) - 1
                relist = option(new, new * (1 - tax - fee), units, units, new * fee * units)
            sold = proceeds = 0
            for level in books[o["item"]][o["city"]]["buy"]["levels"]:
                take = min(units - sold, level["amount"])
                sold, proceeds = sold + take, proceeds + take * level["price"]
                if sold == units:
                    break
            instant = option(proceeds / sold, proceeds / sold * (1 - tax), sold, 0) if sold else None
            if not o["ahead"]:
                advice = "cheapest"
            elif wait["days"] is not None and wait["days"] <= SOON_DAYS:
                advice = "soon"
            elif relist is None or (cost is not None and relist["net"] < cost):
                advice = "below_cost"
            elif per_day is None:
                advice = "no_pace"
            else:
                advice = "relist"
            out.append({"id": o["id"], "item": o["item"], "city": o["city"], "price": o["price"], "remaining": units,
                        "idle_days": idle, "since": last.isoformat() if last else o["created"], "sold_before": last is not None,
                        "ahead": o["ahead"], "best": o["best"], "book_seen": books[o["item"]][o["city"]]["sell"]["seen"],
                        "per_day": per_day, "instant_buy_share": share, "cost": cost,
                        "wait": wait, "relist": relist, "instant": instant, "advice": advice})
        return sorted(out, key=lambda r: -r["idle_days"])

    def link_my_orders(self):
        """Match unlinked own orders to observed orders of the same market, side and price: an
        exact quantity first, else one with fewer units left (already partly sold). An order id
        is never linked twice."""
        linked = {r[0] for r in self.db.execute("SELECT order_id FROM my_orders WHERE order_id IS NOT NULL")}
        for mine in self.db.execute("SELECT * FROM my_orders WHERE order_id IS NULL AND closed IS NULL ORDER BY id").fetchall():
            found = self.db.execute("""SELECT o.order_id, o.amount FROM orders o JOIN sessions s ON o.session=s.id
                WHERE s.item=? AND s.city=? AND s.side=? AND o.price=? AND o.amount<=? AND o.amount>0
                ORDER BY o.amount=? DESC, o.seen DESC""", (mine["item"], mine["city"], mine["side"], mine["price"], mine["amount"], mine["amount"])).fetchall()
            match = next((r["order_id"] for r in found if r["order_id"] not in linked), None)
            if match:
                self.db.execute("UPDATE my_orders SET order_id=? WHERE id=?", (match, mine["id"]))
                linked.add(match)

    def read_client_log(self):
        """Follow the capture client's own log, which the client rewrites each time it starts."""
        path = self.root / "vendor/albiondata-client.log"
        try:
            stat = path.stat()
        except OSError:
            return
        ident = (stat.st_dev, stat.st_ino)
        if ident != self.log_state["ident"] or stat.st_size < self.log_state["offset"]:
            self.log_state = {"ident": ident, "offset": max(0, stat.st_size - 8_000_000)}
            self.pending_mine, self.log_fork = [], False
        start = self.log_state["offset"]
        if stat.st_size <= start:
            return
        try:
            with path.open("rb") as handle:
                handle.seek(start)
                data = handle.read(stat.st_size - start)
        except OSError:
            # A log that cannot be read must never make a market upload fail.
            return
        end = data.rfind(b"\n")
        if end < 0:
            return
        self.log_state["offset"] = start + end + 1
        for raw in data[:end].decode("utf-8", "replace").splitlines():
            self.note_client_line(ANSI.sub("", raw).strip())
        self.db.execute("DELETE FROM client_lines WHERE id <= (SELECT MAX(id) FROM client_lines) - 200")

    def note_client_line(self, line):
        match = LOG_LINE.match(line)
        if not match:
            return
        try:
            logged = datetime.fromisoformat(match["ts"])
        except ValueError:
            return
        logged = logged if logged.tzinfo else logged.astimezone()
        msg = match["msg"]
        version = LOG_VERSION.search(msg)
        if version:
            self.log_fork, self.pending_mine = FORK_VERSION_TAG in version["version"], []
        op = LOG_OPERATION.search(msg)
        number = int(op["op"]) if op else None
        if op:
            self.log_debug_seen = True
            if op["kind"] == "Request" and number in MARKET_LIST_OPS:
                self.pending_mine = []
            elif op["kind"] == "Request" and number in MY_ORDER_OPS:
                self.pending_mine.append(logged)
        if number in RAW_LOG_OPS or "Error while decoding" in msg or "GetMailInfos" in msg or "Mail Info" in msg:
            self.db.execute("INSERT OR IGNORE INTO client_lines(logged,op,text) VALUES(?,?,?)", (logged.isoformat(), number, msg[:4000]))

    def claim_my_order_list(self):
        """True when this market upload answers the in-game "my orders" list: the client logged
        a GetMyOpenAuctions (94) request after the last normal market request. The client
        uploads that answer like any market page, so without the log it cannot be told apart.
        The fork never does: it sends the list on marketorders.mine (ingest_my_orders)."""
        self.read_client_log()
        if self.log_fork:
            self.pending_mine = []
            return False
        now = self.clock()
        while self.pending_mine:
            if -10 <= (now - self.pending_mine.pop(0)).total_seconds() <= 120:
                return True
        return False

    def sync_my_orders(self, rows, received, complete=None):
        """Every order in the in-game "my orders" list is the player's own. Known ones get units
        left and price refreshed, a matching hand-entered one is linked, the rest are registered
        automatically. A tracked order missing from a fresh list of that city and side is gone
        (sold out, cancelled or expired) and closed. complete names the (city, side) lists known
        to be whole, empty ones included; by default every (city, side) seen in rows."""
        listed = defaultdict(set)
        for r, city, price, amount, _ in rows:
            side, oid = "sell" if r["AuctionType"] == "offer" else "buy", str(r["Id"])
            listed[(city, side)].add(oid)
            mine = self.db.execute("SELECT id,remaining FROM my_orders WHERE order_id=? AND closed IS NULL", (oid,)).fetchone()
            if mine is None:
                mine = self.db.execute("""SELECT id,remaining FROM my_orders WHERE order_id IS NULL AND closed IS NULL AND item=? AND city=? AND side=?
                    AND price=? AND amount>=? ORDER BY amount=? DESC, id LIMIT 1""", (r["ItemTypeId"], city, side, price, amount, amount)).fetchone()
            if mine:
                # Fewer units left than the last list showed: some sold (or filled) since.
                moved = received if mine["remaining"] is not None and amount < mine["remaining"] else None
                self.db.execute("UPDATE my_orders SET order_id=?,price=?,remaining=?,checked=?,moved=COALESCE(?,moved) WHERE id=?",
                                (oid, price, amount, received, moved, mine["id"]))
            else:
                self.db.execute("INSERT INTO my_orders(item,city,side,price,amount,created,order_id,remaining,checked,source) VALUES(?,?,?,?,?,?,?,?,?,'auto')",
                                (r["ItemTypeId"], city, side, price, amount, received, oid, amount, received))
        for city, side in (list(listed) if complete is None else complete):
            ids = listed[(city, side)]
            for row in self.db.execute("SELECT id,order_id FROM my_orders WHERE closed IS NULL AND order_id IS NOT NULL AND city=? AND side=?", (city, side)).fetchall():
                if row["order_id"] not in ids:
                    self.db.execute("UPDATE my_orders SET closed=? WHERE id=?", (received, row["id"]))

    def ingest_my_orders(self, payload):
        """One in-game "my orders" list (Kind sell or buy) from the fork's private topic
        marketorders.mine. These are the player's own orders: they update my_orders and
        never touch the order book or the public market feed.
        The game lists only the orders of the market the player stands in (LocationId; the
        orders themselves carry none) and at most one page of MY_ORDERS_PAGE. A shorter list is
        the whole list, so a tracked order of that market and side missing from it, or every
        one when the list is empty, is gone."""
        if not isinstance(payload, dict):
            raise ValueError("JSON object required")
        kind, rows = payload.get("Kind"), payload.get("Orders")
        if kind not in ("sell", "buy"):
            raise ValueError("Kind must be sell or buy")
        auction = "offer" if kind == "sell" else "request"
        city = location(payload.get("LocationId", ""))
        received = self.clock().isoformat()
        with self.lock, self.db:
            normalized = [row for row in self.normalize_orders(rows) if row[0]["AuctionType"] == auction]
            whole = city in CITIES and len(rows) < MY_ORDERS_PAGE
            self.pending_mine = []
            self.sync_my_orders(normalized, received, {(city, kind)} if whole else set())
            if normalized:
                encoded = json.dumps({"Kind": kind, "Orders": [r[0] for r in normalized]}, ensure_ascii=False, separators=(",", ":"))
                self.db.execute("INSERT INTO packets(received,topic,sha256,payload) VALUES(?,?,?,?)", (received, "marketorders.mine", hashlib.sha256(encoded.encode()).hexdigest(), encoded))
        return {"accepted": len(normalized), "received_at": received}

    def ingest_finished(self, payload):
        """One finished-trades tab list from the fork's private topic marketorders.finished,
        entries as the game sent them, for the market at LocationId. Only the latest list per
        market is kept, with the uploader's identity hashed like mail identities so holdings()
        can tell which entries the player bought."""
        if not isinstance(payload, dict):
            raise ValueError("JSON object required")
        entries, place = payload.get("Entries"), str(payload.get("LocationId") or "")
        if not isinstance(entries, list) or not all(isinstance(e, dict) for e in entries):
            raise ValueError("Entries must be a list of objects")
        if not place:
            raise ValueError("LocationId required")
        character = str(payload.get("CharacterId") or "").strip()
        received = self.clock().isoformat()
        with self.lock, self.db:
            self.db.execute("""INSERT INTO finished_lists(location,city,received,entries,identity) VALUES(?,?,?,?,?) ON CONFLICT(location) DO UPDATE SET
                city=excluded.city,received=excluded.received,entries=excluded.entries,identity=excluded.identity""",
                            (place, location(place), received, json.dumps(entries, ensure_ascii=False, separators=(",", ":")),
                             character_identity(character) if character else None))
        return {"accepted": len(entries), "received_at": received}

    def set_bank(self, values):
        """Units of a tracked item the player keeps in that city's bank, entered by hand: the
        capture client does not read the bank. 0 clears the entry."""
        item, city, amount = values.get("item"), values.get("city"), values.get("amount")
        if item not in self.cfg["items"] or city not in CITIES:
            raise ValueError("Unknown item / city")
        if isinstance(amount, bool) or not isinstance(amount, int) or not 0 <= amount <= 10**9:
            raise ValueError("Amount must be a whole number from 0")
        with self.lock, self.db:
            if amount:
                self.db.execute("INSERT INTO bank_stock VALUES(?,?,?,?) ON CONFLICT(item,city) DO UPDATE SET amount=excluded.amount,updated=excluded.updated",
                                (item, city, amount, self.clock().isoformat()))
            else:
                self.db.execute("DELETE FROM bank_stock WHERE item=? AND city=?", (item, city))

    def set_bank_city(self, values):
        """The city of a bank cluster the fork read, picked on the page: remembered for later visits."""
        place, city = str(values.get("location") or ""), values.get("city")
        if city not in CITIES:
            raise ValueError("Unknown city")
        with self.lock, self.db:
            if not self.db.execute("SELECT 1 FROM bank_auto WHERE location=?", (place,)).fetchone():
                raise ValueError("Unknown bank")
            self.db.execute("INSERT INTO bank_cities VALUES(?,?) ON CONFLICT(location) DO UPDATE SET city=excluded.city", (place, city))
            self.db.execute("UPDATE bank_auto SET city=? WHERE location=?", (city, place))

    def holdings(self, open_orders, purchases, books=None):
        """Units of tracked items the player holds per city: listed (units left on own sell
        orders), the bank (entered by hand) and the finished-trades tab (bought, not yet
        collected), valued at the average cost from purchases. A finished-trades entry counts
        when its buyer is the uploader and HasBuyerFetched is false (seen on the live server,
        2026-10-27: buy-order fills waiting in the tab); entries the player sold are skipped.
        Each market's tab is as of its latest list. Units are also priced the dearest way to sell
        them (best_listing, see listing_options) in the trading city where all my units come
        to the most, less transport for units held in another city (carried units are with me)."""
        keep = 1 - self.cfg["sales_tax"] - self.cfg["setup_fee"]
        costs = {s["item"]: s["recent"]["cost_average"] or s["all"]["cost_average"] for s in purchases["summary"]}
        cells = defaultdict(lambda: {"listed": 0, "bank": 0, "bank_updated": None, "bank_source": None, "finished": 0, "finished_seen": None, "carried": 0})
        for o in open_orders:
            if o["side"] == "sell" and o["remaining"]:
                cells[(o["city"], o["item"])]["listed"] += o["remaining"]
        with self.lock:
            # A bank the fork read replaces what was typed in for that city: it lists every stack.
            auto, unmapped = defaultdict(dict), []
            chosen = dict(self.db.execute("SELECT location,city FROM bank_cities").fetchall())
            for r in self.db.execute("SELECT location,city,items,seen FROM bank_auto ORDER BY seen"):
                city = r["city"] or chosen.get(r["location"]) or BANK_CITIES.get(r["location"])
                if city is None:
                    unmapped.append({"location": r["location"], "items": json.loads(r["items"]), "seen": r["seen"]})
                    continue
                for item, amount in json.loads(r["items"]).items():
                    auto[city][item] = auto[city].get(item, 0) + amount
                auto[city][""] = r["seen"]  # newest bank read for the city (rows come oldest first)
            for city, items in auto.items():
                seen = items.pop("")
                for item, amount in items.items():
                    cells[(city, item)].update(bank=amount, bank_updated=seen, bank_source="auto")
            for r in self.db.execute("SELECT item,city,amount,updated FROM bank_stock"):
                if r["city"] not in auto:
                    cells[(r["city"], r["item"])].update(bank=r["amount"], bank_updated=r["updated"], bank_source="manual")
            carried = self.db.execute("SELECT items,seen FROM carried WHERE id=1").fetchone()
            for item, amount in (json.loads(carried["items"]).items() if carried else ()):
                cells[(CARRIED, item)].update(carried=amount, bank_updated=carried["seen"])
            lists = self.db.execute("SELECT location,city,received,entries,identity FROM finished_lists ORDER BY received DESC").fetchall()
        finished = []
        for r in lists:
            entries, waiting = json.loads(r["entries"]), 0
            for e in entries:
                if not r["identity"] or e.get("HasBuyerFetched") is not False or e.get("ItemTypeId") not in self.cfg["items"]:
                    continue
                if int(e.get("QualityLevel") or 1) != 1 or int(e.get("EnchantmentLevel") or 0) != 0:
                    continue
                if character_identity(str(e.get("BuyerCharacterId") or "")) != r["identity"] or r["city"] not in CITIES:
                    continue
                cell = cells[(r["city"], e["ItemTypeId"])]
                cell["finished"] += int(e.get("Amount") or 0)
                cell["finished_seen"] = r["received"]
                waiting += int(e.get("Amount") or 0)
            finished.append({"location": r["location"], "city": r["city"], "received": r["received"], "entries": len(entries),
                             "tracked": sum(1 for e in entries if e.get("ItemTypeId") in self.cfg["items"]),
                             "waiting": waiting, "identity_known": bool(r["identity"])})
        held = defaultdict(int)
        for (city, item), cell in cells.items():
            held[item] += cell["listed"] + cell["bank"] + cell["finished"] + cell["carried"]
        best_listing = {}
        for item in self.cfg["items"]:
            for c in self.trade_cities(item):
                book = (books or {}).get(item, {}).get(c, {}).get("sell")
                if not book or not book["levels"] or book.get("single_only"):
                    continue
                own = defaultdict(int)
                for o in open_orders:
                    if o["side"] == "sell" and o["remaining"] and (o["item"], o["city"]) == (item, c):
                        own[o["price"]] += o["remaining"]
                for option in self.listing_options(book["levels"], own, held[item], keep):
                    if item not in best_listing or (option["value"], option["net"]) > (best_listing[item]["value"], best_listing[item]["net"]):
                        best_listing[item] = {"city": c, "seen": book["seen"], "fresh": book["fresh"], "held": held[item], **option}
        rows = []
        for (city, item), cell in sorted(cells.items(), key=lambda kv: (list(CITIES).index(kv[0][0]) if kv[0][0] in CITIES else 99, kv[0][1])):
            total, cost = cell["listed"] + cell["bank"] + cell["finished"] + cell["carried"], costs.get(item)
            if not total and cell["bank_source"] != "manual":
                continue
            # 지금 이 도시에서 팔면 받는 돈: 매수 요청서에 즉시 판매(최고 매수가)에서 판매세를 뺀 값.
            # 체결을 보장하지는 않습니다 — 그 가격의 잔량(sell_now_amount)도 같이 보냅니다.
            sell_now = sell_now_amount = None
            book = (books or {}).get(item, {}).get(city, {}).get("buy") if books else None
            if book and not book.get("single_only"):
                levels = book["fresh_levels"] if book.get("fresh") else book.get("levels") or []
                if levels:
                    sell_now, sell_now_amount = levels[0]["price"], levels[0]["amount"]
            net = sell_now * (1 - self.cfg["sales_tax"]) if sell_now is not None else None
            best = best_listing.get(item)
            listed_net = best["net"] - (self.cfg["transport_per_unit"] if city not in (best["city"], CARRIED) else 0) if best else None
            rows.append({"city": city, "item": item, **cell, "total": total, "cost": cost,
                         "list_city": best and best["city"], "list_net": listed_net,
                         "list_value": total * listed_net if listed_net is not None else None,
                         "value": total * cost if cost is not None else None,
                         "sell_now": sell_now, "sell_now_amount": sell_now_amount,
                         "net_now": net,
                         "value_now": total * net if net is not None else None,
                         "profit_now": total * (net - cost) if net is not None and cost is not None else None})
        return {"rows": rows, "finished": finished, "unmapped_banks": unmapped, "best_listing": best_listing}

    def listing_options(self, levels, own, held, keep):
        """Ways to sell `held` units with a sell order in one market: one silver under the
        lowest ask (at my own price when the lowest ask is mine), or one silver under another
        seller's higher ask after buying every other seller's listing below it (up to
        BUYOUT_LEVELS price levels and trade_budget silver). My own listings are counted in
        `held` and simply listed again at the new price. value = what all units, bought ones
        included, bring after sales tax and setup fee, less the silver spent buying; net =
        value per held unit. Only prices seen in the list are targets."""
        low = levels[0]["price"]
        price = low if own.get(low) else low - 1
        options = [{"price": price, "own": bool(own.get(low)), "sweep": None, "value": held * price * keep}]
        others = [(l["price"], l["amount"] - own.get(l["price"], 0)) for l in levels if l["amount"] > own.get(l["price"], 0)]
        units = cost = 0
        for k, (floor, amount) in enumerate(others[:BUYOUT_LEVELS + 1]):
            if not held or cost > self.cfg["trade_budget"]:
                break
            if floor - 1 > price:
                options.append({"price": floor - 1, "own": False, "value": (held + units) * (floor - 1) * keep - cost,
                                "sweep": {"units": units, "cost": cost, "levels": k, "floor_before": low, "plain_price": price}})
            units, cost = units + amount, cost + floor * amount
        for o in options:
            o["net"] = o["value"] / held if held else o["price"] * keep
        return options

    def ingest_holdings(self, payload):
        """Units per item the fork read from the inventory and from each bank opened this
        session (private topic holdings.containers, items keyed by the game's item index). A bank
        stands in its own cluster beside the city; its city is the cluster itself when that is a
        market, else the city learned for that cluster before, else the city the player came
        from when entering it (then learned). The inventory replaces the carried units."""
        if not isinstance(payload, dict) or not isinstance(payload.get("Inventory"), dict) or not isinstance(payload.get("Banks"), list):
            raise ValueError("Inventory object and Banks list required")
        def tracked(items):
            if not isinstance(items, dict):
                raise ValueError("Items must be an object")
            out = {}
            for key, amount in items.items():
                if isinstance(amount, bool) or not isinstance(amount, int) or amount < 0:
                    raise ValueError("Item amounts must be whole numbers")
                item = self.cfg["item_numeric_ids"].get(str(key))
                if item in self.cfg["items"] and amount:
                    out[item] = out.get(item, 0) + amount
            return out
        seen = self.clock().isoformat()
        banks = []
        for bank in payload["Banks"]:
            if not isinstance(bank, dict) or not str(bank.get("LocationId") or ""):
                raise ValueError("Each bank needs a LocationId")
            banks.append((str(bank["LocationId"]), location(bank.get("PreviousLocationId") or ""), tracked(bank.get("Items"))))
        carried = tracked(payload["Inventory"])
        with self.lock, self.db:
            self.db.execute("INSERT INTO carried VALUES(1,?,?) ON CONFLICT(id) DO UPDATE SET items=excluded.items,seen=excluded.seen",
                            (json.dumps(carried, sort_keys=True), seen))
            for place, previous, items in banks:
                city = location(place)
                if city not in CITIES:
                    learned = self.db.execute("SELECT city FROM bank_cities WHERE location=?", (place,)).fetchone()
                    city = learned["city"] if learned else BANK_CITIES.get(place) or (previous if previous in CITIES else None)
                    if city and not learned:
                        self.db.execute("INSERT INTO bank_cities VALUES(?,?)", (place, city))
                self.db.execute("INSERT INTO bank_auto VALUES(?,?,?,?) ON CONFLICT(location) DO UPDATE SET city=excluded.city,items=excluded.items,seen=excluded.seen",
                                (place, city, json.dumps(items, sort_keys=True), seen))
        return {"accepted": len(banks), "carried": sum(carried.values()), "received_at": seen}

    def capital(self, cache, open_orders, holdings):
        """Silver tied up in tracked items: units held (holdings) at their average cost plus
        silver locked in open buy orders (units left x price), logged at most once an hour so its
        growth can be followed. Per item, the inventory in days of the item's market-wide daily
        sales (volume_guide summed over cities): units held and still being bought, over that
        pace. heavy once it reaches the inventory_heavy_days setting."""
        now, items = self.clock(), {}
        blank = lambda: {"held": 0, "buying": 0, "value": 0.0, "orders_silver": 0.0, "unpriced": 0, "cost": None}
        for r in holdings["rows"]:
            it = items.setdefault(r["item"], blank())
            it["held"] += r["total"]
            it["cost"] = r["cost"]
            if r["cost"] is None:
                it["unpriced"] += r["total"]
            else:
                it["value"] += r["total"] * r["cost"]
        for o in open_orders:
            if o["side"] == "buy" and o["remaining"]:
                it = items.setdefault(o["item"], blank())
                it["buying"] += o["remaining"]
                it["orders_silver"] += o["remaining"] * o["price"]
        heavy_days = self.cfg["inventory_heavy_days"]
        for item, it in items.items():
            paces = [p for c in CITIES if (p := self.volume_guide(cache, item, c)["per_day"]) is not None]
            it["per_day"] = sum(paces) if paces else None
            it["days"] = (it["held"] + it["buying"]) / it["per_day"] if it["per_day"] else None
            it["heavy"] = it["days"] is not None and it["days"] >= heavy_days
        holdings_value = sum(it["value"] for it in items.values())
        orders_silver = sum(it["orders_silver"] for it in items.values())
        unpriced = sum(it["unpriced"] for it in items.values())
        total = holdings_value + orders_silver
        units = {item: it["held"] for item, it in items.items() if it["held"]}
        with self.lock:
            last = self.db.execute("SELECT at FROM capital_log ORDER BY at DESC LIMIT 1").fetchone()
            if (total or units or last) and (last is None or now - parse_time(last["at"]) >= timedelta(hours=1)):
                with self.db:
                    self.db.execute("INSERT INTO capital_log VALUES(?,?,?,?,?,?)", (now.isoformat(), total, holdings_value, orders_silver, unpriced,
                                                                                   json.dumps(units, sort_keys=True)))
                    self.db.execute("DELETE FROM capital_log WHERE at<?", ((now - timedelta(days=120)).isoformat(),))
            changes = []
            for days in (1, 7, 30):
                then = self.db.execute("SELECT at,total FROM capital_log WHERE at<=? ORDER BY at DESC LIMIT 1", ((now - timedelta(days=days)).isoformat(),)).fetchone()
                changes.append({"days": days, "at": then["at"] if then else None, "total": then["total"] if then else None,
                                "delta": total - then["total"] if then else None,
                                "ratio": (total - then["total"]) / then["total"] if then and then["total"] else None})
            zone, daily = ZoneInfo(self.cfg["timezone"]), {}
            for r in self.db.execute("SELECT at,total,holdings,orders,unpriced FROM capital_log WHERE at>=? ORDER BY at", ((now - timedelta(days=30)).isoformat(),)):
                daily[parse_time(r["at"]).astimezone(zone).date().isoformat()] = dict(r)
        return {"total": total, "holdings": holdings_value, "orders": orders_silver, "unpriced": unpriced, "heavy_days": heavy_days,
                "items": items, "changes": changes, "daily": [{"date": d, **r} for d, r in daily.items()]}

    def client_debug(self):
        lines = [dict(r) for r in self.db.execute("SELECT logged,op,text FROM client_lines ORDER BY logged DESC, id DESC LIMIT 15")]
        last = self.db.execute("SELECT MAX(checked) FROM my_orders").fetchone()[0]
        direct = self.db.execute("SELECT last FROM private_topics WHERE topic='marketorders.mine'").fetchone()
        return {"enabled": self.log_debug_seen, "fork": self.log_fork, "operations": CAPTURE_DEBUG_OPERATIONS, "lines": lines,
                "last_my_orders": last, "last_direct": direct[0] if direct else None}

    def my_orders(self, books):
        """Open own orders with what the latest scans say: units left, units filled since,
        units ahead of them in the queue, and whether they are the best price right now."""
        rows = []
        for mine in self.db.execute("SELECT * FROM my_orders WHERE closed IS NULL ORDER BY item,city,side,price"):
            r = dict(mine)
            latest = self.db.execute("SELECT amount,seen FROM orders WHERE order_id=? ORDER BY seen DESC LIMIT 1", (r["order_id"],)).fetchone() if r["order_id"] else None
            # Units left come from whichever was seen last: a market scan or the "my orders" list.
            remaining, seen = (r["remaining"] if r["remaining"] is not None else r["amount"]), r["checked"]
            if latest and (seen is None or latest["seen"] > seen):
                remaining, seen = latest["amount"], latest["seen"]
            r["remaining"], r["seen"] = remaining, seen
            fills = self.db.execute("SELECT COALESCE(SUM(amount),0) FROM order_fills WHERE order_id=?", (r["order_id"],)).fetchone()[0] if r["order_id"] else 0
            r["filled"] = max(fills, r["amount"] - remaining)
            levels = books[r["item"]][r["city"]][r["side"]]["levels"]
            better = (lambda price: price < r["price"]) if r["side"] == "sell" else (lambda price: price > r["price"])
            r["ahead"] = sum(l["amount"] for l in levels if better(l["price"]))
            r["best"] = levels[0]["price"] if levels else None
            r["is_best"] = bool(levels) and not r["ahead"]
            rows.append(r)
        return rows

    def note_private(self, topic):
        """Counts every private upload so the page can tell whether the capture
        client's private (-p) channel reaches this receiver at all."""
        with self.lock, self.db:
            self.db.execute("INSERT INTO private_topics VALUES(?,1,?) ON CONFLICT(topic) DO UPDATE SET count=count+1,last=excluded.last",
                            (str(topic)[:64], self.clock().isoformat()))

    def ingest_sale(self, payload):
        # Every mail the client sends is logged with its outcome, so a mail that was
        # opened in game but never stored can be traced to the step that dropped it.
        row = payload.get("Notification") if isinstance(payload, dict) else None
        row = row if isinstance(row, dict) else {}
        kind = payload.get("NotificationType") if isinstance(payload, dict) else None
        try:
            result = self.store_sale(payload)
        except ValueError as exc:
            self.note_mail(kind, row, "rejected", str(exc))
            raise
        outcome = "saved" if result.get("accepted") else "conflict" if result.get("conflict") else "duplicate" if result.get("duplicate") else "ignored"
        self.note_mail(kind, row, outcome, result.get("reason"))
        return result

    def note_mail(self, kind, row, outcome, detail):
        with self.lock, self.db:
            self.db.execute("INSERT INTO mail_events(received,kind,item,location,outcome,detail) VALUES(?,?,?,?,?,?)",
                            (self.clock().isoformat(), str(kind)[:64] if kind is not None else None, str(row.get("ItemTypeId", ""))[:64],
                             str(row.get("LocationId", ""))[:64], outcome, (detail or "")[:200]))
            self.db.execute("DELETE FROM mail_events WHERE id <= (SELECT MAX(id) FROM mail_events) - 200")

    def mail_diagnostics(self):
        with self.lock:
            events = [dict(r) for r in self.db.execute("SELECT received,kind,item,location,outcome,detail FROM mail_events ORDER BY id DESC LIMIT 12")]
            counts = dict(self.db.execute("SELECT outcome,COUNT(*) FROM mail_events GROUP BY outcome").fetchall())
            topics = [dict(r) for r in self.db.execute("SELECT topic,count,last FROM private_topics ORDER BY last DESC")]
        return {"events": events, "counts": counts, "private_topics": topics, "client_log": client_mail_log(self.root)}

    def store_sale(self, payload):
        """A completed sell-mail summary, not an execution timestamp or market volume.
        This topic already expresses UnitPriceSilver in silver, unlike live orders.
        Never use the client's hard-coded TotalAfterTaxes (3%) as actual proceeds.
        """
        if not isinstance(payload, dict):
            raise ValueError("JSON object required")
        notification = payload.get("NotificationType")
        if notification != "SalesNotification" and notification not in MAIL_KINDS:
            return {"accepted": 0, "ignored": True, "reason": "알 수 없는 메일 종류 (" + str(notification) + ")"}
        row = payload.get("Notification")
        if not isinstance(row, dict):
            raise ValueError("Notification object required")
        item, city = row.get("ItemTypeId"), location(row.get("LocationId", ""))
        if item not in self.cfg["items"]:
            return {"accepted": 0, "ignored": True, "reason": "추적하지 않는 품목 " + str(item)}
        if city not in CITIES:
            return {"accepted": 0, "ignored": True, "reason": "알 수 없는 도시 코드 " + str(row.get("LocationId"))}
        def integer(key, minimum=1):
            value = row.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, str)):
                raise ValueError("Invalid mail " + key)
            value = int(value)
            if not minimum <= value <= 2**63-1:
                raise ValueError("Invalid mail " + key)
            return value
        mail_id = str(integer("Id"))
        character = str(payload.get("CharacterId") or payload.get("CharacterName") or "").strip()
        identity = character_identity(character)
        seen = self.clock().isoformat()
        # The fork reports when the game sent the mail; the official client does not.
        try:
            mailed = parse_time(row["Received"]).isoformat() if row.get("Received") else None
        except (ValueError, TypeError, OverflowError, OSError):
            mailed = None
        if notification in MAIL_KINDS:
            return self.store_mail(MAIL_KINDS[notification], row, integer, identity, mail_id, item, city, seen, mailed, bool(character))
        price, amount = integer("UnitPriceSilver"), integer("Amount")
        with self.lock, self.db:
            previous = self.db.execute("SELECT * FROM personal_sales WHERE identity=? AND mail_id=?", (identity, mail_id)).fetchone()
            if previous:
                conflict = bool(previous["conflict"]) or any(previous[k] != v for k,v in (("item",item),("city",city),("price",price),("amount",amount)))
                self.db.execute("UPDATE personal_sales SET last_seen=?,conflict=?,mailed=COALESCE(mailed,?) WHERE identity=? AND mail_id=?", (seen,int(conflict),mailed,identity,mail_id))
                return {"accepted": 0, "duplicate": True, "conflict": conflict}
            self.db.execute("""INSERT INTO personal_sales(identity,mail_id,item,city,price,amount,first_seen,last_seen,source_expires,identity_known,conflict,mailed)
                VALUES(?,?,?,?,?,?,?,?,?,?,0,?)""", (identity,mail_id,item,city,price,amount,seen,seen,str(row.get("Expires") or ""),int(bool(character)),mailed))
        return {"accepted": 1, "received_at": seen}

    def store_mail(self, kind, row, integer, identity, mail_id, item, city, seen, mailed, identity_known):
        """A buy-order or expiry mail, in whole silver like sale mails. amount is the units that
        traded (bought or sold), order_amount the order's size, silver the total moved: spent on
        a completed buy, refunded on an expired buy, earned on an expired sell. price is the unit
        price the fork derived, None when no unit traded or none was left to refund."""
        if kind == "buy":
            amount = order_amount = integer("Amount")
            price, silver = integer("UnitPriceSilver"), integer("TotalSilver")
        else:
            order_amount = integer("Amount")
            amount = integer("Bought" if kind == "buy_expired" else "Sold", 0)
            silver = integer("RefundSilver" if kind == "buy_expired" else "TotalSilver", 0)
            price = integer("UnitPriceSilver", 0) or None
            if amount > order_amount:
                raise ValueError("Traded units exceed the order size")
        values = {"kind": kind, "item": item, "city": city, "price": price, "amount": amount, "order_amount": order_amount, "silver": silver}
        with self.lock, self.db:
            previous = self.db.execute("SELECT * FROM personal_mails WHERE identity=? AND mail_id=?", (identity, mail_id)).fetchone()
            if previous:
                conflict = bool(previous["conflict"]) or any(previous[k] != v for k, v in values.items())
                self.db.execute("UPDATE personal_mails SET last_seen=?,conflict=?,mailed=COALESCE(mailed,?) WHERE identity=? AND mail_id=?", (seen, int(conflict), mailed, identity, mail_id))
                return {"accepted": 0, "duplicate": True, "conflict": conflict}
            self.db.execute("""INSERT INTO personal_mails(identity,mail_id,kind,item,city,price,amount,order_amount,silver,first_seen,last_seen,identity_known,conflict,mailed)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,0,?)""", (identity, mail_id, kind, item, city, price, amount, order_amount, silver, seen, seen, int(identity_known), mailed))
        return {"accepted": 1, "received_at": seen}

    def personal_mails(self):
        """Buy-order and expiry mails, listed apart from the sale statistics."""
        with self.lock:
            rows = [dict(r) for r in self.db.execute("""SELECT kind,item,city,price,amount,order_amount,silver,first_seen,mailed FROM personal_mails
                WHERE conflict=0 ORDER BY COALESCE(mailed,first_seen) DESC, mail_id DESC LIMIT 60""")]
            counts = dict(self.db.execute("SELECT kind,COUNT(*) FROM personal_mails WHERE conflict=0 GROUP BY kind").fetchall())
            conflicts = self.db.execute("SELECT COUNT(*) FROM personal_mails WHERE conflict=1").fetchone()[0]
        return {"recent": rows, "counts": counts, "conflicts": conflicts,
                "note": "구매 주문 완료·만료 메일. 판매 통계(시장별 합계·누적 최고 단가)에는 더하지 않음. 시각은 게임이 메일을 보낸 때."}

    def personal_sales(self, item, city):
        with self.lock:
            rows = [dict(r) for r in self.db.execute("SELECT item,city,price,amount,first_seen,last_seen,identity_known,mailed FROM personal_sales WHERE conflict=0 ORDER BY first_seen DESC,mail_id DESC")]
            markets = {}
            for row in rows:
                key = row["item"] + "/" + row["city"]
                group = markets.setdefault(key, {"highest_price":row["price"],"highest_price_amount":0,"total_amount":0,"mail_count":0,"first_read":row["first_seen"]})
                group["highest_price"] = max(group["highest_price"],row["price"])
                group["total_amount"] += row["amount"]
                group["mail_count"] += 1
            for key, group in markets.items():
                group["highest_price_amount"] = sum(r["amount"] for r in rows if r["item"]+"/"+r["city"]==key and r["price"]==group["highest_price"])
            selected = [r for r in rows if r["item"]==item and r["city"]==city]
            return {"by_market":markets,"selected":selected[:100],"selected_count":len(selected),"mail_count":len(rows),"recent":rows[:30],
                "conflicts":self.db.execute("SELECT COUNT(*) FROM personal_sales WHERE conflict=1").fetchone()[0],
                "unknown_identity":sum(not r["identity_known"] for r in rows),
                "note":"판매 완료 메일의 누적 기록 · ‘처음 확인한 때’는 이 프로그램이 처음 읽은 때, ‘메일 받은 때’는 게임이 메일을 보낸 때(전용 수집기만 제공). 시장 거래량·요일·시간대 집계에 더하지 않음. 같은 메일은 중복 집계하지 않음."}

    def reset_book(self, item, city):
        if item not in self.cfg["items"] or city not in CITIES:
            raise ValueError("Unknown item/city")
        with self.lock, self.db:
            for side in ("sell", "buy"):
                self.db.execute("INSERT INTO sessions(item,city,side,started,last_seen) VALUES(?,?,?,?,?)", (item, city, side, self.clock().isoformat(), self.clock().isoformat()))

    def book(self, item, city, side):
        session = self.db.execute("SELECT * FROM sessions WHERE item=? AND city=? AND side=? ORDER BY id DESC LIMIT 1", (item, city, side)).fetchone()
        if session is None:
            return {"levels": [], "fresh_levels": [], "seen": None, "fresh": False, "stale": False,
                    "depth": describe_depth([], side, self.cfg), "analysis_depth": describe_depth([], side, self.cfg), "coverage": "미조회"}
        latest_id = session["id"]
        # A reset creates an empty scan, not evidence that the market is empty.
        # Keep the last received snapshot visible until new orders arrive.
        if not self.db.execute("SELECT 1 FROM orders WHERE session=? LIMIT 1", (latest_id,)).fetchone():
            previous = self.db.execute("SELECT s.* FROM sessions s WHERE s.item=? AND s.city=? AND s.side=? AND EXISTS(SELECT 1 FROM orders o WHERE o.session=s.id) ORDER BY s.id DESC LIMIT 1", (item, city, side)).fetchone()
            if previous is not None:
                session = previous
        archived_snapshot = session["id"] != latest_id
        # Expiry changes whether a quote is actionable, not whether it was observed.
        rows = self.db.execute("SELECT * FROM orders WHERE session=? AND amount>0", (session["id"],)).fetchall()
        single_only = len(rows) == 1
        if single_only:
            # Clicking a selected order also produces a 1-order response. It does
            # not establish the market's lowest ask/highest bid. Prefer the last
            # multi-order observation, retaining its age and marking the fallback.
            bulk = self.db.execute("SELECT s.* FROM sessions s WHERE s.item=? AND s.city=? AND s.side=? AND s.id<? AND (SELECT COUNT(*) FROM orders o WHERE o.session=s.id AND o.amount>0)>=2 ORDER BY s.id DESC LIMIT 1", (item, city, side, session['id'])).fetchone()
            if bulk is not None:
                latest_rows = rows
                rows = self.db.execute("SELECT * FROM orders WHERE session=? AND amount>0", (bulk['id'],)).fetchall()
                merged = {r['order_id']:r for r in rows}
                merged.update({r['order_id']:r for r in latest_rows})
                rows = list(merged.values())
                session = bulk
                archived_snapshot = True
        levels = {}
        for r in rows:
            age = (self.clock() - parse_time(r["seen"])).total_seconds()
            p = r["price"]
            level = levels.setdefault(p, {"price": p, "amount": 0, "orders": 0, "seen": r["seen"], "oldest_seen": r["seen"], "stale": False, "small_orders": 0})
            level["amount"] += r["amount"]
            level["orders"] += 1
            level["small_orders"] += int(r["amount"] <= self.cfg["small_quantity_max"])
            level["stale"] = level["stale"] or archived_snapshot or age > self.cfg["fresh_minutes"] * 60 or parse_time(r["expires"]) <= self.clock()
            level["seen"] = max(level["seen"], r["seen"])
            level["oldest_seen"] = min(level["oldest_seen"], r["seen"])
        result = sorted(levels.values(), key=lambda r: r["price"], reverse=side == "buy")
        cumulative = 0
        for level in result:
            cumulative += level["amount"]
            level["cumulative"] = cumulative
        fresh = [dict(r) for r in result if not r["stale"]] if not single_only else []
        depth = describe_depth(result, side, self.cfg)
        analysis_depth = describe_depth(fresh, side, self.cfg)
        return {"levels": result, "fresh_levels": fresh, "depth": depth, "analysis_depth": analysis_depth,
                "seen": session["last_seen"], "fresh": bool(fresh), "stale": bool(result) and not fresh,
                "mixed_freshness": bool(fresh) and len(fresh) < len(result),
                "awaiting_scan": archived_snapshot,
                "single_only": single_only,
                "coverage": "이전 관측 보관 · 현재 분석에서 제외" if result and not fresh else "조회된 일부 주문 · 전체 수량 미확인", "session": session["id"]}

    def history_rows(self, item, city, days):
        """Choose disjoint intervals. Prefer hourly, then complete non-overlapping 6h.

        AODP graph timestamps are treated as bucket END (see source service's midnight note).
        Keep originals and expose the convention; do not interpolate missing buckets.
        """
        end = self.clock()
        start = end - timedelta(days=days)
        # A single resolution per report avoids ranking 1h spikes against smoothed 6h prices.
        available = self.db.execute("SELECT DISTINCT hours FROM history WHERE item=? AND city=? AND stamp>? AND stamp<=?", (item, city, start.isoformat(), end.isoformat())).fetchall()
        resolutions = {r[0] for r in available}
        preferred = 1 if days == 1 else (6 if 6 in resolutions else 1)
        rows = self.db.execute("SELECT * FROM history WHERE item=? AND city=? AND hours=? AND stamp>? AND stamp<=? ORDER BY stamp", (item, city, preferred, (start - timedelta(hours=6)).isoformat(), end.isoformat())).fetchall()
        selected, occupied = [], []
        for raw in rows:
            r = dict(raw)
            b = parse_time(r["stamp"])
            a = b - timedelta(hours=r["hours"])
            # Do not prorate buckets crossing rolling-window edges.
            if a < start or b > end or a >= b:
                continue
            if any(a < y and b > x for x, y in occupied):
                continue
            r.update(start=a.isoformat(), end=b.isoformat(), avg=r["silver"] / r["amount"] if r["amount"] else None)
            selected.append(r)
            occupied.append((a, b))
        return sorted(selected, key=lambda r: r["stamp"])

    def history_summary(self, item, city, days):
        rows = self.history_rows(item, city, days)
        total = sum(r["amount"] for r in rows)
        silver = sum(r["silver"] for r in rows)
        positive = [r for r in rows if r["avg"] is not None and r["amount"] > 0]
        high = max((r["avg"] for r in positive), default=None)
        near = [r for r in positive if r["avg"] >= high * self.cfg["near_high_fraction"]] if high else []
        coverage = sum(r["hours"] for r in rows)
        newest = max((r["stamp"] for r in rows), default=None)
        resolution = rows[0]["hours"] if rows else None
        history_age = max(0, (self.clock() - parse_time(newest)).total_seconds() / 3600) if newest else None
        return {"days": days, "amount": total if rows else None, "weighted_average": silver / total if total else None,
                "covered_hours": coverage, "requested_hours": days * 24, "coverage": min(1, coverage / (days * 24)),
                "highest_bucket_average": high, "near_high_amount": sum(r["amount"] for r in near) if near else None,
                "top_buckets": sorted(positive, key=lambda r: r["avg"], reverse=True)[:12],
                "near_high_buckets": sorted(near, key=lambda r: r["avg"], reverse=True),
                "last_observed": max((r["seen"] for r in rows), default=None),
                "newest_bucket_end": newest, "buckets": rows,
                "resolution_hours": resolution, "age_hours": history_age,
                "stale": history_age is not None and history_age > 2 * resolution,
                "label": "구간 평균가 · 해당 구간 전체 판매량 (개별 체결가별 수량 아님)"}

    def city_extremes(self, books):
        result = {}
        for item, cities in books.items():
            result[item] = {}
            for side in ('buy', 'sell'):
                fresh, past = [], []
                for city, pair in cities.items():
                    book = pair[side]
                    if book.get('single_only'):
                        continue
                    levels = book['fresh_levels'] if book['fresh'] else book['levels']
                    if not levels:
                        continue
                    row = {**levels[0], 'city': city}
                    (fresh if book['fresh'] else past).append(row)
                pool = fresh or past
                best = (max if side == 'buy' else min)((r['price'] for r in pool), default=None)
                result[item][side] = {'price': best, 'fresh': bool(fresh),
                    'observations': [r for r in pool if r['price'] == best],
                    'compared_cities': len(pool), 'scope': '유효 관측끼리 비교' if fresh else '지난 관측끼리 비교 · 수집 시각 다름'}
        return result

    def patterns(self, item, city):
        zone = ZoneInfo(self.cfg['timezone'])
        end, start = self.clock(), self.clock() - timedelta(days=28)
        hourly, six = [], []
        for raw in self.db.execute('SELECT * FROM history WHERE item=? AND city=? AND stamp>? AND stamp<=? ORDER BY hours,stamp', (item, city, start.isoformat(), end.isoformat())):
            r = dict(raw)
            a, b = parse_time(r['stamp'])-timedelta(hours=r['hours']), parse_time(r['stamp'])
            if a < start or b > end:
                continue
            r.update(start=a, end=b)
            (hourly if r['hours'] == 1 else six).append(r)
        def add(groups, key, r):
            g = groups.setdefault(key, {'total': 0, 'observed_hours': 0, 'silver': 0})
            g['total'] += r['amount']; g['observed_hours'] += r['hours']; g['silver'] += r['silver']
        hourly_groups, weekday_groups = {}, {}
        # Hour-of-day uses only hourly observations. Coarse data are never split.
        for r in hourly:
            add(hourly_groups, r['start'].astimezone(zone).hour, r)
        # Prefer actual hourly observations, then fill disjoint gaps with complete
        # six-hour intervals only when they lie entirely within one local date.
        occupied = [(r['start'], r['end']) for r in hourly]
        weekday_rows = list(hourly)
        crossing_amount, crossing_buckets = 0, 0
        for r in six:
            a, b = r['start'], r['end']
            if any(a < y and b > x for x, y in occupied):
                continue
            if a.astimezone(zone).date() != (b-timedelta(microseconds=1)).astimezone(zone).date():
                crossing_amount += r['amount']; crossing_buckets += 1
                continue
            weekday_rows.append(r); occupied.append((a,b))
        for r in weekday_rows:
            add(weekday_groups, r['start'].astimezone(zone).weekday(), r)
        def records(groups):
            return [{'key': k, **g, 'per_observed_hour': g['total']/g['observed_hours'], 'average_price': g['silver']/g['total'] if g['total'] else None} for k,g in sorted(groups.items())]
        known = {r['key']:r for r in records(weekday_groups)}
        weekdays = [known.get(k, {'key':k,'total':None,'observed_hours':0,'per_observed_hour':None,'average_price':None}) for k in range(7)]
        return {'hourly':records(hourly_groups), 'weekdays':weekdays, 'observed_hours':len(hourly),
            'weekday_observed_hours':sum(r['hours'] for r in weekday_rows),
            'cross_day_buckets':crossing_buckets, 'cross_day_amount':crossing_amount,
            'note':f'최근 28일 · 요일은 1시간 자료 우선, 겹치지 않고 같은 프랑스 날짜 안에 드는 6시간 자료 추가. 시간대는 1시간 자료만 사용. 자정을 걸친 6시간 구간 {crossing_buckets}개({crossing_amount:,}개 판매)는 요일별 배분 불가로 제외. 관측 없음은 판매량 0이 아님. 서머타임은 실제 경과 시간 기준.'}

    def summary(self, cache, item, city, days):
        # One state() call reuses each window summary across every consumer.
        key = (item, city, days)
        if key not in cache:
            cache[key] = self.history_summary(item, city, days)
        return cache[key]

    def volume_guide(self, cache, item, city):
        """Units this market sold per day over the last 24 h, 7 d and 28 d. The slowest pace
        is the basis and my part of it is the cycle_share setting, since other sellers take
        the rest: buying more than one day of my part to resell here is a caution, more than
        three days a risk, because unsold stock waits while other sellers undercut."""
        key = ("guide", item, city)
        if key not in cache:
            windows = []
            for days in (1, 7, 28):
                h = self.summary(cache, item, city, days)
                # A rate from a few observed hours is noise; never extrapolate below these.
                enough = h["amount"] is not None and h["covered_hours"] >= (6 if days == 1 else 24)
                windows.append({"days": days, "amount": h["amount"], "covered_hours": h["covered_hours"], "coverage": h["coverage"],
                                "per_day": h["amount"] / h["covered_hours"] * 24 if enough else None})
            per_day = min((w["per_day"] for w in windows if w["per_day"] is not None), default=None)
            mine = per_day * self.cfg["cycle_share"] if per_day is not None else None
            cache[key] = {"windows": windows, "per_day": per_day, "share": self.cfg["cycle_share"], "my_per_day": mine,
                          "caution_above": math.floor(mine) if mine is not None else None,
                          "danger_above": math.floor(mine * 3) if mine is not None else None}
        return cache[key]

    def candidates(self, books, cache=None):
        """Every viable scenario, item order first. state() trims per item for display."""
        cfg = self.cfg
        out = []
        cache = {} if cache is None else cache
        for item in cfg["items"]:
            rows = []
            for source in self.trade_cities(item):
                for dest in self.trade_cities(item):
                    rows.extend(self.route_scenarios(item, source, dest, books[item], cache))
            out.extend(sorted(rows, key=lambda r: (r["mode"] == "depth", -r["roi"])))
        return out

    def plan_items(self):
        return [i for i in self.cfg["items"] if i not in self.cfg["trial_items"]]

    def trade_cities(self, item=None):
        """Cities trading analysis may buy in or sell to; the rest stay visible as prices only.
        With an item, its item_cities come back in even when excluded_cities drops them."""
        extra = self.cfg.get("item_cities", {}).get(item, ())
        return [c for c in CITIES if c not in self.cfg["excluded_cities"] or c in extra]

    def route_scenarios(self, item, source, dest, books, cache):
        cfg = self.cfg
        src, dst = books[source], books[dest]
        transport = cfg["transport_per_unit"] if source != dest else 0
        setup = cfg["setup_fee"]
        # (mode, entry, exit, buy fee, extra sell fee, quotes actually used)
        scenarios = []
        if src["buy"]["fresh"] and dst["sell"]["fresh"]:
            buy_levels, sell_levels = src["buy"]["fresh_levels"], dst["sell"]["fresh_levels"]
            resting = [("best", buy_levels[0]["price"] + 1, sell_levels[0]["price"] - 1)]
            buy_ref, sell_ref = src["buy"]["analysis_depth"]["reference"], dst["sell"]["analysis_depth"]["reference"]
            if buy_ref and sell_ref:
                alternative = (buy_ref["price"] + 1, sell_ref["price"] - 1)
                if alternative != resting[0][1:]:
                    resting.append(("depth", *alternative))
            local_asks, local_bids = src["sell"]["fresh_levels"], dst["buy"]["fresh_levels"]
            for mode, entry, exit_price in resting:
                # A resting order priced through the opposite side would fill at once;
                # that trade is the "instant" model below, not a queue position.
                if (local_asks and entry >= local_asks[0]["price"]) or (local_bids and exit_price <= local_bids[0]["price"]):
                    continue
                scenarios.append((mode, entry, exit_price, setup, setup, (src["buy"], dst["sell"])))
        if source != dest and src["sell"]["fresh"]:
            ask = src["sell"]["fresh_levels"][0]["price"]
            if dst["buy"]["fresh"]:
                scenarios.append(("instant", ask, dst["buy"]["fresh_levels"][0]["price"], 0, 0, (src["sell"], dst["buy"])))
            if dst["sell"]["fresh"]:
                exit_price = dst["sell"]["fresh_levels"][0]["price"] - 1
                if not (dst["buy"]["fresh"] and exit_price <= dst["buy"]["fresh_levels"][0]["price"]):
                    scenarios.append(("instant_list", ask, exit_price, 0, setup, (src["sell"], dst["sell"])))
        rows = []
        for mode, entry, exit_price, buy_fee, sell_fee, (entry_book, exit_book) in scenarios:
            cost = entry * (1 + buy_fee)
            net = exit_price * (1 - cfg["sales_tax"] - sell_fee) - cost - transport
            roi = net / cost if cost else 0
            if net <= 0 or roi < cfg["minimum_margin"]:
                continue
            h = self.summary(cache, item, dest, 7)
            instant_buy, instant_sell = mode in ("instant", "instant_list"), mode == "instant"
            buy_ref = entry_book["fresh_levels"][0] if instant_buy else src["buy"]["analysis_depth"]["reference"]
            sell_ref = exit_book["fresh_levels"][0] if instant_sell else dst["sell"]["analysis_depth"]["reference"]
            buy_ahead = 0 if instant_buy else sum(r["amount"] for r in src["buy"]["fresh_levels"] if r["price"] >= entry)
            sell_ahead = 0 if instant_sell else sum(r["amount"] for r in dst["sell"]["fresh_levels"] if r["price"] <= exit_price)
            reasons = []
            if mode == "depth":
                reasons.append(f"물량 기준 조건부: 매수 앞 {buy_ahead:,}개 / 매도 앞 {sell_ahead:,}개 소화 필요")
            if instant_buy and buy_ref["amount"] <= cfg["small_quantity_max"]:
                reasons.append(f"최저 매도가 잔량 {buy_ref['amount']:,}개뿐 · 다음 가격은 더 비쌈")
            if instant_sell and sell_ref["amount"] <= cfg["small_quantity_max"]:
                reasons.append(f"최고 매수가 잔량 {sell_ref['amount']:,}개뿐 · 다음 가격은 더 낮음")
            if (not instant_buy and src["buy"]["analysis_depth"]["small_dominated"]) or (not instant_sell and dst["sell"]["analysis_depth"]["small_dominated"]):
                reasons.append("소량 호가가 절반 이상 · 최우선 호가만으로 시장 판단 주의")
            if not instant_buy and not instant_sell and (not buy_ref or not sell_ref):
                reasons.append("인근 가격대의 의미 있는 주문 물량 부족")
            if not cfg["fees_confirmed"]:
                reasons.append(FEES_UNCONFIRMED)
            if source != dest and not cfg["transport_confirmed"]:
                reasons.append(TRANSPORT_UNCONFIRMED)
            if h["coverage"] < 0.8:
                reasons.append("7일 판매 이력 부족")
            if h["stale"]:
                reasons.append("판매 이력의 최신 구간이 오래됨 · 다시 조회 필요")
            if not instant_sell and h["highest_bucket_average"] is not None and exit_price > h["highest_bucket_average"]:
                reasons.append("목표 매도가가 관측 7일 최고 구간 평균가보다 높음 · 해당 가격 체결 근거 미확인")
            if entry_book.get("mixed_freshness") or exit_book.get("mixed_freshness"):
                reasons.append("최신·이전 호가 혼재 · 이전 호가는 계산에서 제외")
            rows.append({"item": item, "buy_city": source, "sell_city": dest, "buy_price": entry, "sell_price": exit_price,
                "mode": mode, "buy_ahead": buy_ahead, "sell_ahead": sell_ahead,
                "buy_available": buy_ref["amount"] if instant_buy else None,
                "sell_available": sell_ref["amount"] if instant_sell else None,
                "buy_reference": buy_ref, "sell_reference": sell_ref,
                "net_per_unit": round(net, 2), "roi": roi, "status": MODE_LABELS[mode], "reasons": reasons,
                "buy_seen": entry_book["seen"], "sell_seen": exit_book["seen"], "history_coverage": h["coverage"],
                "history_weighted_average": h["weighted_average"], "history_highest_bucket_average": h["highest_bucket_average"],
                "history_amount": h["amount"], "history_stale": h["stale"],
                "quantity_recommendation": None, "priority": list(cfg["items"]).index(item) + 1})
        return rows

    def city_board(self, books, candidates):
        """Per city: routes worth carrying from here, and where this city is the best
        place to sell or buy right now. Fresh, multi-order observations only."""
        settings = {FEES_UNCONFIRMED, TRANSPORT_UNCONFIRMED}
        def route(c):
            return {k: c[k] for k in ("item", "buy_city", "sell_city", "buy_price", "sell_price", "net_per_unit", "roi", "mode", "status",
                                      "buy_available", "sell_available")} | {"warnings": [r for r in c["reasons"] if r not in settings]}
        quotes = {}
        for item in self.cfg["items"]:
            for side in ("buy", "sell"):
                quotes[item, side] = {city: books[item][city][side]["fresh_levels"][0] for city in self.trade_cities(item) if books[item][city][side]["fresh"]}
        board = []
        for city in CITIES:
            here = [item for item in self.cfg["items"] if city in self.trade_cities(item)]
            if not here:
                board.append({"city": city, "excluded": True, "routes": [], "sell_here": [], "buy_here": [], "missing": [],
                              "fresh_items": 0, "newest_seen": None})
                continue
            picks = {}
            for c in candidates:
                if c["buy_city"] != city or c["mode"] == "depth":
                    continue
                key = (c["item"], c["mode"] in ("instant", "instant_list"))
                if key not in picks or c["roi"] > picks[key]["roi"]:
                    picks[key] = c
            routes = []
            for item in here:
                main = max((picks[k] for k in ((item, False), (item, True)) if k in picks), key=lambda c: c["roi"], default=None)
                if main is None:
                    continue
                entry = route(main)
                other = picks.get((item, main["mode"] not in ("instant", "instant_list")))
                # The certain "바로" trade next to a higher-ROI order trade, so the choice stays visible.
                if other is not None and main["mode"] == "best":
                    entry["alternative"] = route(other)
                routes.append(entry)
            routes.sort(key=lambda r: -r["roi"])
            best_here = {"sell_here": [], "buy_here": []}
            for item in here:
                for side, key, better in (("buy", "sell_here", max), ("sell", "buy_here", min)):
                    pool = quotes[item, side]
                    if city not in pool or len(pool) < 2:
                        continue
                    price = pool[city]["price"]
                    if price != better(q["price"] for q in pool.values()):
                        continue
                    others = [q["price"] for c, q in pool.items() if c != city and q["price"] != price]
                    runner = better(others) if others else None
                    best_here[key].append({"item": item, "price": price, "amount": pool[city]["amount"], "compared": len(pool),
                                           "lead": abs(price - runner) if runner is not None else 0})
            missing = []
            for item in here:
                pair = books[item][city]
                if pair["buy"]["fresh"] and pair["sell"]["fresh"]:
                    continue
                if not pair["buy"]["levels"] and not pair["sell"]["levels"]:
                    reason = "미조회"
                elif pair["buy"].get("single_only") or pair["sell"].get("single_only"):
                    reason = "단일 주문만 수신"
                elif pair["buy"]["fresh"] or pair["sell"]["fresh"]:
                    reason = "한쪽 목록만 최신"
                else:
                    reason = "지난 자료"
                missing.append({"item": item, "reason": reason})
            seen = [books[item][city][side]["seen"] for item in here for side in ("buy", "sell")
                    if books[item][city][side]["fresh"]]
            board.append({"city": city, "routes": routes[:3], **best_here, "missing": missing,
                          "fresh_items": len(here) - len(missing), "newest_seen": max(seen, default=None)})
        return board

    def buy_under(self, books):
        """Per item, the units each trading city's sell list offers at or under a target price
        for instant buys: the buy_targets setting, else a suggestion that leaves minimum_margin
        after listing at the dearest lowest ask among the trading cities (sales tax and setup fee
        paid, transport added). Which units really sell at that ask is not known, so the 7-day
        average traded price there goes beside it. Lists are each market's last observation,
        fresh or not; a list of one order (a clicked order) does not count."""
        cfg, keep, out = self.cfg, 1 - self.cfg["sales_tax"] - self.cfg["setup_fee"], []
        known = lambda book: bool(book["levels"]) and not book["single_only"]
        for item in cfg["items"]:
            cities = self.trade_cities(item)
            lists = {(item, c): books[item][c]["sell"] for c in cities}
            asks = {c: lists[item, c]["levels"][0]["price"] for c in cities if known(lists[item, c])}
            sell_city = max(asks, key=asks.get, default=None)
            sell = asks.get(sell_city)
            history = self.history_rows(item, sell_city, 7) if sell_city else []
            traded = sum(r["amount"] for r in history)
            suggested = math.floor(sell * keep / (1 + cfg["minimum_margin"]) - cfg["transport_per_unit"]) if sell else None
            target = cfg["buy_targets"].get(item) or suggested
            rows = []
            for c in cities:
                book = lists[item, c]
                row = {"city": c, "seen": book["seen"], "fresh": book["fresh"], "known": known(book), "lowest": None,
                       "units": 0, "cost": 0, "average": None, "profit": None}
                if row["known"]:
                    under = [l for l in book["levels"] if target is not None and l["price"] <= target]
                    row.update(lowest=book["levels"][0]["price"], units=sum(l["amount"] for l in under),
                               cost=sum(l["price"] * l["amount"] for l in under))
                    if row["units"]:
                        row["average"] = row["cost"] / row["units"]
                        carry = cfg["transport_per_unit"] if c != sell_city else 0
                        row["profit"] = row["units"] * (sell * keep - carry) - row["cost"]
                rows.append(row)
            units = sum(r["units"] for r in rows)
            out.append({"item": item, "target": target, "chosen": cfg["buy_targets"].get(item), "suggested": suggested,
                        "sell_city": sell_city, "sell": sell, "sell_average": sum(r["silver"] for r in history) / traded if traded else None,
                        "rows": rows, "units": units, "cost": sum(r["cost"] for r in rows),
                        "profit": sum(r["profit"] for r in rows if r["profit"] is not None) if units and sell else None})
        return out

    def buyouts(self, books, cache):
        """Buying the cheapest sell orders lifts the floor to the next observed price. For each
        fresh sell list: silver and units per step, and what relisting the bought units one
        silver under the new floor would return. Targets are always prices actually seen."""
        cfg = self.cfg
        keep = 1 - cfg["sales_tax"] - cfg["setup_fee"]
        out = []
        for item in cfg["items"]:
            for city in self.trade_cities(item):
                book = books[item][city]["sell"]
                levels = book["fresh_levels"]
                if not book["fresh"] or len(levels) < 2:
                    continue
                # Never "buy out" the player's own listings.
                own = defaultdict(int)
                for r in cache.get("mine", {}).get((item, city, "sell"), []):
                    own[r["price"]] += r["remaining"]
                levels = [dict(l, amount=l["amount"] - own[l["price"]]) for l in levels if l["amount"] > own[l["price"]]]
                if len(levels) < 2:
                    continue
                steps, units, cost = [], 0, 0
                for bought, level in zip(levels, levels[1:BUYOUT_LEVELS + 1]):
                    units += bought["amount"]
                    cost += bought["price"] * bought["amount"]
                    profit = units * (level["price"] - 1) * keep - cost
                    steps.append({"units": units, "cost": cost, "average_cost": cost / units, "floor": level["price"], "top_price": bought["price"],
                                  "lift": level["price"] / levels[0]["price"] - 1, "relist": level["price"] - 1,
                                  "profit": round(profit, 2), "roi": profit / cost,
                                  "next_amount": level["amount"], "next_wall": bool(level.get("wall"))})
                # Dense lists never pay after fees; skip their history lookups.
                if not any(s["profit"] > 0 and s["roi"] >= cfg["minimum_margin"] for s in steps):
                    continue
                guide = self.volume_guide(cache, item, city)
                for step in steps:
                    step["risk"] = volume_risk(step["units"], guide)
                # The most profit among buyouts this market can absorb within three days of its slowest pace.
                viable = [s for s in steps if s["profit"] > 0 and s["roi"] >= cfg["minimum_margin"] and s["risk"] != "danger"
                          and s["cost"] <= cfg["trade_budget"]]
                if not viable:
                    continue
                best = max(viable, key=lambda s: s["profit"])
                h = self.summary(cache, item, city, 7)
                reasons = []
                if best["risk"] == "caution":
                    reasons.append(f"내 몫(하루 판매량의 {guide['share']:.0%}) {guide['my_per_day']:,.0f}개보다 많이 삼 · 다 팔려면 약 {best['units'] / guide['my_per_day']:.1f}일")
                if best["risk"] == "unknown":
                    reasons.append("24시간·7일·28일 판매 이력 부족 · 다 팔린다는 근거 없음")
                if h["highest_bucket_average"] is not None and best["relist"] > h["highest_bucket_average"]:
                    reasons.append("다시 올릴 가격이 7일 최고 구간 평균가보다 높음 · 이 가격에 팔린 근거 없음")
                if h["stale"]:
                    reasons.append("판매 이력의 최신 구간이 오래됨 · 다시 조회 필요")
                if book.get("mixed_freshness"):
                    reasons.append("최신·이전 호가 혼재 · 실제로는 더 많이 사야 할 수 있음")
                if not cfg["fees_confirmed"]:
                    reasons.append(FEES_UNCONFIRMED)
                out.append({"item": item, "city": city, "floor_now": levels[0]["price"], "best": best, "steps": steps[:8],
                            "visible_units": sum(r["amount"] for r in levels), "visible_levels": len(levels),
                            "history_average": h["weighted_average"], "history_high": h["highest_bucket_average"],
                            "reasons": reasons, "seen": book["seen"]})
        return sorted(out, key=lambda r: -r["best"]["profit"])

    def demand_outlook(self, cache, item, city):
        """Expected units sold here over the next 6 and 24 hours, from the last 28 days of
        hourly sales split into weekdays and weekends by local hour. Waiting trades are capped
        by it, and a coming stretch well below the usual pace (e.g. weekday night) is flagged
        so orders are not piled up into hours with few buyers. None without enough history."""
        key = ("outlook", item, city)
        if key in cache:
            return cache[key]
        zone = ZoneInfo(self.cfg["timezone"])
        now = self.clock()
        totals = {}
        for r in self.db.execute("SELECT stamp,amount FROM history WHERE item=? AND city=? AND hours=1 AND stamp>? AND stamp<=?",
                                 (item, city, (now - timedelta(days=28)).isoformat(), now.isoformat())):
            start = (parse_time(r["stamp"]) - timedelta(hours=1)).astimezone(zone)
            for k in ((start.weekday() >= 5, start.hour), (None, start.hour)):
                t = totals.setdefault(k, [0, 0])
                t[0] += r["amount"]; t[1] += 1
        rates = []
        for step in range(24):
            moment = (now + timedelta(hours=step)).astimezone(zone)
            exact, general = totals.get((moment.weekday() >= 5, moment.hour)), totals.get((None, moment.hour))
            # Two observations of this weekday/weekend hour, else any day at this hour.
            sample = exact if exact and exact[1] >= 2 else general
            rates.append((moment, sample[0] / sample[1] if sample else None))
        known = [r for _, r in rates if r is not None]
        if len(known) < 12:
            cache[key] = None
            return None
        usual = sum(known) / len(known)
        filled = [(m, r if r is not None else usual) for m, r in rates]
        next6, next24 = sum(r for _, r in filled[:6]), sum(r for _, r in filled)
        quiet = [m.hour for m, r in filled if usual and r < usual * 0.5]
        cache[key] = result = {"next6": next6, "next24": next24, "usual6": usual * 6,
                               "low_window": bool(usual) and next6 < usual * 6 * 0.6,
                               "weekend": filled[0][0].weekday() >= 5, "quiet_hours": quiet}
        return result

    def plan_route(self, c, books, cache):
        """Turn one candidate into a concrete order: how many units, at which prices,
        for how much silver. Instant trades walk both order books level by level;
        listing and order trades are capped by one day of the slowest recent sales."""
        cfg = self.cfg
        item, source, dest, mode = c["item"], c["buy_city"], c["sell_city"], c["mode"]
        transport = cfg["transport_per_unit"] if source != dest else 0
        asks = [[l["price"], l["amount"]] for l in books[item][source]["sell"]["fresh_levels"]]
        sell_guide = self.volume_guide(cache, item, dest)
        units = cost = revenue = 0
        buy_high = sell_low = None
        budget = cfg["trade_budget"]
        if mode == "instant":
            bids = [[l["price"], l["amount"]] for l in books[item][dest]["buy"]["fresh_levels"]]
            i = j = 0
            while i < len(asks) and j < len(bids):
                (ask, left_ask), (bid, left_bid) = asks[i], bids[j]
                net = bid * (1 - cfg["sales_tax"]) - ask - transport
                if net <= 0 or net / ask < cfg["minimum_margin"]:
                    break
                q = min(left_ask, left_bid, int((budget - cost) // ask))
                if q <= 0:
                    break
                units, cost, revenue = units + q, cost + ask * q, revenue + bid * (1 - cfg["sales_tax"]) * q
                buy_high, sell_low = ask, bid
                asks[i][1] -= q; bids[j][1] -= q
                i += asks[i][1] == 0; j += bids[j][1] == 0
        elif mode == "instant_list":
            cap = self.waiting_cap(cache, item, dest, sell_guide["caution_above"])
            if not cap:
                return None
            price = c["sell_price"]
            for ask, amount in asks:
                net = price * (1 - cfg["sales_tax"] - cfg["setup_fee"]) - ask - transport
                if net <= 0 or net / ask < cfg["minimum_margin"] or units >= cap:
                    break
                q = min(amount, cap - units, int((budget - cost) // ask))
                if q <= 0:
                    break
                units, cost, revenue = units + q, cost + ask * q, revenue + price * (1 - cfg["sales_tax"] - cfg["setup_fee"]) * q
                buy_high, sell_low = ask, price
        elif mode == "best":
            buy_guide = self.volume_guide(cache, item, source)
            # Both caps are already my part of each market's day (cycle_share).
            units = min(self.waiting_cap(cache, item, source, buy_guide["caution_above"], "buy"),
                        self.waiting_cap(cache, item, dest, sell_guide["caution_above"]),
                        int(budget // (c["buy_price"] * (1 + cfg["setup_fee"]))))
            cost = c["buy_price"] * (1 + cfg["setup_fee"]) * units
            revenue = cost + c["net_per_unit"] * units + transport * units
            buy_high, sell_low = c["buy_price"], c["sell_price"]
        else:
            return None
        if units <= 0:
            return None
        profit = revenue - cost - transport * units
        weight, certainty = CERTAINTY[mode]
        warnings = [r for r in c["reasons"] if r not in (FEES_UNCONFIRMED, TRANSPORT_UNCONFIRMED)]
        if any("최고 구간 평균가보다 높음" in r for r in warnings):
            weight *= 0.5
        for city in {source, dest} & DANGEROUS_ROUTES.keys():
            if source != dest:
                warnings.append(DANGEROUS_ROUTES[city])
                weight *= 0.8
        return {"kind": mode, "item": item, "buy_city": source, "sell_city": dest, "units": units, "cost": round(cost),
                "profit": round(profit), "roi": profit / cost, "buy_low": c["buy_price"], "buy_high": buy_high,
                "sell_high": c["sell_price"], "sell_low": sell_low, "certainty": certainty, "score": profit * weight,
                "sell_per_day": sell_guide["per_day"], "warnings": warnings}

    def waiting_cap(self, cache, item, city, caution, side="sell"):
        """Units a waiting trade may hold in this market: one day of my part of the slowest recent pace,
        and no more than the hourly profile expects to sell over the next 24 hours, minus what
        the player's own open orders on that side already wait for."""
        outlook = self.demand_outlook(cache, item, city)
        if not caution:
            return 0
        cap = min(caution, int(outlook["next24"])) if outlook else caution
        return max(0, cap - sum(r["remaining"] for r in cache.get("mine", {}).get((item, city, side), [])))

    def recommendations(self, books, candidates, buyouts, cache):
        """The three best trades now in each of two styles: settle at once (buy from sell
        orders, sell into buy orders) or wait for orders to fill. Within a style two picks never
        buy from the same sell list or sell the same item in the same city: they would empty
        each other's supply or compete for the same buyers."""
        plans = [plan for c in candidates if c["mode"] != "depth" for plan in [self.plan_route(c, books, cache)] if plan]
        for b in buyouts:
            best = b["best"]
            # Relisting needs buyers; without any sales history this is a guess, not a pick.
            if best["risk"] == "unknown":
                continue
            weight, certainty = CERTAINTY["buyout"]
            if any("최고 구간 평균가보다 높음" in r for r in b["reasons"]):
                weight *= 0.5
            plans.append({"kind": "buyout", "item": b["item"], "buy_city": b["city"], "sell_city": b["city"], "units": best["units"],
                          "cost": round(best["cost"]), "profit": round(best["profit"]), "roi": best["roi"], "buy_low": b["floor_now"],
                          "buy_high": best["top_price"], "sell_high": best["relist"], "sell_low": best["relist"], "certainty": certainty,
                          "score": best["profit"] * weight, "sell_per_day": self.volume_guide(cache, b["item"], b["city"])["per_day"],
                          "floor_after": best["floor"], "warnings": [r for r in b["reasons"] if r != FEES_UNCONFIRMED]})
        plans = [p for p in plans if p["item"] not in self.cfg["trial_items"]]
        heavy_days = self.cfg["inventory_heavy_days"]
        for plan in plans:
            # Stock already worth more than heavy_days of sales: picks that add stock buy only the
            # share that keeps it there (none at all from twice the limit, so the plan drops), and a
            # buyout, which must buy the whole sweep, ranks lower instead.
            stock = cache.get("inventory", {}).get(plan["item"])
            if plan["kind"] in STOCKING_KINDS and stock and stock["heavy"]:
                note = f"재고 {stock['days']:,.1f}일치(보유 {stock['held']:,}개 + 매수 주문 {stock['buying']:,}개 · 시장 하루 {stock['per_day']:,.0f}개 팔림)"
                if plan["kind"] == "buyout":
                    plan["score"] *= heavy_days / stock["days"]
                    plan["warnings"].append(note + " · 더 사기 전에 파는 쪽이 먼저라 순위를 낮췄음")
                else:
                    keep = max(0.0, 2 - stock["days"] / heavy_days)
                    units = int(plan["units"] * keep)
                    if units >= plan["units"]:
                        continue
                    if units < 1:
                        plan["dropped"] = True
                        continue
                    ratio = units / plan["units"]
                    plan["warnings"].append(note + f" · 매수 수량을 {plan['units']:,}개에서 {units:,}개로 줄였음")
                    plan["units"], plan["cost"], plan["profit"], plan["score"] = units, round(plan["cost"] * ratio), round(plan["profit"] * ratio), plan["score"] * ratio
        plans = [p for p in plans if not p.get("dropped")]
        for plan in plans:
            # Listing and buyout plans sell over the coming day, so today's usual pace matters;
            # an instant trade settles now and only gets the information.
            plan["group"] = "instant" if plan["kind"] in INSTANT_KINDS else "wait"
            plan["timing"] = timing = self.timing(cache, plan["item"], plan["sell_city"], books)
            plan["outlook"] = outlook = self.demand_outlook(cache, plan["item"], plan["sell_city"])
            if plan["group"] == "instant":
                continue
            if timing["sales_weekday"] is not None:
                plan["score"] *= min(1.2, max(0.8, timing["sales_weekday"]))
            if outlook and outlook["low_window"]:
                plan["score"] *= max(0.6, outlook["next6"] / outlook["usual6"]) if outlook["usual6"] else 0.6
                plan["warnings"].append(f"앞으로 6시간은 {'주말' if outlook['weekend'] else '평일'} 수요가 적은 시간대 · 예상 판매 {outlook['next6']:,.0f}개(평소 {outlook['usual6']:,.0f}개) · 주문을 한꺼번에 쌓지 마세요")
            for side, city, price, verb in (("sell", plan["sell_city"], plan["sell_high"], "매도"), ("buy", plan["buy_city"], plan["buy_low"], "매수")):
                if plan["kind"] in ("instant_list", "buyout") and side == "buy":
                    continue
                mine = cache.get("mine", {}).get((plan["item"], city, side), [])
                if not mine:
                    continue
                left = sum(r["remaining"] for r in mine)
                edge = min(r["price"] for r in mine) if side == "sell" else max(r["price"] for r in mine)
                worse = price < edge if side == "sell" else price > edge
                # Units per price, best price first: one total with one price read as if every unit sat there.
                at_price = defaultdict(int)
                for r in mine:
                    at_price[r["price"]] += r["remaining"]
                spread = " · ".join(f"{p:,.0f}에 {n:,}개" for p, n in sorted(at_price.items(), reverse=side == "buy"))
                plan["warnings"].append(f"내 기존 {verb} 주문 {left:,}개 남음({spread}) · 그만큼 수량을 줄였음"
                                        + (f" · {price:,.0f}에 새로 걸면 기존 주문이 뒤로 밀림" if worse else ""))
                if worse:
                    plan["score"] *= 0.7
            if outlook and plan["units"] > outlook["next24"]:
                plan["warnings"].append(f"앞으로 24시간 예상 판매 {outlook['next24']:,.0f}개보다 많이 삼 · 나눠서 올리세요")
        groups = {}
        for group in ("instant", "wait"):
            picks, buying, selling = [], set(), set()
            for plan in sorted((p for p in plans if p["group"] == group), key=lambda p: -p["score"]):
                source, dest = (plan["item"], plan["buy_city"]), (plan["item"], plan["sell_city"])
                if source in buying or dest in selling:
                    continue
                buying.add(source)
                selling.add(dest)
                picks.append(plan)
                if len(picks) == 3:
                    break
            groups[group] = picks
        return groups

    def log_picks(self, picks):
        """Keep each new set of top picks, so earlier advice stays readable after the
        15-minute freshness window closes or the next day. At most one entry per 5 minutes
        unless the picks change; 30 days are kept."""
        now = self.clock()
        digest = hashlib.sha256(json.dumps([(p["kind"], p["item"], p["buy_city"], p["sell_city"], p["units"]) for p in picks]).encode()).hexdigest()
        last = self.db.execute("SELECT created,digest FROM pick_log ORDER BY id DESC LIMIT 1").fetchone()
        if last and (last["digest"] == digest or now - parse_time(last["created"]) < timedelta(minutes=5)):
            return
        with self.db:
            self.db.execute("INSERT INTO pick_log(created,digest,payload) VALUES(?,?,?)", (now.isoformat(), digest, json.dumps(picks, ensure_ascii=False, allow_nan=False)))
            self.db.execute("DELETE FROM pick_log WHERE created<?", ((now - timedelta(days=30)).isoformat(),))

    def remember_results(self, kind, rows):
        """Keep the latest non-empty list of a kind (candidates, buyouts) and hand it back when
        the current one is empty, so the last advice stays readable after the 15-minute
        freshness window closes. seen is the last time that list was current (refreshed at most
        once a minute while it stays the same); None while the current list has rows."""
        now = self.clock()
        if rows:
            payload = json.dumps(rows, ensure_ascii=False, allow_nan=False, sort_keys=True)
            digest = hashlib.sha256(payload.encode()).hexdigest()
            last = self.db.execute("SELECT digest,seen FROM last_results WHERE kind=?", (kind,)).fetchone()
            if last is None or last["digest"] != digest:
                with self.db:
                    self.db.execute("""INSERT INTO last_results VALUES(?,?,?,?,?) ON CONFLICT(kind) DO UPDATE SET
                        created=excluded.created,seen=excluded.seen,digest=excluded.digest,payload=excluded.payload""",
                                    (kind, now.isoformat(), now.isoformat(), digest, payload))
            elif now - parse_time(last["seen"]) >= timedelta(minutes=1):
                with self.db:
                    self.db.execute("UPDATE last_results SET seen=? WHERE kind=?", (now.isoformat(), kind))
            return None
        last = self.db.execute("SELECT created,seen,payload FROM last_results WHERE kind=?", (kind,)).fetchone()
        return {"created": last["created"], "seen": last["seen"], "rows": json.loads(last["payload"])} if last else None

    def pick_history(self, limit=60):
        return [{"created": r["created"], "picks": json.loads(r["payload"])} for r in
                self.db.execute("SELECT created,payload FROM pick_log ORDER BY id DESC LIMIT ?", (limit,))]

    def state(self, item=None, city=None):
        with self.lock:
            with self.db:
                self.read_client_log()
            item = item if item in self.cfg["items"] else next(iter(self.cfg["items"]))
            city = city if city in CITIES else "1002"
            books = {i: {c: {s: self.book(i, c, s) for s in ("sell", "buy")} for c in CITIES} for i in self.cfg["items"]}
            sales = self.personal_sales(item, city)
            cache = {}
            mine_rows = self.my_orders(books)
            purchases = self.purchases(mine_rows)
            for r in mine_rows:
                cache.setdefault("mine", {}).setdefault((r["item"], r["city"], r["side"]), []).append(r)
            every = self.candidates(books, cache)
            board = self.city_board(books, every)
            buyouts = self.buyouts(books, cache)
            for card in board:
                card["buyout"] = next((b for b in buyouts if b["city"] == card["city"]), None)
            holdings = self.holdings(mine_rows, purchases, books)
            for item, b in holdings["best_listing"].items():
                # Days my part of that market needs to sell everything listed there, bought units included.
                mine = self.volume_guide(cache, item, b["city"])["my_per_day"]
                b["sell_days"] = (b["held"] + (b["sweep"] or {}).get("units", 0)) / mine if mine and b["held"] else None
            capital = self.capital(cache, mine_rows, holdings)
            cache["inventory"] = capital["items"]
            now_groups = self.recommendations(books, every, buyouts, cache)
            now = now_groups["instant"] + now_groups["wait"]
            if now:
                self.log_picks(now)
            history = self.pick_history()
            # Trim per item so a busy first item cannot push the others off the list.
            candidates, shown = [], defaultdict(int)
            for candidate in every:
                shown[candidate["item"]] += 1
                if shown[candidate["item"]] <= CANDIDATES_PER_ITEM:
                    candidates.append(candidate)
            for candidate in candidates:
                evidence = sales["by_market"].get(candidate["item"] + "/" + candidate["sell_city"])
                candidate["personal_sale_evidence"] = evidence
                if evidence:
                    candidate["reasons"].append(f"내 판매 메일 누적 최고 {evidence['highest_price']:,} / {evidence['highest_price_amount']:,}개 · 체결 시각 미확인, 현재 체결 보장 아님")
                stock = capital["items"].get(candidate["item"])
                if candidate["mode"] in STOCKING_KINDS + ("depth",) and stock and stock["heavy"]:
                    candidate["reasons"].append(f"재고 {stock['days']:,.1f}일치 보유 · 더 사기 전에 파는 쪽이 먼저")
            past_candidates, past_buyouts = self.remember_results("candidates", candidates), self.remember_results("buyouts", buyouts)
            # Buying-quantity guides for every market a displayed number depends on.
            routes = [*candidates, *(past_candidates or {}).get("rows", []),
                      *(r for card in board for r in (*card["routes"], *(x["alternative"] for x in card["routes"] if "alternative" in x)))]
            guides = {f"{i}/{c}": self.volume_guide(cache, i, c) for i, c in
                      {(item, city), *((r["item"], r["sell_city"]) for r in routes),
                       *((b["item"], b["city"]) for b in (*buyouts, *(past_buyouts or {}).get("rows", [])))}}
            last = self.db.execute("SELECT received,topic FROM packets ORDER BY id DESC LIMIT 1").fetchone()
            count = self.db.execute("SELECT COUNT(*) FROM packets").fetchone()[0]
            return {"build": BUILD, "personal_sales": sales, "personal_mails": self.personal_mails(), "capture": capture_status(self.root), "game_watch": watch_status(self.root), "ui_revision": getattr(self, "ui_revision", None), "auto_update": getattr(self, "auto_update", {"enabled": False}), "generated_at": self.clock().isoformat(), "server": "Europe (게임도 유럽 서버인지 확인)",
                "timezone": self.cfg["timezone"], "items": {i: name + (" · 테스트·보류" if i in self.cfg["trial_items"] else "") for i, name in self.cfg["items"].items()},
                "trial_items": self.cfg["trial_items"], "item_cities": self.cfg["item_cities"], "cities": CITIES,
                "selected_item": item, "selected_city": city, "books": books,
                "history": [self.summary(cache, item, city, d) for d in (1, 7, 28)], "patterns": self.patterns(item, city),
                "city_extremes": self.city_extremes(books), "candidates": candidates, "past_candidates": past_candidates, "past_buyouts": past_buyouts, "city_board": board,
                "candidate_notes": COMMON_NOTES, "buyouts": buyouts, "buyout_notes": BUYOUT_NOTES, "buy_under": self.buy_under(books), "now": now, "now_groups": now_groups, "pick_history": history, "outlook": self.demand_outlook(cache, item, city), "mail_diagnostics": self.mail_diagnostics(), "client_debug": self.client_debug(),
                "book_patterns": self.book_patterns(item, city), "studies": self.market_studies(cache), "my_orders": mine_rows, "purchases": purchases, "stale_listings": self.stale_listings(cache, books, mine_rows, purchases), "holdings": holdings, "capital": capital, "portfolio": self.portfolio(cache, books, capital, purchases), "timing": self.timing(cache, item, city, books), "volume_guides": guides, "packets": count, "last_packet": dict(last) if last else None,
                "settings": {k: self.cfg[k] for k in ("sales_tax", "setup_fee", "fees_confirmed", "minimum_margin", "near_high_fraction", "fresh_minutes", "transport_per_unit", "transport_confirmed", *SETTING_DEFAULTS)},
                "email": {"enabled": self.cfg["email"]["enabled"], "configured": bool(self.cfg["email"]["to"] and self.cfg["email"]["smtp_host"]), "times": self.cfg["email"]["times"]},
                "limits": ["개별 최고 체결가·가격별 체결량은 제공되지 않음", "호가는 마지막 조회 묶음의 일부 주문이며 미조회·소멸 주문을 체결로 간주하지 않음", "이력 timestamp는 구간 끝으로 해석 · 구간 경계 밖은 비례 배분하지 않음", "모바일·아이패드는 Windows 자동 수집 대상이 아님", "수집 클라이언트 수신 데이터에는 서버 식별자가 없어 유럽 서버 접속을 직접 확인해야 함"]}

    def export_csv(self):
        with self.lock:
            output = io.StringIO(newline="")
            writer = csv.writer(output)
            writer.writerow(["item", "city", "bucket_hours", "bucket_end_utc", "quantity", "total_silver", "bucket_average_price", "observed_at_utc"])
            for r in self.db.execute("SELECT * FROM history ORDER BY item,city,hours,stamp"):
                writer.writerow([r["item"], CITIES.get(r["city"], r["city"]), r["hours"], r["stamp"], r["amount"], r["silver"], r["silver"] / r["amount"] if r["amount"] else "", r["seen"]])
            return output.getvalue().encode("utf-8-sig")


def build_report(store, slot):
    """The scheduled report message, or None when mail is off. Call under the store lock."""
    cfg = store.cfg["email"]
    if not cfg["enabled"] or not cfg["to"] or not cfg["from"] or not cfg["smtp_host"]:
        return None
    if not os.environ.get(cfg["password_env"]):
        raise ValueError("SMTP password environment variable is not configured")
    state = store.state()
    lines = ["알비온 유럽 시장 · " + slot, "모든 가격은 조회 당시 일부 주문. 체결·판매 완료를 보장하지 않습니다. " + " · ".join(COMMON_NOTES), ""]
    if not state["candidates"]:
        lines.append("검토 가능한 새 매매 후보 없음. 미수집·오래된 자료일 수 있습니다.")
    for r in sorted(state["candidates"], key=lambda r: -r["roi"])[:12]:
        queue = "" if r["mode"] in ("instant", "instant_list") else f"; 앞선 매수 {r['buy_ahead']:,}개 / 매도 {r['sell_ahead']:,}개"
        lines.append(f"{state['items'][r['item']]} [{r['status']}]: {CITIES[r['buy_city']]} {r['buy_price']:,.0f} → {CITIES[r['sell_city']]} {r['sell_price']:,.0f}{queue}; 개당 예상 {r['net_per_unit']:,.0f} ({r['roi'] * 100:.1f}%); {', '.join(r['reasons'])}")
    for item in store.cfg["items"]:
        for city in CITIES:
            summaries = [store.history_summary(item, city, d) for d in (1, 7)]
            if not any(h["buckets"] for h in summaries):
                continue
            lines.extend(["", store.cfg["items"][item] + " / " + CITIES[city]])
            for h in summaries:
                lines.append(f"{h['days'] * 24}시간: 관측 판매량 {h['amount']}, 범위 {h['covered_hours']}/{h['requested_hours']}시간, 가중평균 {h['weighted_average']}, 최고 구간평균 {h['highest_bucket_average']}, 고가 구간 전체 판매량 {h['near_high_amount']}")
                for r in h["top_buckets"][:3]:
                    zone = ZoneInfo(store.cfg["timezone"])
                    a = parse_time(r["start"]).astimezone(zone).isoformat()
                    b = parse_time(r["end"]).astimezone(zone).isoformat()
                    lines.append(f"  {a} ~ {b}: 평균 {r['avg']:,.2f}, 구간 판매량 {r['amount']}")
            patterns = store.patterns(item, city)
            for title, values in (("요일별", patterns["weekdays"]), ("시간대별", patterns["hourly"])):
                for r in values:
                    if not r['observed_hours']:
                        continue
                    label = WEEKDAYS[r["key"]] if title == "요일별" else f"{r['key']:02d}시"
                    lines.append(f"  {title} {label}: 관측 총 {r['total']}개 / {r['observed_hours']}시간, 시간당 {r['per_observed_hour']:.1f}개")
    msg = EmailMessage()
    msg["Subject"] = "알비온 시장 분석 · " + slot
    msg["From"], msg["To"] = cfg["from"], cfg["to"]
    msg.set_content("\n".join(lines))
    return msg


def send_report(cfg, msg):
    """Network only; never call while holding the store lock."""
    password = os.environ.get(cfg["password_env"])
    if not password:
        raise ValueError("SMTP password environment variable is not configured")
    with smtplib.SMTP(cfg["smtp_host"], int(cfg["smtp_port"]), timeout=25) as smtp:
        smtp.ehlo()
        smtp.starttls()
        smtp.ehlo()
        smtp.login(cfg["username"] or cfg["from"], password)
        smtp.send_message(msg)


def email_report(store, slot):
    # Kept for a receiver started with an older auto_update.py; that one calls this name.
    msg = build_report(store, slot)
    if msg is None:
        return False
    send_report(store.cfg["email"], msg)
    return True


def handler_class(store, token):
    class Handler(BaseHTTPRequestHandler):
        server_version = "AlbionLocal/1.0"

        def respond(self, data, status=200, content_type="application/json; charset=utf-8"):
            if not isinstance(data, bytes):
                data = json.dumps(data, ensure_ascii=False, allow_nan=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(data)

        def valid_host(self):
            return self.headers.get("Host") in (f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}")

        def do_GET(self):
            if not self.valid_host():
                return self.respond({"error": "Invalid host"}, 403)
            parts = urlparse(self.path)
            if parts.path == "/api/state":
                params = parse_qs(parts.query)
                result = store.state(params.get("item", [None])[0], params.get("city", [None])[0])
                result["token"] = token
                return self.respond(result)
            if parts.path == "/api/health":
                return self.respond({"app": "AlbionLocal", "ready": True, "auto_update": getattr(store, "auto_update", {"enabled": False}), "capture_verified": bool(store.db.execute("SELECT 1 FROM packets WHERE topic='marketorders.ingest' LIMIT 1").fetchone())})
            if parts.path == "/api/export.csv":
                return self.respond(store.export_csv(), content_type="text/csv; charset=utf-8")
            if parts.path in ("/", "/app.js", "/app-v2.js", "/style.css", "/v2.css"):
                name = {"/": "index.html", "/app.js": "app.js", "/app-v2.js": "app-v2.js", "/style.css": "style.css", "/v2.css": "v2.css"}[parts.path]
                kind = {"/": "text/html", "/app.js": "application/javascript", "/app-v2.js": "application/javascript", "/style.css": "text/css", "/v2.css": "text/css"}[parts.path]
                return self.respond((store.root / "web" / name).read_bytes(), content_type=kind + "; charset=utf-8")
            self.respond({"error": "Not found"}, 404)

        def do_POST(self):
            if not self.valid_host():
                return self.respond({"error": "Invalid host"}, 403)
            # No CORS: only native capture client or the same-origin local app may write.
            origin = self.headers.get("Origin")
            allowed = (f"http://127.0.0.1:{self.server.server_port}", f"http://localhost:{self.server.server_port}")
            if origin and origin not in allowed:
                return self.respond({"error": "Invalid origin"}, 403)
            try:
                size = int(self.headers.get("Content-Length", 0))
                if not 0 < size <= 4_000_000:
                    return self.respond({"error": "Invalid body size"}, 413)
                path = urlparse(self.path).path
                if path.startswith("/private/europe/"):
                    store.note_private(path.rsplit("/", 1)[-1])
                payload = json.loads(self.rfile.read(size))
                if path.startswith("/private/europe/"):
                    # Private uploaders also receive public orders. Ignore those here
                    # so the two destinations never double-ingest order snapshots.
                    # marketorders.mine exists only here: the fork's own-order lists.
                    topic = path.rsplit("/", 1)[-1]
                    if topic == "marketnotifications":
                        result = store.ingest_sale(payload)
                    elif topic == "marketorders.mine":
                        result = store.ingest_my_orders(payload)
                    elif topic == "marketorders.finished":
                        result = store.ingest_finished(payload)
                    elif topic == "holdings.containers":
                        result = store.ingest_holdings(payload)
                    elif topic == "marketorders.instantbuy":
                        result = store.ingest_instant_buy(payload)
                    else:
                        result = {"accepted": 0, "ignored": True}
                elif path.startswith("/ingest/europe/"):
                    result = store.ingest(path.rsplit("/", 1)[-1], payload)
                else:
                    if self.headers.get("X-Local-Token") != token:
                        return self.respond({"error": "Local UI token required"}, 403)
                    if path == "/api/settings":
                        store.save_config(payload)
                        result = {"saved": True}
                    elif path == "/api/my-orders":
                        if payload.get("action") == "close":
                            store.close_my_order(payload["id"])
                            result = {"closed": True}
                        else:
                            result = {"added": store.add_my_order(payload)}
                    elif path == "/api/purchases":
                        if payload.get("action") == "remove":
                            store.remove_purchase(payload["id"])
                            result = {"removed": True}
                        else:
                            result = {"added": store.add_purchase(payload)}
                    elif path == "/api/bank":
                        store.set_bank(payload)
                        result = {"saved": True}
                    elif path == "/api/bank-city":
                        store.set_bank_city(payload)
                        result = {"saved": True}
                    elif path == "/api/reset":
                        store.reset_book(payload["item"], payload["city"])
                        result = {"reset": True}
                    else:
                        return self.respond({"error": "Not found"}, 404)
                self.respond(result)
            except (ValueError, KeyError, TypeError, OverflowError) as exc:
                self.respond({"error": str(exc)}, 400)
            except Exception:
                logging.exception("Receiver failed")
                self.respond({"error": "Internal error; see data/server.log"}, 500)

        def log_request(self, code="-", size="-"):
            # The page polls every 5 s and the watcher checks health every 3 s;
            # logging each success would grow server.log without bound.
            if isinstance(code, int) and code < 400:
                return
            super().log_request(code, size)

        def log_message(self, fmt, *args):
            logging.info(fmt, *args)
    return Handler


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int)
    parser.add_argument("--data-dir", type=Path, default=ROOT)
    args = parser.parse_args()
    from auto_update import run
    run(ROOT, args.data_dir, args.port)


if __name__ == "__main__":
    main()
