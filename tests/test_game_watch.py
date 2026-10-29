"""Game/capture lifecycle simulations; never start or stop the real game/client."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from game_watch import Controller, GAME, WindowsPlatform
import subprocess


class FakePlatform:
    def __init__(self):
        self.names = set()
        self.running = False
        self.starts = self.stops = self.receiver_starts = self.windows = 0
        self.success = True
        self.receiver = True
    def processes(self): return self.names
    def capture_running(self): return self.running
    def start_capture(self):
        self.starts += 1
        self.running = self.success
        return self.success
    def stop_capture(self):
        self.stops += 1
        self.running = False
        return True
    def receiver_ready(self): return self.receiver
    def start_receiver(self):
        self.receiver_starts += 1
        self.receiver = self.success
        return self.success
    def open_dashboard(self): self.windows += 1


class GameWatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name);(self.root/'data').mkdir()
        self.time = 100
        self.platform = FakePlatform()
        self.controller = Controller(self.root,self.platform,lambda:self.time)
    def tearDown(self): self.temp.cleanup()
    def step(self, seconds=0):
        self.time += seconds
        return self.controller.step()

    def test_game_launch_starts_once_and_running_game_does_not_duplicate(self):
        self.platform.names={GAME}
        self.step();self.step(3);self.step(3)
        self.assertEqual(self.platform.starts,1)
        self.assertEqual(self.platform.stops,0)

    def test_launcher_and_anticheat_alone_are_not_game(self):
        self.platform.names={'albion-online_be.exe','beservice.exe','albionlauncher.exe'}
        self.step()
        self.assertEqual(self.platform.starts,0)

    def test_exit_stops_capture_after_grace_period(self):
        self.platform.names={GAME};self.step()
        self.platform.names=set();self.step();self.step(9)
        self.assertEqual(self.platform.stops,0)
        self.step(1)
        self.assertEqual(self.platform.stops,1)
        self.assertFalse(self.platform.running)

    def test_brief_game_restart_cancels_pending_stop(self):
        self.platform.names={GAME};self.step()
        self.platform.names=set();self.step();self.step(6)
        self.platform.names={GAME};self.step(3)
        self.assertEqual(self.platform.stops,0)
        self.assertEqual(self.platform.starts,1)

    def test_crashed_capture_is_restarted_while_game_is_running(self):
        self.platform.names={GAME};self.step()
        self.platform.running=False
        self.step(11)
        self.assertEqual(self.platform.starts,2)

    def test_failed_starts_back_off(self):
        self.platform.names={GAME};self.platform.success=False
        with self.assertLogs(level='ERROR'): self.step()
        self.step(10);self.step(19)
        self.assertEqual(self.platform.starts,1)
        with self.assertLogs(level='ERROR'): self.step(1)
        self.assertEqual(self.platform.starts,2)
        self.step(59)
        self.assertEqual(self.platform.starts,2)

    def test_new_game_session_resets_old_start_failure_backoff(self):
        self.platform.names={GAME};self.platform.success=False
        with self.assertLogs(level='ERROR'): self.step()
        self.platform.names=set();self.step(1)
        self.platform.success=True;self.platform.names={GAME};self.step(1)
        self.assertEqual(self.platform.starts,2)
        self.assertTrue(self.platform.running)

    def test_unmanaged_capture_not_duplicated_or_stopped(self):
        self.platform.names={GAME,'albiondata-client.exe'}
        self.step()
        self.assertEqual(self.platform.starts,0)
        self.platform.names={'albiondata-client.exe'}
        self.step();self.step(15)
        self.assertEqual(self.platform.stops,0)
        self.platform.names={GAME,'albiondata-client-fork.exe'}
        self.step()
        self.assertEqual(self.platform.starts,0)

    def test_unknown_capture_state_not_started_or_stopped(self):
        self.platform.names={GAME};self.platform.running=None
        self.step()
        self.assertEqual(self.platform.starts,0)
        self.platform.names=set();self.step();self.step(15)
        self.assertEqual(self.platform.stops,0)

    def test_manual_stop_pauses_current_session_and_next_game_resumes(self):
        pause=self.root/'data/game-watch-paused';pause.write_text('manual')
        self.platform.names={GAME};self.step();self.step(30)
        self.assertEqual(self.platform.starts,0)
        self.platform.names=set();self.step()
        self.assertFalse(pause.exists())
        self.platform.names={GAME};self.step(3)
        self.assertEqual(self.platform.starts,1)

    def test_status_file_reports_only_lifecycle_state(self):
        self.platform.names={GAME};self.step()
        state=json.loads((self.root/'data/game-watch-state.json').read_text(encoding='utf-8'))
        self.assertTrue(state['game_running'])
        self.assertTrue(state['capture_running'])
        self.assertNotIn('player',state)

    def test_dead_receiver_is_restarted_while_game_runs(self):
        self.platform.names={GAME};self.step()
        self.platform.receiver=False
        self.step(3);self.step(3)
        self.assertEqual(self.platform.receiver_starts,0)
        self.step(3)
        self.assertEqual(self.platform.receiver_starts,1)
        self.assertTrue(self.platform.receiver)
        self.step(3)
        self.assertEqual((self.platform.receiver_starts,self.platform.starts),(1,1))

    def test_failed_receiver_restart_waits_before_retrying(self):
        self.platform.names={GAME};self.step()
        self.platform.receiver=False;self.platform.success=False
        self.step(3);self.step(3)
        with self.assertLogs(level='ERROR'): self.step(3)
        self.step(30)
        self.assertEqual(self.platform.receiver_starts,1)
        with self.assertLogs(level='ERROR'): self.step(30)
        self.assertEqual(self.platform.receiver_starts,2)

    def test_receiver_left_alone_when_game_closed_or_manually_stopped(self):
        self.platform.receiver=False
        self.step();self.step(3);self.step(3);self.step(3)
        (self.root/'data/game-watch-paused').write_text('manual')
        self.platform.names={GAME}
        for _ in range(4): self.step(3)
        self.assertEqual(self.platform.receiver_starts,0)

    def test_dashboard_window_opens_once_per_game_session(self):
        self.step()
        self.assertEqual(self.platform.windows,0)
        self.platform.names={GAME};self.platform.receiver=False
        self.step();self.step(3)
        # Not before the receiver answers: the window would show a dead page.
        self.assertEqual(self.platform.windows,0)
        self.platform.receiver=True
        for _ in range(4): self.step(3)
        self.assertEqual(self.platform.windows,1)
        self.platform.names=set()
        for _ in range(5): self.step(3)
        self.platform.names={GAME}
        for _ in range(3): self.step(3)
        self.assertEqual(self.platform.windows,2)

    def test_launcher_uses_file_output_not_gui_inherited_pipes(self):
        platform=WindowsPlatform.__new__(WindowsPlatform)
        platform.root=self.root
        with patch('game_watch.subprocess.run',return_value=SimpleNamespace(returncode=0)) as run, patch.object(WindowsPlatform,'capture_running',return_value=True):
            self.assertTrue(platform.start_capture())
            arguments=run.call_args.kwargs
            self.assertNotEqual(arguments['stdout'],subprocess.PIPE)
            self.assertEqual(arguments['stderr'],subprocess.STDOUT)
            self.assertNotIn('capture_output',arguments)

    def test_exited_capture_is_not_asked_for_its_image_name(self):
        platform=WindowsPlatform.__new__(WindowsPlatform)
        platform.root=self.root
        def exited(handle,code):
            code._obj.value=0
            return True
        def fail(*args):
            raise AssertionError('image name asked for an exited process')
        platform.k=SimpleNamespace(GetExitCodeProcess=exited,QueryFullProcessImageNameW=fail)
        self.assertFalse(platform.identity(1))


if __name__=='__main__': unittest.main()
