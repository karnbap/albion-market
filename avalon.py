"""Roads of Avalon: what each road holds, from the official game files (ao-bin-dumps), kept in data/avalon_roads.json.

Per road: name, tier, road type, the outer zones its portals can open to, gathering resources by tier, the
resource areas (small or large, and the main resource), chests by colour and dungeon entrances.

How it is read (checked on 2026-11-08 against all 400 roads: our totals equal world.xml's own <distribution>,
<mobcounts> and <minimapmarkers>, and equal avalontracker.app, which builds from the same dump):
- cluster/world.xml: <cluster type="TUNNEL_*" displayname file> with <distribution><resource name tier count>
  (resource nodes plus resource mobs) and <minimapmarkers><marker type="dungeon_solo|dungeon_group|dungeon_elite">.
- Tier: the _T4_/_T6_/_T8_ token of the cluster file name (it equals the road's active atmosphere layer).
- The road's cluster file lists <templateinstance ref> with <activelayer id>; a template's tiles count when they
  sit in an active layer or outside every layer. S_ROADS_RES_* areas are 80x80 and M_ROADS_RES_* 160x160
  (editorBounds), holding 2-4 times as much; the minimap marker of the area's tiles names its main resource.
- Chests: SpawnPoint_LOOTCHEST_(SMALL|MEDIUM)_AVALON_ROAD_[VETERAN_|ELITE_]T<n> tiles; plain is green, VETERAN blue,
  ELITE gold (the journal's "Green/Blue/Yellow Chests" names). Rarity is rolled when a chest spawns.
- Outer zones (tunnelsystem.xml): only TUNNEL_ROYAL opens to blue and yellow zones, TUNNEL_ROYAL_RED to red zones,
  TUNNEL_BLACK_* to black zones; every other road type connects only to other roads. Portals are random and close
  after 12-24 hours, so this says what can open, never what is open now.
Not used: the road-name suffix rule (-tum hide, -lum ore, ...); on these files it matches no better than chance.
Read-only: it never talks to the game.
"""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor
import json
import logging
from pathlib import Path
import re
import threading
import time
import urllib.request
import xml.etree.ElementTree as ET

DUMPS = "https://raw.githubusercontent.com/ao-data/ao-bin-dumps/master/"
WORLD = "cluster/world.xml"
# After a failed download, wait this long before trying again; refresh after a game patch at most this often.
RETRY_SECONDS = 1800
REFRESH_SECONDS = 14 * 86400
# The outer zone colours each road type's portals can open to (tunnelsystem.xml exit presets).
REACH = {"TUNNEL_ROYAL": ["blue", "yellow"], "TUNNEL_ROYAL_RED": ["red"],
         "TUNNEL_BLACK_LOW": ["black"], "TUNNEL_BLACK_MEDIUM": ["black"], "TUNNEL_BLACK_HIGH": ["black"]}
RESOURCES = {"HIDE": "HIDE", "ORE": "ORE", "FIBER": "FIBER", "WOOD": "WOOD", "ROCK": "ROCK"}
MARKER = {"Hide": "HIDE", "Ore": "ORE", "Fiber": "FIBER", "Wood": "WOOD", "Stone": "ROCK"}
CHEST = re.compile(r"^SpawnPoint_LOOTCHEST_(SMALL|MEDIUM)_AVALON_ROAD_(?:(VETERAN|ELITE)_)?T\d$")
AREA = re.compile(r"^([SM])_ROADS_RES_")


def counted_tiles(template, active):
    """The tiles of a template that a road turns on: those in its active layers and those outside every layer."""
    tiles = []

    def walk(node, layer):
        for child in node:
            if child.tag == "layer":
                walk(child, child.get("id"))
            elif child.tag == "tile":
                if layer is None or layer in active:
                    tiles.append(child)
            else:
                walk(child, layer)
    found = template.find("tiles")
    if found is not None:
        walk(found, None)
    return tiles


