"""Local-only AODP raw-ingest receiver. No game input, no public-feed uploads."""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import logging
import math
import os
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
PORTALS = {"1301": "1002", "2301": "2004", "3301": "3008", "4301": "4002", "301": "7"}
WEEKDAYS = ["월", "화", "수", "목", "금", "토", "일"]
DEPTH_DEFAULTS = {"small_quantity_max": 9, "large_quantity_min": 100, "wall_quantity_min": 500,
                  "wall_multiple": 3.0, "depth_price_band": 0.20}
BUILD = "20261025.8"
# Trade scenarios. "바로" crosses an existing order now; "주문" waits in the queue.
MODE_LABELS = {"instant": "바로 매입 · 바로 판매", "instant_list": "바로 매입 · 주문 판매",
               "best": "주문 매입 · 주문 판매", "depth": "주문 매입 · 주문 판매 · 물량 기준 조건부"}
# Caveats true of every candidate; shown once instead of on every row.
COMMON_NOTES = ["주문 목록 일부 · 전체 깊이 미확인", "내 재고·기존 주문 미연결 · 수량 추천 보류",
                "바로 매입·판매 가격은 조회 뒤 다른 사람이 먼저 거래하면 바뀜", "주문 매입·판매는 체결 대기"]
FEES_UNCONFIRMED, TRANSPORT_UNCONFIRMED = "수수료 설정 미확인", "운송비 설정 미확인"
CANDIDATES_PER_ITEM = 15


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
        expected = os.path.normcase(str((Path(root)/'vendor/albiondata-client.exe').resolve()))
        alive = code.value == 259 and os.path.normcase(image.value) == expected
        return {'running':alive,'status':'수집기 실행 중' if alive else '수집기 꺼짐'}
    finally:
        kernel.CloseHandle(handle)


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
        for key, value in DEPTH_DEFAULTS.items():
            self.cfg.setdefault(key, value)
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
        CREATE INDEX IF NOT EXISTS session_lookup ON sessions(item,city,side,id);
        CREATE INDEX IF NOT EXISTS history_lookup ON history(item,city,stamp);
        CREATE INDEX IF NOT EXISTS packet_topic_received ON packets(topic,received);
        CREATE TABLE IF NOT EXISTS observed_pages(session INTEGER NOT NULL REFERENCES sessions(id),
          ids TEXT NOT NULL, seen TEXT NOT NULL, PRIMARY KEY(session,ids));
        """)
        self.db.commit()
        last = self.db.execute("SELECT payload FROM packets WHERE topic='marketorders.ingest' ORDER BY id DESC LIMIT 1").fetchone()
        orders = json.loads(last[0]).get("Orders", []) if last else []
        self.active_market = (orders[0]["ItemTypeId"], location(orders[0]["LocationId"])) if orders else None

    def save_config(self, values):
        with self.lock:
            cfg = dict(self.cfg)
            for k in ("timezone", "sales_tax", "setup_fee", "fees_confirmed", "minimum_margin", "near_high_fraction", "fresh_minutes", "transport_per_unit", "transport_confirmed", *DEPTH_DEFAULTS):
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
                rows = payload.get("Orders")
                if not isinstance(rows, list):
                    raise ValueError("Orders array required")
                filtered = [r for r in rows if isinstance(r, dict) and r.get("ItemTypeId") in self.cfg["items"]]
                normalized = []
                for r in filtered:
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
                sessions, groups = {}, {}
                for row in normalized:
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
                        self.db.execute("INSERT INTO orders VALUES(?,?,?,?,?,?) ON CONFLICT(session,order_id) DO UPDATE SET price=excluded.price,amount=excluded.amount,seen=excluded.seen,expires=excluded.expires", (sid, str(r["Id"]), price, amount, received, expiry))
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

    def ingest_sale(self, payload):
        """A completed sell-mail summary, not an execution timestamp or market volume.
        This topic already expresses UnitPriceSilver in silver, unlike live orders.
        Never use the client's hard-coded TotalAfterTaxes (3%) as actual proceeds.
        """
        if not isinstance(payload, dict):
            raise ValueError("JSON object required")
        if payload.get("NotificationType") != "SalesNotification":
            return {"accepted": 0, "ignored": True, "reason": "completed sell mail only"}
        row = payload.get("Notification")
        if not isinstance(row, dict):
            raise ValueError("Notification object required")
        item, city = row.get("ItemTypeId"), location(row.get("LocationId", ""))
        if item not in self.cfg["items"] or city not in CITIES:
            return {"accepted": 0, "ignored": True, "reason": "untracked item or city"}
        def positive_integer(key):
            value = row.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, str)):
                raise ValueError("Invalid sale " + key)
            value = int(value)
            if not 0 < value <= 2**63-1:
                raise ValueError("Invalid sale " + key)
            return value
        mail_id = str(positive_integer("Id"))
        price, amount = positive_integer("UnitPriceSilver"), positive_integer("Amount")
        character = str(payload.get("CharacterId") or payload.get("CharacterName") or "").strip()
        identity = hashlib.sha256(("Europe|" + character).encode()).hexdigest()
        seen = self.clock().isoformat()
        with self.lock, self.db:
            previous = self.db.execute("SELECT * FROM personal_sales WHERE identity=? AND mail_id=?", (identity, mail_id)).fetchone()
            if previous:
                conflict = bool(previous["conflict"]) or any(previous[k] != v for k,v in (("item",item),("city",city),("price",price),("amount",amount)))
                self.db.execute("UPDATE personal_sales SET last_seen=?,conflict=? WHERE identity=? AND mail_id=?", (seen,int(conflict),identity,mail_id))
                return {"accepted": 0, "duplicate": True, "conflict": conflict}
            self.db.execute("INSERT INTO personal_sales VALUES(?,?,?,?,?,?,?,?,?,?,0)",
                (identity,mail_id,item,city,price,amount,seen,seen,str(row.get("Expires") or ""),int(bool(character))))
        return {"accepted": 1, "received_at": seen}

    def personal_sales(self, item, city):
        with self.lock:
            rows = [dict(r) for r in self.db.execute("SELECT item,city,price,amount,first_seen,last_seen,identity_known FROM personal_sales WHERE conflict=0 ORDER BY first_seen DESC,mail_id DESC")]
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
            return {"by_market":markets,"selected":selected[:100],"selected_count":len(selected),"mail_count":len(rows),
                "conflicts":self.db.execute("SELECT COUNT(*) FROM personal_sales WHERE conflict=1").fetchone()[0],
                "unknown_identity":sum(not r["identity_known"] for r in rows),
                "note":"판매 완료 메일의 누적 기록 · 시각은 처음 읽은 때이며 체결 시각은 미확인. 시장 거래량·요일·시간대 집계에 더하지 않음. 같은 메일은 중복 집계하지 않음."}

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

    def candidates(self, books):
        """Every viable scenario, item order first. state() trims per item for display."""
        cfg = self.cfg
        out = []
        histories = {}
        for item in cfg["items"]:
            rows = []
            for source in CITIES:
                for dest in CITIES:
                    rows.extend(self.route_scenarios(item, source, dest, books[item], histories))
            out.extend(sorted(rows, key=lambda r: (r["mode"] == "depth", -r["roi"])))
        return out

    def route_scenarios(self, item, source, dest, books, histories):
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
            # The 7-day destination summary is the same for every source city; build it once.
            if (item, dest) not in histories:
                histories[item, dest] = self.history_summary(item, dest, 7)
            h = histories[item, dest]
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
                quotes[item, side] = {city: books[item][city][side]["fresh_levels"][0] for city in CITIES if books[item][city][side]["fresh"]}
        board = []
        for city in CITIES:
            picks = {}
            for c in candidates:
                if c["buy_city"] != city or c["mode"] == "depth":
                    continue
                key = (c["item"], c["mode"] in ("instant", "instant_list"))
                if key not in picks or c["roi"] > picks[key]["roi"]:
                    picks[key] = c
            routes = []
            for item in self.cfg["items"]:
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
            for item in self.cfg["items"]:
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
            for item in self.cfg["items"]:
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
            seen = [books[item][city][side]["seen"] for item in self.cfg["items"] for side in ("buy", "sell")
                    if books[item][city][side]["fresh"]]
            board.append({"city": city, "routes": routes[:3], **best_here, "missing": missing,
                          "fresh_items": len(self.cfg["items"]) - len(missing), "newest_seen": max(seen, default=None)})
        return board

    def state(self, item=None, city=None):
        with self.lock:
            item = item if item in self.cfg["items"] else next(iter(self.cfg["items"]))
            city = city if city in CITIES else "1002"
            books = {i: {c: {s: self.book(i, c, s) for s in ("sell", "buy")} for c in CITIES} for i in self.cfg["items"]}
            sales = self.personal_sales(item, city)
            every = self.candidates(books)
            board = self.city_board(books, every)
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
            last = self.db.execute("SELECT received,topic FROM packets ORDER BY id DESC LIMIT 1").fetchone()
            count = self.db.execute("SELECT COUNT(*) FROM packets").fetchone()[0]
            return {"build": BUILD, "personal_sales": sales, "capture": capture_status(self.root), "game_watch": watch_status(self.root), "ui_revision": getattr(self, "ui_revision", None), "auto_update": getattr(self, "auto_update", {"enabled": False}), "generated_at": self.clock().isoformat(), "server": "Europe (게임도 유럽 서버인지 확인)",
                "timezone": self.cfg["timezone"], "items": self.cfg["items"], "cities": CITIES,
                "selected_item": item, "selected_city": city, "books": books,
                "history": [self.history_summary(item, city, d) for d in (1, 7, 28)], "patterns": self.patterns(item, city),
                "city_extremes": self.city_extremes(books), "candidates": candidates, "city_board": board,
                "candidate_notes": COMMON_NOTES, "packets": count, "last_packet": dict(last) if last else None,
                "settings": {k: self.cfg[k] for k in ("sales_tax", "setup_fee", "fees_confirmed", "minimum_margin", "near_high_fraction", "fresh_minutes", "transport_per_unit", "transport_confirmed", *DEPTH_DEFAULTS)},
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
                payload = json.loads(self.rfile.read(size))
                path = urlparse(self.path).path
                if path.startswith("/private/europe/"):
                    # Private uploaders also receive public orders. Ignore those here
                    # so the two destinations never double-ingest order snapshots.
                    result = store.ingest_sale(payload) if path == "/private/europe/marketnotifications" else {"accepted":0,"ignored":True}
                elif path.startswith("/ingest/europe/"):
                    result = store.ingest(path.rsplit("/", 1)[-1], payload)
                else:
                    if self.headers.get("X-Local-Token") != token:
                        return self.respond({"error": "Local UI token required"}, 403)
                    if path == "/api/settings":
                        store.save_config(payload)
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
