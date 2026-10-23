import unittest
from unittest import mock

from gi.repository import GLib

from lgtv_companion import display
from lgtv_companion.display import DisplayWatcher

from .test_daemon import FakeSessionBus, wait_for


class DisplayWatcherTest(unittest.TestCase):
    def setUp(self):
        self.changes = []
        patcher = mock.patch.object(display, "RETRY_SECONDS", 0.01)
        patcher.start()
        self.addCleanup(patcher.stop)

    def watch(self, *buses):
        """Start a watcher whose successive connection attempts return `buses` (None: fails)."""
        attempts = iter(buses)

        def connect():
            bus = next(attempts)
            if bus is None:
                raise GLib.Error("Could not connect: No such file or directory")
            return bus

        watcher = DisplayWatcher(self.changes.append, connect)
        watcher.start()
        return watcher

    def test_reports_display_turning_off_and_on(self):
        bus = FakeSessionBus()
        watcher = self.watch(bus)
        self.assertIs(watcher.display_on, True)
        self.assertEqual(self.changes, [])  # the initial state is only recorded

        bus.set_mode(3)
        bus.set_mode(3)
        bus.set_mode(0)
        self.assertEqual(self.changes, [False, True])

    def test_standby_and_suspend_count_as_off_and_unknown_is_ignored(self):
        bus = FakeSessionBus()
        self.watch(bus)
        bus.set_mode(1)
        bus.set_mode(0)
        bus.set_mode(2)
        bus.set_mode(-1)
        self.assertEqual(self.changes, [False, True, False])

    def test_daemon_starting_with_display_off_wakes_tv_when_it_comes_back(self):
        bus = FakeSessionBus(power_save_mode=3)
        self.watch(bus)
        bus.set_mode(0)
        self.assertEqual(self.changes, [True])

    def test_keeps_trying_until_the_user_logs_in_and_again_after_logout(self):
        first, second = FakeSessionBus(), FakeSessionBus()
        watcher = self.watch(None, None, first, None, second)
        wait_for(lambda: watcher.bus is first)

        first.close()
        self.assertIsNone(watcher.display_on)
        wait_for(lambda: watcher.bus is second)
        second.set_mode(3)
        self.assertEqual(self.changes, [False])

    def test_waits_for_gnome_shell_to_start(self):
        bus = FakeSessionBus(mutter=False)
        watcher = self.watch(bus)
        self.assertIsNone(watcher.display_on)

        bus.set_mutter_running(True)
        self.assertIs(watcher.display_on, True)
        bus.set_mutter_running(False)
        self.assertIsNone(watcher.display_on)


if __name__ == "__main__":
    unittest.main()
