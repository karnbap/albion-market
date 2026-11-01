"""Any-item price lookup: a local store of public market orders and item names.

Orders come from two places, both read-only: the AODP public live stream (orders every data-client
user uploads, received over NATS) and markets this PC's collector opens. Nothing personal is sent
anywhere: the stream login is AODP's published public account, and the public price API is only
asked for item ids. Orders live in their own database (data/lookup.sqlite3) and are kept a day.
"""
from __future__ import annotations

import gzip
import json
import logging
import socket
import sqlite3
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

# AODP developer page (albion-online-data.com/developer): one host, a port per region, public login.
NATS_HOST = "nats.albion-online-data.com"
NATS_PORTS = {"europe": 34222}
NATS_LOGIN = {"user": "public", "pass": "thenewalbiondata"}
NATS_SUBJECT = "marketorders.ingest"
API = "https://europe.albion-online-data.com/api/v2/stats"
NAMES_URL = "https://raw.githubusercontent.com/ao-data/ao-bin-dumps/master/formatted/items.json"
KEEP = timedelta(hours=24)
# About 200 bytes an order on disk, so the cap keeps the file near 200 MB.
MAX_ROWS = 1_000_000
PUBLIC_TTL = 300
QUALITIES = (1, 2, 3, 4, 5)


def utcnow():
    return datetime.now(timezone.utc)


def connect(directory):
    db = sqlite3.connect(Path(directory) / "data" / "lookup.sqlite3", check_same_thread=False, timeout=10)
    db.executescript("""
    PRAGMA journal_mode=WAL;
    CREATE TABLE IF NOT EXISTS orders(id INTEGER PRIMARY KEY, item TEXT NOT NULL, location TEXT NOT NULL,
      quality INTEGER NOT NULL, side TEXT NOT NULL, price INTEGER NOT NULL, amount INTEGER NOT NULL,
      expires TEXT NOT NULL, seen TEXT NOT NULL, source TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS orders_item ON orders(item, quality);
    CREATE INDEX IF NOT EXISTS orders_seen ON orders(seen);
    CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
    """)
    return db


def order_rows(orders, source, seen):
    """Market orders in the AODP upload shape -> rows for the orders table; malformed ones are skipped."""
    rows = []
    for o in orders if isinstance(orders, list) else []:
        try:
            side = {"offer": "sell", "request": "buy"}[o["AuctionType"]]
            price, amount = int(o["UnitPriceSilver"]) // 10_000, int(o["Amount"])
            rows.append((int(o["Id"]), str(o["ItemTypeId"]), str(o["LocationId"]), int(o.get("QualityLevel") or 1),
                         side, price, amount, str(o.get("Expires", "")), seen, source))
        except (KeyError, TypeError, ValueError):
            continue
    return [r for r in rows if r[5] > 0 and r[6] >= 0]


def add_orders(db, orders, source, seen=None):
    rows = order_rows(orders, source, seen or utcnow().isoformat())
    with db:
        db.executemany("""INSERT INTO orders VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
          price=excluded.price, amount=excluded.amount, expires=excluded.expires, seen=excluded.seen,
          source=excluded.source""", rows)
    return len(rows)


def purge(db, now=None):
    now = now or utcnow()
    with db:
        db.execute("DELETE FROM orders WHERE seen<? OR (expires<>'' AND expires<?)",
                   ((now - KEEP).isoformat(), now.replace(tzinfo=None).isoformat()))
        extra = db.execute("SELECT COUNT(*) FROM orders").fetchone()[0] - MAX_ROWS
        if extra > 0:
            db.execute("DELETE FROM orders WHERE id IN (SELECT id FROM orders ORDER BY seen LIMIT ?)", (extra,))