def road(cluster, layout, templates):
    """One road's entry from its world.xml <cluster>, its cluster file root and {ref: template root}."""
    tier = int(re.search(r"_T(\d)_", cluster.get("file")).group(1))
    resources = {}
    for r in cluster.iter("resource"):
        kind, level = RESOURCES.get(r.get("name")), int(r.get("tier") or 0)
        # T1 entries (a little wood, Wolpertinger hide on deep roads) are left out: nothing to gather there.
        if kind and level >= 2 and int(r.get("count") or 0) > 0:
            resources.setdefault(kind, {})[str(level)] = int(r.get("count"))
    marks = cluster.find("minimapmarkers")
    dungeons = {"solo": 0, "group": 0, "elite": 0}
    for m in (marks if marks is not None else []):
        kind = (m.get("type") or "").removeprefix("dungeon_")
        if kind in dungeons and (m.get("type") or "").startswith("dungeon_"):
            dungeons[kind] += 1
    areas, chests = [], {"green": 0, "blue": 0, "gold": 0}
    for inst in layout.iter("templateinstance"):
        template = templates.get(inst.get("ref"))
        if template is None:
            continue
        tiles = counted_tiles(template, {a.get("id") for a in inst.findall("activelayer")})
        size = AREA.match(inst.get("ref"))
        if size:
            main = next((MARKER[m.get("type")] for t in tiles for m in t.iter("minimapmarker") if m.get("type") in MARKER), None)
            if main:
                areas.append({"size": "large" if size.group(1) == "M" else "small", "main": main})
        for t in tiles:
            chest = CHEST.match(t.get("name") or "")
            if chest:
                chests[{None: "green", "VETERAN": "blue", "ELITE": "gold"}[chest.group(2)]] += 1
    areas.sort(key=lambda a: (a["size"] != "large", a["main"]))
    return {"id": cluster.get("id"), "name": cluster.get("displayname"), "type": cluster.get("type"), "tier": tier,
            "reach": REACH.get(cluster.get("type"), []), "resources": resources, "areas": areas,
            "chests": chests, "dungeons": dungeons}


def needed(ref):
    """Templates that hold what a road entry counts: resource areas and PvE camps (chests)."""
    return bool(re.match(r"^[SM]_ROADS_(RES|PVE)_", ref or ""))


def build(fetch):
    """[road entries] from fetch(path in the dump) -> bytes, name order."""
    world = ET.fromstring(fetch(WORLD))
    clusters = [c for c in world.iter("cluster")
                if (c.get("type") or "").startswith("TUNNEL") and c.get("file") and c.get("displayname")]
    if not clusters:
        raise ValueError("no Avalon roads in world.xml")
    files = sorted({c.get("file") for c in clusters})
    with ThreadPoolExecutor(8) as pool:
        layouts = dict(zip(files, pool.map(lambda f: ET.fromstring(fetch("cluster/" + f)), files)))
        refs = sorted({i.get("ref") for root in layouts.values() for i in root.iter("templateinstance") if needed(i.get("ref"))})
        templates = dict(zip(refs, pool.map(lambda r: ET.fromstring(fetch("templates/NONE/" + r + ".template.xml")), refs)))
    return sorted((road(c, layouts[c.get("file")], templates) for c in clusters), key=lambda r: r["name"].casefold())


def download(path):
    with urllib.request.urlopen(DUMPS + path, timeout=180) as response:
        return response.read()


class Roads:
    def __init__(self):
        self.roads, self.loading, self.error, self.lock, self.failed_at = None, False, "", threading.Lock(), None

    def ensure(self, directory, download=False):
        """Loads data/avalon_roads.json; with download, builds it in the background when it is missing or
        older than REFRESH_SECONDS (again RETRY_SECONDS after a failure). An old file stays in use meanwhile."""
        path = Path(directory) / "data" / "avalon_roads.json"
        with self.lock:
            if self.loading:
                return
            if self.roads is None and path.exists():
                try:
                    self.roads = json.loads(path.read_text(encoding="utf-8"))["roads"]
                except (ValueError, KeyError, TypeError):
                    logging.warning("Avalon road data unreadable; building it again")
            fresh = path.exists() and time.time() - path.stat().st_mtime < REFRESH_SECONDS and self.roads is not None
            if fresh or not download or (self.failed_at and time.monotonic() - self.failed_at < RETRY_SECONDS):
                return
            self.loading = True
        threading.Thread(target=self.refresh, args=(path,), daemon=True).start()

    def refresh(self, path, fetch=download):
        try:
            roads = build(fetch)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"built": time.strftime("%Y-%m-%d"), "roads": roads}, ensure_ascii=False), encoding="utf-8")
            tmp.replace(path)
            self.roads, self.error, self.failed_at = roads, "", None
        except Exception as exc:
            self.error, self.failed_at = str(exc)[:160], time.monotonic()
            logging.warning("Avalon road data download failed: %s", exc)
        finally:
            self.loading = False


roads = Roads()
