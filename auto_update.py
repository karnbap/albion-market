"""Atomically reload trusted local analysis code/assets without restarting the listener."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import threading
from http.server import ThreadingHTTPServer
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

ASSETS = {'/': 'index.html', '/app.js': 'app.js', '/style.css': 'style.css',
          '/app-v2.js': 'app-v2.js', '/v2.css': 'v2.css'}


def find_node():
    for candidate in (os.environ.get('ALBION_NODE'), shutil.which('node'),
                      Path.home()/'.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node.exe'):
        if candidate and Path(candidate).exists():
            return str(candidate)
    return None


class Runtime:
    def __init__(self, source_root, data_root):
        self.source_root, self.data_root = Path(source_root), Path(data_root)
        self.lock = threading.RLock()
        self.token = secrets.token_urlsafe(24)
        self.store = None
        self.module = None
        self.assets = {}
        self.revision = None
        self.pending = None
        self.rejected = None
        self.syntax_checked = True
        self.stopped = threading.Event()
        self.apply_changes()

    def snapshot(self):
        code = (self.source_root / 'market.py').read_bytes()
        assets = {url: (self.source_root/'web'/name).read_bytes() for url,name in ASSETS.items()}
        digest = hashlib.sha256(code)
        ui = hashlib.sha256()
        for url, content in sorted(assets.items()):
            ui.update(url.encode()); ui.update(content)
        digest.update(ui.digest())
        return digest.hexdigest(), ui.hexdigest(), code, assets

    def status(self, revision):
        value = {'enabled': True, 'status': '자동 반영 사용 중', 'revision': revision[:12]}
        if not self.syntax_checked:
            value['note'] = 'Node가 없어 화면 코드 구문 검사는 생략'
        return value

    def validate_assets(self, assets):
        node = find_node()
        # Node is optional: without it the dashboard must still start, otherwise
        # the game watcher can never bring up capture on a PC without Node.
        self.syntax_checked = bool(node)
        if not node:
            logging.warning('Node not found; browser script syntax check skipped')
        else:
            for script in {assets['/app.js'], assets['/app-v2.js']}:
                result = subprocess.run([node, '--check', '-'], input=script, capture_output=True, timeout=15,
                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                if result.returncode:
                    raise ValueError('화면 코드 구문 오류 · 기존 버전을 유지합니다.')
        html = assets['/'].decode('utf-8')
        for element in ('connection', 'settings', 'item', 'city', 'overview'):
            if f'id="{element}"' not in html:
                raise ValueError('화면 필수 요소 누락: ' + element)

    def apply_changes(self, snapshot=None):
        revision, ui_revision, code, assets = snapshot or self.snapshot()
        if revision == self.revision:
            return False
        candidate = None
        try:
            compiled = compile(code, str(self.source_root/'market.py'), 'exec')
            self.validate_assets(assets)
            name = '_albion_local_' + revision[:16]
            spec = importlib.util.spec_from_loader(name, loader=None, origin=str(self.source_root/'market.py'))
            module = importlib.util.module_from_spec(spec)
            module.__file__ = str(self.source_root/'market.py')
            # Do not register each revision in sys.modules indefinitely.
            exec(compiled, module.__dict__)
            with self.lock:
                candidate = module.Store(self.data_root)
                candidate.ui_revision = ui_revision
                candidate.auto_update = self.status(revision)
                json.dumps(candidate.state(), allow_nan=False)
                base = module.handler_class(candidate, self.token)
                runtime = self
                class CachedHandler(base):
                    def do_GET(self):
                        path = urlparse(self.path).path
                        if path in runtime.assets:
                            if not self.valid_host():
                                return self.respond({'error': 'Invalid host'}, 403)
                            kind = 'text/html' if path == '/' else 'application/javascript' if path.endswith('.js') else 'text/css'
                            return self.respond(runtime.assets[path], content_type=kind+'; charset=utf-8')
                        return super().do_GET()
                old = self.store
                self.module, self.store, self.assets = module, candidate, assets
                self.handler, self.revision = CachedHandler, revision
                self.rejected = None
                candidate = None
                if old is not None:
                    old.db.close()
            logging.info('Local update applied: %s', revision[:12])
            return True
        except Exception as exc:
            if candidate is not None:
                candidate.db.close()
            if self.store is None:
                raise
            self.rejected = revision
            with self.lock:
                self.store.auto_update = {'enabled': True, 'status': '수정 오류 · 이전 정상 버전 유지', 'error': str(exc)[:200]}
            logging.exception('Local update rejected; previous version remains active')
            return False

    def handle(self, request, address, server):
        # The listener never closes during updates. Requests already accepted by
        # the OS wait for this same lock while a validated version is exchanged.
        request.settimeout(10)
        with self.lock:
            self.handler(request, address, server)

    def watch_once(self):
        current = self.snapshot()
        if current[0] == self.rejected:
            return False
        if current[0] == self.revision:
            self.pending = None
            self.rejected = None
            with self.lock:
                self.store.auto_update = self.status(self.revision)
            return False
        if current[0] != self.pending:
            self.pending = current[0]
            return False
        return self.apply_changes(current)

    def watch(self):
        while not self.stopped.wait(2):
            try:
                self.watch_once()
            except Exception:
                logging.exception('Reading local update files failed; keeping current version')

    def mail_once(self):
        # The report is built under the lock, but SMTP (up to tens of seconds)
        # runs outside it so capture uploads are never held back by a slow mail server.
        with self.lock:
            store, module = self.store, self.module
            local = store.clock().astimezone(ZoneInfo(store.cfg['timezone']))
            if local.strftime('%H:%M') not in store.cfg['email']['times']:
                return False
            slot = local.strftime('%Y-%m-%d %H:%M')
            if store.db.execute('SELECT 1 FROM mail_receipts WHERE slot=?',(slot,)).fetchone():
                return False
            message = module.build_report(store, slot)
            settings = dict(store.cfg['email'])
        if message is None:
            return False
        module.send_report(settings, message)
        with self.lock, self.store.db:
            self.store.db.execute('INSERT OR IGNORE INTO mail_receipts VALUES(?,?)',(slot,module.now_iso()))
        return True

    def mail(self):
        # A single mail worker survives revisions.
        while not self.stopped.wait(20):
            try:
                self.mail_once()
            except Exception:
                logging.exception('Scheduled email failed')


def run(source_root, data_root, port=None):
    data_root = Path(data_root)
    (data_root/'data').mkdir(exist_ok=True, parents=True)
    handler = RotatingFileHandler(data_root/'data/server.log', maxBytes=5_000_000, backupCount=3, encoding='utf-8')
    logging.basicConfig(handlers=[handler], level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    runtime = Runtime(source_root, data_root)
    server = ThreadingHTTPServer(('127.0.0.1', port or runtime.store.cfg['port']), runtime.handle)
    server.daemon_threads = True
    threading.Thread(target=runtime.watch, daemon=True).start()
    threading.Thread(target=runtime.mail, daemon=True).start()
    print(f'Albion local (automatic updates): http://127.0.0.1:{server.server_port}',flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        runtime.stopped.set()
        server.server_close()

