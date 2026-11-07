"""The game item data parse and its cache: weights, travel factors, item values and enchant materials. Synthetic data only."""
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import gamedata

# Shaped like ao-bin-dumps items.xml: resources carry their own itemvalue; equipment is valued from its recipe.
XML = b"""<?xml version="1.0"?>
<items>
  <simpleitem uniquename="T4_TESTBAR" tier="4" weight="0.5" itemvalue="16" />
  <simpleitem uniquename="T4_TESTBAR_LEVEL1" tier="4" enchantmentlevel="1" weight="0.5" fasttravelfactor="4" itemvalue="32">
    <craftingrequirements><craftresource uniquename="T4_TESTBAR" count="1" /></craftingrequirements>
  </simpleitem>
  <simpleitem uniquename="T4_TESTCLOTH" tier="4" weight="0.5" itemvalue="16" />
  <simpleitem uniquename="T4_TESTCLOTH_LEVEL1" tier="4" weight="0.5" itemvalue="32" />
  <simpleitem uniquename="T8_TESTSOUL" tier="8" weight="1">
    <craftingrequirements><craftresource uniquename="T7_TESTSOUL" count="5" /></craftingrequirements>
  </simpleitem>
  <weapon uniquename="T4_TESTSWORD" tier="4" weight="7.6">
    <craftingrequirements silver="0">
      <craftresource uniquename="T4_TESTBAR" count="20" />
      <craftresource uniquename="T4_TESTCLOTH" count="12" />
    </craftingrequirements>
    <enchantments>
      <enchantment enchantmentlevel="1">
        <craftingrequirements>
          <craftresource uniquename="T4_TESTBAR_LEVEL1" enchantmentlevel="1" count="20" />
          <craftresource uniquename="T4_TESTCLOTH_LEVEL1" enchantmentlevel="1" count="12" />
        </craftingrequirements>
        <upgraderequirements><upgraderesource uniquename="T4_RUNE" count="384" /></upgraderequirements>
      </enchantment>
      <enchantment enchantmentlevel="2">
        <craftingrequirements><craftresource uniquename="T4_UNPRICED" count="20" /></craftingrequirements>
        <upgraderequirements><upgraderesource uniquename="T4_SOUL" count="384" /></upgraderequirements>
      </enchantment>
    </enchantments>
  </weapon>
  <simpleitem uniquename="T9_BROKEN" weight="heavy" />
</items>"""


class ParseTests(unittest.TestCase):
    def setUp(self):
        self.items = gamedata.parse(io.BytesIO(XML))

    def test_weight_and_factor_with_factor_one_when_missing(self):
        self.assertEqual(self.items["T8_TESTSOUL"], {"w": 1.0, "f": 1.0})
        self.assertEqual(self.items["T4_TESTBAR_LEVEL1"], {"w": 0.5, "f": 4.0})
        self.assertNotIn("T9_BROKEN", self.items)

    def test_equipment_value_is_its_recipe_and_each_step_needs_its_materials(self):
        sword = self.items["T4_TESTSWORD"]
        self.assertEqual(sword["v"], [512, 1024])  # 20x16 + 12x16 = 512, the wiki's T4 two-hander example
        self.assertEqual(sword["up"], [[["T4_RUNE", 384]]])  # .2 stops: a recipe resource has no value

    def test_resources_keep_their_own_value_and_no_upgrade_path(self):
        self.assertNotIn("v", self.items["T4_TESTBAR_LEVEL1"])
        self.assertNotIn("v", self.items["T8_TESTSOUL"])


class CatalogTests(unittest.TestCase):
    def test_enchanted_id_uses_the_entry_before_the_at(self):
        catalog = gamedata.Catalog()
        catalog.items = gamedata.parse(io.BytesIO(XML))
        self.assertEqual(catalog.get("T4_TESTSWORD@1")["v"], [512, 1024])
        self.assertEqual(catalog.get("T4_TESTBAR_LEVEL1@1"), {"w": 0.5, "f": 4.0})
        self.assertIsNone(catalog.get("T5_UNKNOWN"))

    def test_ensure_reads_the_saved_file_and_never_downloads_unless_asked(self):
        with tempfile.TemporaryDirectory() as tmp:
            catalog = gamedata.Catalog()
            with patch.object(catalog, "download") as download:
                catalog.ensure(tmp)
                self.assertIsNone(catalog.items)
                download.assert_not_called()
            (Path(tmp) / "data").mkdir()
            (Path(tmp) / "data/item_data.json").write_text(json.dumps({"T8_TESTSOUL": {"w": 1, "f": 1}}), encoding="utf-8")
            catalog.ensure(tmp)
            self.assertEqual(catalog.get("T8_TESTSOUL"), {"w": 1, "f": 1})

    def test_a_failed_download_is_tried_again_only_after_the_wait(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "data").mkdir()
            catalog, clock = gamedata.Catalog(), [1000.0]
            with patch("gamedata.time.monotonic", lambda: clock[0]), patch("gamedata.urllib.request.urlopen", side_effect=OSError("offline")), \
                    patch("gamedata.threading.Thread") as thread:
                thread.side_effect = lambda target, args, daemon: type("T", (), {"start": lambda self: target(*args)})()
                catalog.ensure(tmp, download=True)
                self.assertEqual((catalog.items, catalog.error, catalog.loading), (None, "offline", False))
                clock[0] += gamedata.RETRY_SECONDS - 1
                catalog.ensure(tmp, download=True)
                self.assertEqual(thread.call_count, 1)
                clock[0] += 2
                catalog.ensure(tmp, download=True)
                self.assertEqual(thread.call_count, 2)


if __name__ == "__main__":
    unittest.main()
