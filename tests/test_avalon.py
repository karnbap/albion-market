"""Roads of Avalon read from game-file shaped XML. Synthetic data only; nothing is downloaded."""
import json
import os
from pathlib import Path
import shutil
import tempfile
import time
import unittest
from unittest.mock import patch

import avalon
from market import Store

ROOT = Path(__file__).resolve().parents[1]

WORLD = b"""<world><clusters>
<cluster id="TNL-901" file="TNL-901_RDS_RO_AUTO_T4_AVA_AVA.cluster.xml" displayname="Testa-Ixtum" type="TUNNEL_ROYAL">
  <distribution><resource name="HIDE" tier="4" count="16"/><resource name="HIDE" tier="5" count="8"/>
    <resource name="ORE" tier="6" count="3"/><resource name="WOOD" tier="1" count="2"/></distribution>
  <minimapmarkers><marker type="Hide" pos="0 0"/><marker type="dungeon_solo" pos="1 1"/><marker type="dungeon_group" pos="2 2"/></minimapmarkers>
</cluster>
<cluster id="TNL-902" file="TNL-902_RDS_RO_AUTO_T6_AVA_AVA.cluster.xml" displayname="Bexo-Olum" type="TUNNEL_LOW"/>
<cluster id="TNL-901"><tunnelexit path="instanceslot_01/tunnelexit_01"/></cluster>
<cluster id="0001" file="0001_CTY_SW_AUTO_T1_NON.cluster.xml" displayname="Town" type="CITY"/>
</clusters></world>"""

LAYOUT = b"""<cluster>
  <templateinstance id="instanceslot_00" ref="M_ROADS_RES_HideOre_01"><activelayer id="Layer_02"/></templateinstance>
  <templateinstance id="instanceslot_01" ref="S_ROADS_PVE_SOLO_01"><activelayer id="Layer_01"/></templateinstance>
  <templateinstance id="instanceslot_02" ref="S_ROADS_Portal_01"><activelayer id="Layer_03"/></templateinstance>
</cluster>"""

EMPTY = b"<cluster/>"

RES = b"""<template editorBoundsMin="-80 -80" editorBoundsMax="80 80"><tiles>
  <layergroup name="Resource">
    <layer id="Layer_01" name="RES_Group_T6"><tile name="ROADS_T6_ORE"><minimapmarker type="Ore"/></tile></layer>
    <layer id="Layer_02" name="RES_Group_T4"><tile name="ROADS_T4_ORE"><minimapmarker type="Hide"/></tile></layer>
  </layergroup>
</tiles></template>"""

PVE = b"""<template><tiles>
  <tile name="SpawnPoint_LOOTCHEST_SMALL_AVALON_ROAD_T4"/>
  <layer id="Layer_01" name="PVE_Solo_T4">
    <tile name="SpawnPoint_LOOTCHEST_SMALL_AVALON_ROAD_T4"/><tile name="SpawnPoint_LOOTCHEST_MEDIUM_AVALON_ROAD_VETERAN_T4"/>
  </layer>
  <layer id="Layer_02" name="PVE_Raid_T6"><tile name="SpawnPoint_LOOTCHEST_MEDIUM_AVALON_ROAD_ELITE_T6"/></layer>
</tiles></template>"""

FILES = {avalon.WORLD: WORLD,
         "cluster/TNL-901_RDS_RO_AUTO_T4_AVA_AVA.cluster.xml": LAYOUT,
         "cluster/TNL-902_RDS_RO_AUTO_T6_AVA_AVA.cluster.xml": EMPTY,
         "templates/NONE/M_ROADS_RES_HideOre_01.template.xml": RES,
         "templates/NONE/S_ROADS_PVE_SOLO_01.template.xml": PVE}


def fetch(path):
    return FILES[path]


class BuildTests(unittest.TestCase):
    def test_a_road_reads_tier_reach_resources_areas_chests_and_dungeons(self):
        roads = avalon.build(fetch)
        self.assertEqual([r["name"] for r in roads], ["Bexo-Olum", "Testa-Ixtum"])  # roads only, by name
        r = roads[1]
        self.assertEqual((r["id"], r["tier"], r["type"], r["reach"]), ("TNL-901", 4, "TUNNEL_ROYAL", ["blue", "yellow"]))
        # T1 entries are left out; counts come from world.xml's own totals.
        self.assertEqual(r["resources"], {"HIDE": {"4": 16, "5": 8}, "ORE": {"6": 3}})
        # Only the active layer's marker names the area; M_ is the large size.
        self.assertEqual(r["areas"], [{"size": "large", "main": "HIDE"}])
        # The loose tile and the active layer count; the inactive raid layer does not.
        self.assertEqual(r["chests"], {"green": 2, "blue": 1, "gold": 0})
        self.assertEqual(r["dungeons"], {"solo": 1, "group": 1, "elite": 0})

    def test_inner_roads_have_no_outer_exit(self):
        r = avalon.build(fetch)[0]
        self.assertEqual((r["tier"], r["reach"], r["resources"], r["areas"]), (6, [], {}, []))

    def test_only_resource_and_camp_templates_are_fetched(self):
        asked = []
        avalon.build(lambda p: asked.append(p) or FILES[p])
        self.assertNotIn("templates/NONE/S_ROADS_Portal_01.template.xml", asked)

    def test_world_without_roads_is_an_error(self):
        with self.assertRaises(ValueError):
            avalon.build(lambda p: b"<world><cluster id='1' file='a' displayname='x' type='CITY'/></world>")


class RoadsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "data").mkdir()
        self.roads = avalon.Roads()

    def tearDown(self):
        self.tmp.cleanup()

    def test_refresh_writes_the_file_that_a_later_start_reads(self):
        self.roads.refresh(self.root / "data/avalon_roads.json", fetch=fetch)
        self.assertEqual(len(self.roads.roads), 2)
        later = avalon.Roads()
        later.ensure(self.root)
        self.assertEqual(later.roads, self.roads.roads)

    def test_nothing_is_downloaded_without_permission(self):
        with patch("threading.Thread") as thread:
            self.roads.ensure(self.root)
        thread.assert_not_called()
        self.assertIsNone(self.roads.roads)

    def test_old_file_stays_in_use_while_it_is_refreshed(self):
        path = self.root / "data/avalon_roads.json"
        path.write_text(json.dumps({"roads": [{"name": "Old"}]}), encoding="utf-8")
        old = time.time() - avalon.REFRESH_SECONDS - 60
        os.utime(path, (old, old))
        with patch("threading.Thread") as thread:
            self.roads.ensure(self.root, download=True)
        thread.assert_called_once()
        self.assertEqual(self.roads.roads, [{"name": "Old"}])

    def test_a_failed_download_waits_before_trying_again(self):
        self.roads.refresh(self.root / "data/avalon_roads.json", fetch=lambda p: (_ for _ in ()).throw(OSError("offline")))
        self.assertIn("offline", self.roads.error)
        with patch("threading.Thread") as thread:
            self.roads.ensure(self.root, download=True)
        thread.assert_not_called()


class StoreTests(unittest.TestCase):
    def test_answer_says_whether_the_list_is_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shutil.copy(ROOT / "config.example.json", root / "config.json")
            store = Store(root)
            try:
                with patch.object(avalon, "roads", avalon.Roads()):
                    self.assertEqual(store.avalon_roads(), {"ready": False, "loading": False, "error": "", "roads": []})
                    avalon.roads.roads = [{"name": "Testa-Ixtum"}]
                    self.assertTrue(store.avalon_roads()["ready"])
            finally:
                store.db.close()


if __name__ == "__main__":
    unittest.main()