def markets(db, item, quality, city_of, now=None):
    """Per city: cheapest sell and dearest buy with their amounts, total units, newest sighting."""
    now = now or utcnow()
    out = {}
    rows = db.execute("SELECT location, side, price, amount, seen, source FROM orders WHERE item=? AND quality=? AND seen>=? AND amount>0",
                      (item, quality, (now - KEEP).isoformat())).fetchall()
    for loc, side, price, amount, seen, source in rows:
        city = city_of(loc)
        if city is None:
            continue
        m = out.setdefault(city, {}).setdefault(side, {"levels": {}, "seen": "", "units": 0, "mine": False})
        m["levels"][price] = m["levels"].get(price, 0) + amount
        m["units"] += amount
        m["seen"] = max(m["seen"], seen)
        m["mine"] = m["mine"] or source == "me"
    for sides in out.values():
        for side, m in sides.items():
            prices = sorted(m.pop("levels").items(), reverse=side == "buy")
            m["price"], m["amount"] = prices[0]
            m["levels"] = [{"price": p, "amount": a} for p, a in prices[:5]]
    return out


def set_meta(db, **values):
    with db:
        db.executemany("INSERT INTO meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                       [(k, json.dumps(v)) for k, v in values.items()])


def get_meta(db):
    return {k: json.loads(v) for k, v in db.execute("SELECT key, value FROM meta")}


def fetch_json(url, timeout=30):
    req = urllib.request.Request(url, headers={"Accept-Encoding": "gzip", "User-Agent": "albion-market-local"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            data = gzip.decompress(data)
    return json.loads(data)


class Stream(threading.Thread):
    """Listens to the public order stream and writes what it hears into the lookup database."""

    def __init__(self, directory, region="europe"):
        super().__init__(daemon=True, name="aodp-stream")
        self.directory, self.port = Path(directory), NATS_PORTS[region]
        self.stopped = threading.Event()

    def run(self):
        db, backoff = connect(self.directory), 5
        purge(db)
        while not self.stopped.is_set():
            try:
                self.listen(db)
                backoff = 5
            except Exception as exc:
                logging.warning("Public order stream: %s; retrying in %ss", exc, backoff)
                set_meta(db, stream_error=str(exc)[:160], stream_connected=False)
            self.stopped.wait(backoff)
            backoff = min(backoff * 2, 300)

    def listen(self, db):
        with socket.create_connection((NATS_HOST, self.port), timeout=30) as s:
            s.settimeout(5)
            s.sendall(b"CONNECT " + json.dumps({**NATS_LOGIN, "verbose": False, "pedantic": False}).encode()
                      + f"\r\nSUB {NATS_SUBJECT} 1\r\n".encode())
            set_meta(db, stream_connected=True, stream_since=utcnow().isoformat(), stream_error="")
            buf, batch, flushed, purged, quiet = b"", [], time.monotonic(), time.monotonic(), time.monotonic()
            while not self.stopped.is_set():
                try:
                    chunk = s.recv(65536)
                    if not chunk:
                        raise ConnectionError("stream closed")
                    buf += chunk
                    quiet = time.monotonic()
                except socket.timeout:
                    if time.monotonic() - quiet > 120:
                        raise ConnectionError("no data for 2 minutes")
                buf = self.parse(buf, batch, s)
                if batch and time.monotonic() - flushed > 2:
                    count = add_orders(db, batch, "stream")
                    batch.clear()
                    flushed = time.monotonic()
                    meta = get_meta(db)
                    set_meta(db, stream_last=utcnow().isoformat(), stream_count=meta.get("stream_count", 0) + count)
                if time.monotonic() - purged > 3600:
                    purge(db)
                    purged = time.monotonic()

    @staticmethod
    def parse(buf, batch, sock):
        while True:
            end = buf.find(b"\r\n")
            if end < 0:
                return buf
            line = buf[:end]
            if line.startswith(b"MSG "):
                size = int(line.split()[-1])
                if len(buf) < end + 2 + size + 2:
                    return buf
                body, buf = buf[end + 2:end + 2 + size], buf[end + 2 + size + 2:]
                try:
                    batch.extend(json.loads(body).get("Orders") or [])
                except (ValueError, AttributeError):
                    pass
                continue
            buf = buf[end + 2:]
            if line.startswith(b"PING"):
                sock.sendall(b"PONG\r\n")
            elif line.startswith(b"-ERR"):
                raise ConnectionError(line.decode(errors="replace")[:120])


_stream = None


def start_stream(directory, region="europe"):
    """One listener per process; later calls (a new code revision) reuse it."""
    global _stream
    if region in NATS_PORTS and (_stream is None or not _stream.is_alive()):
        _stream = Stream(directory, region)
        _stream.start()
    return _stream


class Catalog:
    """Item ids with Korean and English names, from the public ao-bin-dumps item list."""

    def __init__(self):
        self.items, self.loading, self.error, self.lock = None, False, "", threading.Lock()

    def ensure(self, directory):
        path = Path(directory) / "data" / "item_names.json"
        with self.lock:
            if self.items is not None or self.loading:
                return
            if path.exists():
                self.items = json.loads(path.read_text(encoding="utf-8"))
                return
            self.loading = True
        threading.Thread(target=self.download, args=(path,), daemon=True).start()

    def download(self, path):
        try:
            items = [{"id": x["UniqueName"], "ko": (x.get("LocalizedNames") or {}).get("KO-KR", ""),
                      "en": (x.get("LocalizedNames") or {}).get("EN-US", "")}
                     for x in fetch_json(NAMES_URL, timeout=120) if x.get("LocalizedNames")]
            path.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
            self.items, self.error = items, ""
        except Exception as exc:
            self.error = str(exc)[:160]
            logging.warning("Item names download failed: %s", exc)
        finally:
            self.loading = False

    def search(self, query, limit=30):
        words = [w for w in query.lower().split() if w]
        if not words or self.items is None:
            return []
        found, order = {}, []
        for x in self.items:
            base, _, enchant = x["id"].partition("@")
            hay = f"{x['ko']} {x['en']} {x['id']}".lower()
            if all(w in hay for w in words):
                if base not in found:
                    found[base] = {"id": base, "ko": x["ko"], "en": x["en"], "enchants": [0]}
                    order.append(base)
                if enchant.isdigit():
                    found[base]["enchants"].append(int(enchant))
        # Exact id or name hits first; then each item family together, tiers in order (T4, T5, ...).
        def key(b):
            tier, _, family = b.partition("_")
            return (query.lower() not in (b.lower(), found[b]["ko"].lower()), family if tier[1:].isdigit() else b,
                    int(tier[1:]) if tier[:1] == "T" and tier[1:].isdigit() else 0)
        order.sort(key=key)
        return [found[b] for b in order[:limit]]

    def name(self, item):
        base = item.partition("@")[0]
        for x in self.items or []:
            if x["id"] == base:
                return x["ko"] or x["en"]
        return item


catalog = Catalog()


class PublicPrices:
    """Public price API answers, fetched in the background and kept five minutes per item and quality."""

    def __init__(self):
        self.cache, self.running, self.lock = {}, set(), threading.Lock()

    def get(self, item, quality, city_names):
        key = (item, quality)
        with self.lock:
            hit = self.cache.get(key)
            fresh = hit and time.time() - hit["fetched"] < PUBLIC_TTL
            if not fresh and key not in self.running:
                self.running.add(key)
                threading.Thread(target=self.fetch, args=(key, city_names), daemon=True).start()
        return hit, key in self.running

    def fetch(self, key, city_names):
        item, quality = key
        where = urllib.parse.quote(",".join(city_names))
        result = {"fetched": time.time(), "prices": [], "history": [], "error": ""}
        try:
            result["prices"] = fetch_json(f"{API}/prices/{item}.json?qualities={quality}&locations={where}")
            # History for one quality is often missing for gear, so every quality comes back and the caller picks.
            result["history"] = fetch_json(f"{API}/history/{item}.json?time-scale=24&locations={where}")
        except Exception as exc:
            result["error"] = str(exc)[:160]
        with self.lock:
            self.cache[key] = result
            self.running.discard(key)


public = PublicPrices()

