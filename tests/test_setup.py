"""setup.py with every download replaced by synthetic bytes; nothing is fetched."""
import gzip
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import setup

ROOT = Path(__file__).resolve().parents[1]


class SetupTests(unittest.TestCase):
    def test_refreshing_the_official_client_keeps_the_local_fork(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/'vendor').mkdir()
            shutil.copy(ROOT/'config.json', root/'config.json')
            fork = root/'vendor'/setup.FORK
            fork.write_bytes(b'MZ local fork')
            # An older recorded release forces the official client to be downloaded again.
            (root/'vendor/provenance.json').write_text(json.dumps({'release': '0.0.1'}), encoding='utf-8')
            items = json.loads((root/'config.json').read_text(encoding='utf-8-sig'))['items']
            listing = '\n'.join(f'{number}: {item} : synthetic' for number, item in enumerate(items, start=1))

            def fetch(url):
                return {setup.URL: gzip.compress(b'MZ official'), setup.ITEMS: listing.encode()}.get(url, b'license')

            with patch.object(setup, 'ROOT', root), patch.object(setup, 'fetch', fetch), patch('builtins.print'):
                setup.main()
            self.assertEqual(fork.read_bytes(), b'MZ local fork')
            self.assertEqual((root/'vendor/albiondata-client.exe').read_bytes(), b'MZ official')
            self.assertEqual(json.loads((root/'vendor/provenance.json').read_text(encoding='utf-8'))['release'], setup.RELEASE)


if __name__ == '__main__':
    unittest.main()
