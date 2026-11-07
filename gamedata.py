"""The game's item data this program needs, from ao-bin-dumps items.xml, kept in data/item_data.json.

Per item id: w weight (kg), f fasttravelfactor (1 when missing), and for items crafted from resources v, the item
value at each enchantment level (.0 first), and up, the materials each enchantment step takes (.0 -> .1 first) as
[resource id, count] pairs. Item value is the sum of the crafting resources' item values times their counts; for
T4_2H_CLAYMORE that gives 512, the wiki's reroll cost example (wiki.albiononline.com/wiki/Quality, read 2026-11-07).
Read-only: it never talks to the game.
"""
from __future__ import annotations
import json
import logging
from pathlib import Path
import threading
import time
import urllib.request
import xml.etree.ElementTree as ET

ITEMS_URL = "https://raw.githubusercontent.com/ao-data/ao-bin-dumps/master/items.xml"
# After a failed download, wait this long before trying again.
RETRY_SECONDS = 1800


def parse(source):
    """{item id: {"w", "f", and when crafted from resources "v" and "up"}} from an items.xml file or stream."""
    items = [e for e in ET.parse(source).getroot() if e.get("uniquename")]
    values = {}
    for e in items:
        try:
            values[e.get("uniquename")] = float(e.get("itemvalue")) if e.get("itemvalue") else None
        except ValueError:
            pass

    def value(requirements):
        if requirements is None:
            return None
        parts = [(values.get(r.get("uniquename")), r.get("count")) for r in requirements.findall("craftresource")]
        if not parts or any(v is None or not (c or "").isdigit() for v, c in parts):
            return None
        return sum(v * int(c) for v, c in parts)

    out = {}
    for e in items:
        try:
            row = {"w": float(e.get("weight")), "f": float(e.get("fasttravelfactor") or 1)}
        except (TypeError, ValueError):
            continue
        base = value(e.find("craftingrequirements"))
        steps = e.findall("enchantments/enchantment")
        if base is not None and not e.get("itemvalue"):
            row["v"], row["up"] = [base], []
            for step in sorted(steps, key=lambda s: int(s.get("enchantmentlevel") or 0)):
                need = step.find("upgraderequirements")
                worth = value(step.find("craftingrequirements"))
                if worth is None or need is None:
                    break
                row["v"].append(worth)
                row["up"].append([[r.get("uniquename"), int(r.get("count"))] for r in need.findall("upgraderesource")])
        out[e.get("uniquename")] = row
    return out


class Catalog:
    def __init__(self):
        self.items, self.loading, self.error, self.lock, self.failed_at = None, False, "", threading.Lock(), None

    def ensure(self, directory, download=False):
        """Loads data/item_data.json; with download, fetches it in the background when it is missing
        (again RETRY_SECONDS after a failure)."""
        path = Path(directory) / "data" / "item_data.json"
        with self.lock:
            if self.items is not None or self.loading:
                return
            if path.exists():
                try:
                    self.items = json.loads(path.read_text(encoding="utf-8"))
                    return
                except ValueError:
                    logging.warning("Item data unreadable; downloading again")
            if not download or (self.failed_at and time.monotonic() - self.failed_at < RETRY_SECONDS):
                return
            self.loading = True
        threading.Thread(target=self.download, args=(path,), daemon=True).start()

    def download(self, path):
        try:
            with urllib.request.urlopen(ITEMS_URL, timeout=180) as response:
                items = parse(response)
            if not items:
                raise ValueError("no items in the item data")
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(items), encoding="utf-8")
            tmp.replace(path)
            self.items, self.error, self.failed_at = items, "", None
        except Exception as exc:
            self.error, self.failed_at = str(exc)[:160], time.monotonic()
            logging.warning("Item data download failed: %s", exc)
        finally:
            self.loading = False

    def get(self, item):
        """The entry of an item id. Enchanted resources are items of their own (T4_ORE_LEVEL1); an id with
        @n (T4_ORE_LEVEL1@1, T6_2H_CLAYMORE@2) looks up the entry before the @."""
        if not self.items:
            return None
        return self.items.get(item) or self.items.get(item.split("@")[0])


catalog = Catalog()
