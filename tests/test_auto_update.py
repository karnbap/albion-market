"""Isolated automatic-update tests; never restart the production process."""
import json
import os
import shutil
import tempfile
import threading
import unittest
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from http.server import ThreadingHTTPServer
from unittest.mock import patch

from zoneinfo import ZoneInfo

from auto_update import Runtime, find_node

ROOT = Path(__file__).resolve().parents[1]


class AutomaticUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        shutil.copy2(ROOT/'market.py', self.root/'market.py')
        shutil.copy2(ROOT/'config.json', self.root/'config.json')
        shutil.copytree(ROOT/'web', self.root/'web')
        self.runtime = Runtime(self.root, self.root)

    def tearDown(self):
        self.runtime.stopped.set()
        self.runtime.store.db.close()
        self.temp.cleanup()

    def test_backend_update_keeps_token_data_and_user_settings(self):
        before_config = (self.root/'config.json').read_bytes()
        token = self.runtime.token
        self.runtime.store.ingest('marketorders.ingest', {'Orders':[self.order(1)]})
        p = self.root/'market.py'
        p.write_text(p.read_text(encoding='utf-8').replace('20261025.8','20261025.8-test'),encoding='utf-8')
        self.assertTrue(self.runtime.apply_changes())
        self.assertEqual(self.runtime.store.state()['build'],'20261025.8-test')
        self.assertEqual(self.runtime.token,token)
        self.assertEqual(self.runtime.store.state()['packets'],1)
        self.assertEqual((self.root/'config.json').read_bytes(),before_config)

    def test_broken_backend_keeps_previous_working_version(self):
        revision, store = self.runtime.revision, self.runtime.store
        (self.root/'market.py').write_text('def broken(:',encoding='utf-8')
        with self.assertLogs(level='ERROR'):
            self.assertFalse(self.runtime.apply_changes())
        self.assertIs(self.runtime.store,store)
        self.assertEqual(self.runtime.revision,revision)
        self.assertEqual(self.runtime.store.state()['build'],'20261025.8')

    @unittest.skipUnless(find_node(), 'Node syntax check unavailable')
    def test_broken_frontend_not_served_and_next_good_update_recovers(self):
        original = self.runtime.assets['/app.js']
        (self.root/'web/app.js').write_text('const broken = ;',encoding='utf-8')
        with self.assertLogs(level='ERROR'):
            self.assertFalse(self.runtime.apply_changes())
        self.assertEqual(self.runtime.assets['/app.js'],original)
        (self.root/'web/app.js').write_bytes(original+b'\n// validated local revision\n')
        self.assertTrue(self.runtime.apply_changes())
        self.assertEqual(self.runtime.store.auto_update['status'],'자동 반영 사용 중')

    def test_frontend_revision_changes_only_for_assets_and_waits_for_stable_files(self):
        old = self.runtime.store.ui_revision
        p = self.root/'market.py'
        p.write_bytes(p.read_bytes()+b'\n# backend-only revision\n')
        self.assertFalse(self.runtime.watch_once())
        self.assertTrue(self.runtime.watch_once())
        self.assertEqual(self.runtime.store.ui_revision,old)
        p = self.root/'web/style.css'
        p.write_bytes(p.read_bytes()+b'\n/* ui revision */\n')
        self.assertFalse(self.runtime.watch_once())
        self.assertTrue(self.runtime.watch_once())
        self.assertNotEqual(self.runtime.store.ui_revision,old)

    def test_reverting_bad_edit_clears_error_status(self):
        p = self.root/'market.py';original=p.read_bytes()
        p.write_text('def invalid(:',encoding='utf-8')
        with self.assertLogs(level='ERROR'):
            self.assertFalse(self.runtime.apply_changes())
        p.write_bytes(original)
        self.assertFalse(self.runtime.watch_once())
        self.assertEqual(self.runtime.store.auto_update['status'],'자동 반영 사용 중')

    def test_missing_node_does_not_block_startup(self):
        self.runtime.store.db.close()
        with patch('auto_update.find_node', return_value=None), self.assertLogs(level='WARNING'):
            self.runtime = Runtime(self.root, self.root)
        self.assertIsNotNone(self.runtime.store)
        self.assertIn('Node', self.runtime.store.auto_update['note'])

    def test_scheduled_mail_is_sent_without_holding_the_request_lock(self):
        store = self.runtime.store
        local = store.clock().astimezone(ZoneInfo(store.cfg['timezone']))
        store.cfg['email'] = dict(store.cfg['email'], enabled=True, to='a@example.com', **{'from': 'b@example.com'},
                                  smtp_host='smtp.example.com', times=[local.strftime('%H:%M')])
        free = []
        def lock_is_free():
            if not self.runtime.lock.acquire(timeout=1):
                return False
            self.runtime.lock.release()
            return True
        def send(settings, message):
            # Probe from another thread: the RLock is re-entrant for this one.
            probe = threading.Thread(target=lambda: free.append(lock_is_free()))
            probe.start(); probe.join()
        with patch.dict(os.environ, {store.cfg['email']['password_env']: 'secret'}), patch.object(self.runtime.module, 'send_report', send):
            self.assertTrue(self.runtime.mail_once())
            self.assertFalse(self.runtime.mail_once())
        self.assertEqual(free, [True])

    def order(self, number):
        return {'Id':number,'ItemTypeId':'T7_RELIC','LocationId':'1002','QualityLevel':1,
            'EnchantmentLevel':0,'UnitPriceSilver':1000000,'Amount':10,
            'AuctionType':'offer','Expires':'2027-01-01T00:00:00+00:00'}

    def test_listener_accepts_ingestion_during_update_and_serves_cached_assets(self):
        server = ThreadingHTTPServer(('127.0.0.1',0),self.runtime.handle)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        address = f'http://127.0.0.1:{server.server_port}'
        def ingest(i):
            request=urllib.request.Request(address+'/ingest/europe/marketorders.ingest',
                data=json.dumps({'Orders':[self.order(i)]}).encode(),headers={'Content-Type':'application/json'})
            with urllib.request.urlopen(request,timeout=10) as r: return json.load(r)['accepted']
        try:
            p = self.root/'market.py';p.write_bytes(p.read_bytes()+b'\n# parallel ingestion update\n')
            with ThreadPoolExecutor(max_workers=6) as pool:
                requests=[pool.submit(ingest,i) for i in range(10)]
                update=pool.submit(self.runtime.apply_changes)
                self.assertEqual(sum(r.result() for r in requests),10)
                self.assertTrue(update.result())
            self.assertEqual(self.runtime.store.state()['packets'],10)
            cached=self.runtime.assets['/app.js']
            (self.root/'web/app.js').write_text('bad edit not yet validated',encoding='utf-8')
            with urllib.request.urlopen(address+'/app.js') as response:
                self.assertEqual(response.read(),cached)
        finally:
            server.shutdown();server.server_close();thread.join(timeout=3)


if __name__=='__main__':
    unittest.main()
