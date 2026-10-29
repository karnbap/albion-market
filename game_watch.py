"""Current-user game lifecycle integration. No game input or memory inspection."""
from __future__ import annotations
import ctypes
from ctypes import wintypes
import hashlib
import json
import logging
import os
from pathlib import Path
import subprocess
import threading
import time
import urllib.request
import dashboard_window

ROOT = Path(__file__).resolve().parent
GAME = 'albion-online.exe'
CAPTURE_NAMES = ('albiondata-client-fork.exe', 'albiondata-client.exe')


class WindowsPlatform:
    def __init__(self, root):
        self.root = Path(root)
        self.k = ctypes.WinDLL('kernel32', use_last_error=True)
        self.k.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD,wintypes.DWORD)
        self.k.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        self.k.CloseHandle.argtypes = (wintypes.HANDLE,)
        self.k.OpenProcess.argtypes = (wintypes.DWORD,wintypes.BOOL,wintypes.DWORD)
        self.k.OpenProcess.restype = wintypes.HANDLE
        self.k.GetExitCodeProcess.argtypes = (wintypes.HANDLE,ctypes.POINTER(wintypes.DWORD))
        self.k.QueryFullProcessImageNameW.argtypes = (wintypes.HANDLE,wintypes.DWORD,wintypes.LPWSTR,ctypes.POINTER(wintypes.DWORD))
        self.k.TerminateProcess.argtypes = (wintypes.HANDLE,wintypes.UINT)
        self.k.WaitForSingleObject.argtypes = (wintypes.HANDLE,wintypes.DWORD)

    def processes(self):
        class Entry(ctypes.Structure):
            _fields_ = [('dwSize',wintypes.DWORD),('cntUsage',wintypes.DWORD),
                ('pid',wintypes.DWORD),('heap',ctypes.c_size_t),('module',wintypes.DWORD),
                ('threads',wintypes.DWORD),('parent',wintypes.DWORD),('priority',wintypes.LONG),
                ('flags',wintypes.DWORD),('name',wintypes.WCHAR*260)]
        self.k.Process32FirstW.argtypes = (wintypes.HANDLE,ctypes.POINTER(Entry))
        self.k.Process32NextW.argtypes = (wintypes.HANDLE,ctypes.POINTER(Entry))
        handle = self.k.CreateToolhelp32Snapshot(2,0)
        if handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            row = Entry(); row.dwSize = ctypes.sizeof(row)
            more = self.k.Process32FirstW(handle,ctypes.byref(row))
            names = set()
            while more:
                names.add(row.name.casefold())
                more = self.k.Process32NextW(handle,ctypes.byref(row))
            if not names:
                raise RuntimeError('Process inventory unavailable')
            return names
        finally:
            self.k.CloseHandle(handle)

    def capture_pid(self):
        try:
            pid = int((self.root/'data/capture.pid').read_text(encoding='utf-8-sig').strip())
            return pid if 0 < pid <= 0xFFFFFFFF else None
        except (ValueError,OSError):
            return None

    def identity(self, handle):
        code = wintypes.DWORD()
        image = ctypes.create_unicode_buffer(32768); size = wintypes.DWORD(len(image))
        if not self.k.GetExitCodeProcess(handle,ctypes.byref(code)):
            raise ctypes.WinError(ctypes.get_last_error())
        # An exited process can still be opened, but asking its image name then fails
        # (WinError 31, seen 2026-10-28) and left a crashed capture never restarted.
        if code.value != 259:
            return False
        if not self.k.QueryFullProcessImageNameW(handle,0,image,ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        # start.ps1 runs the local fork instead of the official client when it is present.
        expected = {os.path.normcase(str((self.root/'vendor'/name).resolve())) for name in CAPTURE_NAMES}
        return code.value == 259 and os.path.normcase(image.value) in expected

    def capture_running(self):
        pid = self.capture_pid()
        if pid is None:
            return False
        handle = self.k.OpenProcess(0x1000,False,pid)
        if not handle:
            if ctypes.get_last_error() == 5:
                return None
            return False
        try:
            return self.identity(handle)
        finally:
            self.k.CloseHandle(handle)

    def launch(self, *switches):
        # Automatic mode never installs software, opens a browser, or restarts
        # an existing receiver (the dashboard window is opened separately). It starts only missing local components.
        # A file avoids inherited stdout pipes keeping the launcher wait open
        # after a GUI child has successfully started.
        with (self.root/'data/game-launcher.log').open('ab') as output:
            result = subprocess.run(['powershell.exe','-NoProfile','-ExecutionPolicy','Bypass',
                '-File',str(self.root/'start.ps1'),'-NoBrowser','-Automatic',*switches],
                cwd=self.root,stdout=output,stderr=subprocess.STDOUT,timeout=40,
                creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        if result.returncode:
            raise RuntimeError('Local capture launcher failed; see game-launcher.log')

    def start_capture(self):
        self.launch()
        return self.capture_running() is True

    def address(self):
        port = int(json.loads((self.root/'config.json').read_text(encoding='utf-8-sig'))['port'])
        return f'http://127.0.0.1:{port}'

    def receiver_ready(self):
        # The capture client keeps posting to a dead port silently, so the
        # receiver itself must be checked while the game is running.
        try:
            # No proxy: a system proxy must never see or answer the local health check.
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(self.address()+'/api/health',timeout=2) as response:
                return json.load(response).get('app') == 'AlbionLocal'
        except Exception:
            return False

    def start_receiver(self):
        self.launch('-DashboardOnly')
        return self.receiver_ready()

    def open_dashboard(self):
        # A thread: waiting for the new window to minimize must not stall the watch loop.
        address = self.address()
        def run():
            try:
                dashboard_window.open_window(address,background=True)
            except Exception:
                logging.exception('Dashboard window did not open')
        threading.Thread(target=run,daemon=True).start()

    def stop_capture(self):
        pid = self.capture_pid()
        if pid is None:
            return False
        handle = self.k.OpenProcess(0x1000|0x100000|1,False,pid)
        if not handle:
            return False
        try:
            # Verify using the same handle that will be stopped, guarding PID reuse.
            if not self.identity(handle):
                return False
            if not self.k.TerminateProcess(handle,0):
                raise ctypes.WinError(ctypes.get_last_error())
            return self.k.WaitForSingleObject(handle,5000) == 0
        finally:
            self.k.CloseHandle(handle)


class Controller:
    def __init__(self, root, platform, clock=time.monotonic):
        self.root, self.platform, self.clock = Path(root), platform, clock
        self.absent_since = None
        self.next_start = 0
        self.failures = 0
        self.receiver_misses = 0
        self.next_receiver = 0
        self.window_opened = False
        self.last_status = None

    def status(self, text, game, capture):
        value = {'enabled':True,'watcher_pid':os.getpid(),'game_running':game,
            'capture_running':capture,'status':text,'checked_at':time.time()}
        tmp = self.root/'data/game-watch-state.tmp'
        tmp.write_text(json.dumps(value,ensure_ascii=False),encoding='utf-8')
        tmp.replace(self.root/'data/game-watch-state.json')
        if text != self.last_status:
            logging.info('%s',text)
            self.last_status = text
        return value

    def step(self):
        names = self.platform.processes()
        game = GAME in names
        capture = self.platform.capture_running()
        now = self.clock()
        if not game:
            self.next_start = 0
            self.failures = 0
            self.window_opened = False
        pause = self.root/'data/game-watch-paused'
        if pause.exists():
            if not game:
                pause.unlink()
            self.absent_since = None
            return self.status('수동 종료로 일시 중지 · 다음 게임 실행 때 재개',game,capture)
        if game:
            self.absent_since = None
            if capture is True:
                self.failures = 0
                return self.check_receiver(now)
            if capture is None:
                return self.status('수집기 상태 확인 불가 · 중복 실행 보류',True,None)
            if names & set(CAPTURE_NAMES):
                return self.status('다른 수집기 실행 중 · 중복 실행 보류',True,None)
            if now < self.next_start:
                return self.status('수집기 재시도 대기',True,False)
            try:
                if self.platform.start_capture():
                    self.next_start = now+10
                    self.failures = 0
                    return self.status('게임 실행 중 · 자동 수집 시작',True,True)
                raise RuntimeError('Capture did not start')
            except Exception:
                logging.exception('Automatic capture startup failed')
                self.failures += 1
                self.next_start = self.clock()+min(300,30*2**min(self.failures-1,4))
                return self.status('수집기 시작 실패 · 잠시 후 재시도',True,False)
        self.receiver_misses = 0
        if self.absent_since is None:
            self.absent_since = now
        if capture is True and now-self.absent_since >= 10:
            if not self.platform.stop_capture():
                return self.status('게임 종료 · 수집기 종료 확인 필요',False,True)
            return self.status('게임 종료 · 수집기 자동 종료',False,False)
        return self.status('게임 실행 대기' if capture is not True else '게임 종료 확인 중',False,capture)

    def check_receiver(self, now):
        if self.platform.receiver_ready():
            self.receiver_misses = 0
            if not self.window_opened:
                # Once per game session; closing the window keeps it closed until the next launch.
                self.window_opened = True
                self.platform.open_dashboard()
            return self.status('게임 실행 중 · 자동 수집 중',True,True)
        # Three misses (~9 s) so a briefly busy receiver is never started twice.
        self.receiver_misses += 1
        if self.receiver_misses < 3 or now < self.next_receiver:
            return self.status('분석 프로그램 응답 없음 · 확인 중',True,True)
        try:
            if not self.platform.start_receiver():
                raise RuntimeError('Receiver did not answer after restart')
            self.receiver_misses = 0
            return self.status('분석 프로그램 다시 시작 · 자동 수집 중',True,True)
        except Exception:
            logging.exception('Receiver restart failed')
            self.next_receiver = self.clock()+60
            return self.status('분석 프로그램 다시 시작 실패 · 잠시 후 재시도',True,True)


def main():
    (ROOT/'data').mkdir(exist_ok=True)
    logging.basicConfig(filename=ROOT/'data/game-watch.log',level=logging.INFO,
        format='%(asctime)s %(levelname)s %(message)s',encoding='utf-8')
    platform = WindowsPlatform(ROOT)
    platform.k.CreateMutexW.argtypes = (ctypes.c_void_p,wintypes.BOOL,wintypes.LPCWSTR)
    platform.k.CreateMutexW.restype = wintypes.HANDLE
    mutex = platform.k.CreateMutexW(None,True,'Local\\AlbionMarketGameWatch_'+hashlib.sha256(str(ROOT).casefold().encode()).hexdigest()[:20])
    if not mutex:
        raise ctypes.WinError(ctypes.get_last_error())
    if ctypes.get_last_error() == 183:
        platform.k.CloseHandle(mutex)
        return
    (ROOT/'data/game-watch.pid').write_text(str(os.getpid()),encoding='utf-8')
    controller = Controller(ROOT,platform)
    try:
        while True:
            try:
                controller.step()
            except Exception:
                # A failed inventory is not evidence the game exited.
                logging.exception('Game lifecycle check failed; keeping existing processes')
            time.sleep(3)
    except KeyboardInterrupt:
        pass
    finally:
        platform.k.CloseHandle(mutex)


if __name__ == '__main__':
    main()
