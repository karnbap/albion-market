"""Opens the dashboard in its own Edge app window (no tabs or address bar).

Usage: python dashboard_window.py http://127.0.0.1:PORT
"""
from __future__ import annotations
import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import subprocess
import sys
import time

# web/index.html <title>; Edge app windows are titled by the page alone.
TITLE = '알비온 · 나의 시장 기록'
SW_MINIMIZE, SW_RESTORE = 6, 9


def edge_path():
    for base in (os.environ.get('ProgramFiles(x86)'), os.environ.get('ProgramFiles'), os.environ.get('LOCALAPPDATA')):
        if base and (path := Path(base)/'Microsoft/Edge/Application/msedge.exe').is_file():
            return path
    return None


def find_window():
    user = ctypes.WinDLL('user32', use_last_error=True)
    found = []
    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def visit(hwnd, _):
        if user.IsWindowVisible(hwnd):
            title = ctypes.create_unicode_buffer(512)
            user.GetWindowTextW(hwnd, title, len(title))
            # Exact: a browser window showing the page as a tab adds ' - Microsoft Edge'.
            if title.value == TITLE:
                found.append(hwnd)
                return False
        return True
    user.EnumWindows(visit, 0)
    return found[0] if found else None


def open_window(address, background=False, wait=15):
    """Shows the dashboard window, starting one when none is open.

    background: for the game watcher. An open window is left alone and a new
    one is minimized to the taskbar so it never takes focus from the game.
    Returns 'existing', 'opened', 'browser' or None (nothing done).
    """
    user = ctypes.WinDLL('user32', use_last_error=True)
    if hwnd := find_window():
        if not background:
            if user.IsIconic(hwnd):
                user.ShowWindow(hwnd, SW_RESTORE)
            user.SetForegroundWindow(hwnd)
        return 'existing'
    edge = edge_path()
    if edge is None:
        if background:
            return None
        os.startfile(address)
        return 'browser'
    subprocess.Popen([str(edge), f'--app={address}'], close_fds=True,
        creationflags=subprocess.DETACHED_PROCESS|subprocess.CREATE_NEW_PROCESS_GROUP)
    if background:
        deadline = time.monotonic()+wait
        while time.monotonic() < deadline:
            if hwnd := find_window():
                user.ShowWindow(hwnd, SW_MINIMIZE)
                break
            time.sleep(.2)
    return 'opened'


if __name__ == '__main__':
    if len(sys.argv) != 2 or not sys.argv[1].startswith('http://127.0.0.1:'):
        sys.exit('usage: dashboard_window.py http://127.0.0.1:PORT')
    open_window(sys.argv[1])
