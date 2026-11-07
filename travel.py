"""Teleport (fast travel) cost of carrying items between cities. Read-only: it never talks to the game.

The game charges per item stack: 150 silver x weight (kg) x the item's fasttravelfactor x amount x a server-wide
multiplier, rounded up, times the route distance. Weight and fasttravelfactor come from the game's item data
(ao-bin-dumps items.xml; a missing factor is 1, as for equipment and souls). The formula and distances are from
Albion Free Market's reverse-engineered article (checked 2026-11-07), not official, so the multiplier is a setting the
player confirms against the in-game travel planner.
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
SILVER_PER_KG = 150
# After a failed download, wait this long before trying again.
RETRY_SECONDS = 1800
# The royal city ring, in order: neighbours are distance 1, any other pair 2.
# Thetford, Fort Sterling, Lymhurst, Bridgewatch, Martlock.
RING = ("7", "4002", "1002", "2004", "3008")
BRECILIEN = "5003"


def distance(source, dest):
    """Route distance between two city ids, or None when the travel planner has no such route (Caerleon)."""
    if source == dest:
        return 0
    if BRECILIEN in (source, dest):
        return 2 if {source, dest} - {BRECILIEN} <= set(RING) else None
    if source not in RING or dest not in RING:
        return None
    gap = abs(RING.index(source) - RING.index(dest))
    return 1 if gap in (1, len(RING) - 1) else 2


def parse(source):
    """{item id: [weight, factor]} for every item with a weight in an items.xml file or stream."""
    out = {}
    for _, element in ET.iterparse(source):
        name, weight = element.get("uniquename"), element.get("weight")
        if name and weight is not None:
            try:
                out[name] = [float(weight), float(element.get("fasttravelfactor") or 1)]
            except ValueError:
                pass
        # Only top-level items carry a weight; drop finished subtrees to keep memory flat.
        if element.tag != "items":
            element.clear()
    return out


class Catalog:
    def __init__(self):
        self.items, self.loading, self.error, self.lock, self.failed_at = None, False, "", threading.Lock(), None

    def ensure(self, directory, download=False):
        """Loads data/item_travel.json; with download, fetches it in the background when it is missing
        (again RETRY_SECONDS after a failure)."""
        path = Path(directory) / "data" / "item_travel.json"
        with self.lock:
            if self.items is not None or self.loading:
                return
            if path.exists():
                try:
                    self.items = json.loads(path.read_text(encoding="utf-8"))
                    return
                except ValueError:
                    logging.warning("Item travel data unreadable; downloading again")
            if not download or (self.failed_at and time.monotonic() - self.failed_at < RETRY_SECONDS):
                return
            self.loading = True
        threading.Thread(target=self.download, args=(path,), daemon=True).start()

    def download(self, path):
        try:
            with urllib.request.urlopen(ITEMS_URL, timeout=180) as response:
                items = parse(response)
            if not items:
                raise ValueError("no item weights in the item data")
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(items), encoding="utf-8")
            tmp.replace(path)
            self.items, self.error, self.failed_at = items, "", None
        except Exception as exc:
            self.error, self.failed_at = str(exc)[:160], time.monotonic()
            logging.warning("Item travel data download failed: %s", exc)
        finally:
            self.loading = False

    def get(self, item):
        """[weight, factor] of an item id. Enchanted resources are items of their own (T4_ORE_LEVEL1); an id with
        @n (T4_ORE_LEVEL1@1, T6_2H_CLAYMORE@2) looks up the entry before the @."""
        if not self.items:
            return None
        return self.items.get(item) or self.items.get(item.split("@")[0])


catalog = Catalog()


def unit_cost(item, source, dest, multiplier, items=catalog):
    """Silver per unit to teleport an item from one city to another, before the per-stack rounding up
    (under one silver per stack). None when the item's weight or the route is unknown."""
    d, data = distance(source, dest), items.get(item)
    if d is None or data is None:
        return None
    weight, factor = data
    return SILVER_PER_KG * weight * factor * multiplier * d
