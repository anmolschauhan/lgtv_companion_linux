import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from gi.repository import Gio, GLib

from lgtv_companion import webos
from lgtv_companion.daemon import LOGIND, LOGIND_IFACE, LOGIND_PATH, Daemon

from .fake_tv import FakeTV
from .test_tv import closed_port

LOGIND_XML = f"""<node><interface name="{LOGIND_IFACE}">
  <signal name="PrepareForShutdown"><arg type="b"/></signal>
  <signal name="PrepareForShutdownWithMetadata"><arg type="b"/><arg type="a{{sv}}"/></signal>
  <signal name="PrepareForSleep"><arg type="b"/></signal>
</interface></node>"""


class FakeBus:
    """Stands in for the system bus: hands out inhibitor fds and records signal subscriptions."""

    def __init__(self):
        self.handlers = {}
        self.lock_pipes = {}  # what -> write end of a pipe whose read end the daemon holds
        self.system_state = "running"
        self.jobs = []

    def call_with_unix_fd_list_sync(self, name, path, iface, method, params, *rest):
        what, who, why, mode = params.unpack()
        assert (method, mode) == ("Inhibit", "delay")
        r, w = os.pipe()
        self.lock_pipes[what] = w
        return GLib.Variant("(h)", (0,)), Gio.UnixFDList.new_from_array([r])

    def lock_held(self, what):
        try:
            os.write(self.lock_pipes[what], b"x")
            return True
        except BrokenPipeError:  # the daemon closed its end: lock released
            return False

    def call_sync(self, name, path, iface, method, params, *rest):
        if method == "Introspect":
            return GLib.Variant("(s)", (LOGIND_XML,))
        if method == "Get":
            return GLib.Variant("(v)", (GLib.Variant("s", self.system_state),))
        if method == "ListJobs":
            return GLib.Variant("(a(usssoo))", (self.jobs,))
        raise AssertionError(method)

    def signal_subscribe(self, sender, iface, member, path, arg0, flags, callback):
        self.handlers[member] = callback

    def emit(self, member, signature, args):
        self.handlers[member](self, LOGIND, LOGIND_PATH, LOGIND_IFACE, member,
                              GLib.Variant(signature, args))


def wait_for(condition, timeout=10):
    context = GLib.MainContext.default()
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("timed out")
        context.iteration(False)
        time.sleep(0.01)


class DaemonTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.tv = FakeTV(state="Active")
        self.addCleanup(self.tv.close)
        patcher = mock.patch.multiple(webos, PORT_PLAIN=self.tv.port, PORT_TLS=closed_port())
        patcher.start()
        self.addCleanup(patcher.stop)
        self.write_config()
        self.bus = FakeBus()
        self.daemon = Daemon(self.tmp / "config.json", bus=self.bus, state_dir=self.tmp / "state")

    def write_config(self, **device_options):
        device = {"name": "TV", "ip": "127.0.0.1", "client_key": "valid-key", "use_tls": False,
                  **device_options}
        (self.tmp / "config.json").write_text(json.dumps({"devices": [device]}))

    def start(self):
        self.daemon.start()
        self.assertTrue(self.bus.lock_held("sleep"))
        self.assertTrue(self.bus.lock_held("shutdown"))
        wait_for(lambda: not self.daemon._busy.locked())

    def shutdown(self, kind):
        self.bus.emit("PrepareForShutdownWithMetadata", "(ba{sv})",
                      (True, {"type": GLib.Variant("s", kind)}))

    def test_powers_on_at_boot_but_not_on_restart(self):
        self.tv.state = "Active Standby"
        self.start()
        wait_for(lambda: self.tv.state == "Active")
        self.tv.state = "Active Standby"
        Daemon(self.tmp / "config.json", bus=FakeBus(), state_dir=self.tmp / "state").start()
        time.sleep(0.5)
        self.assertEqual(self.tv.state, "Active Standby")

    def test_suspend_turns_tv_off_then_releases_lock_and_resume_turns_it_on(self):
        self.start()
        self.bus.emit("PrepareForSleep", "(b)", (True,))
        wait_for(lambda: not self.bus.lock_held("sleep"))
        self.assertEqual(self.tv.state, "Active Standby")

        self.bus.emit("PrepareForSleep", "(b)", (False,))
        self.assertTrue(self.bus.lock_held("sleep"))
        wait_for(lambda: self.tv.state == "Active")

    def test_sleep_lock_is_retaken_if_resume_comes_before_power_off_finishes(self):
        # logind only waits InhibitDelayMaxSec for us; the system can be back before we're done.
        self.start()
        first_lock = self.bus.lock_pipes["sleep"]
        self.bus.emit("PrepareForSleep", "(b)", (True,))
        self.bus.emit("PrepareForSleep", "(b)", (False,))
        wait_for(lambda: self.bus.lock_pipes["sleep"] != first_lock)
        with self.assertRaises(BrokenPipeError):
            os.write(first_lock, b"x")
        self.assertTrue(self.bus.lock_held("sleep"))

    def test_poweroff_turns_tv_off(self):
        self.start()
        self.shutdown("power-off")
        wait_for(lambda: not self.bus.lock_held("shutdown"))
        self.assertIn("system/turnOff", self.tv.uris())

    def test_reboot_leaves_tv_alone(self):
        self.start()
        requests_before = len(self.tv.requests)
        self.shutdown("reboot")
        wait_for(lambda: not self.bus.lock_held("shutdown"))
        self.assertEqual(len(self.tv.requests), requests_before)
        self.assertEqual(self.tv.state, "Active")

    def test_suspend_respects_input_check(self):
        self.write_config(hdmi_input=1, check_input_before_power_off=True)
        self.tv.app = "com.webos.app.hdmi2"
        self.start()
        self.bus.emit("PrepareForSleep", "(b)", (True,))
        wait_for(lambda: not self.bus.lock_held("sleep"))
        self.assertEqual(self.tv.state, "Active")

    def test_unannounced_poweroff_is_caught_on_stop(self):
        self.start()
        self.bus.system_state = "stopping"
        self.bus.jobs = [(1, "poweroff.target", "start", "waiting", "/j/1", "/u/1")]
        self.daemon._on_stop()
        self.assertEqual(self.tv.state, "Active Standby")

    def test_plain_stop_and_unannounced_reboot_leave_tv_alone(self):
        self.start()
        self.daemon._on_stop()
        self.bus.system_state = "stopping"
        self.bus.jobs = [(1, "reboot.target", "start", "waiting", "/j/1", "/u/1")]
        self.daemon._on_stop()
        self.assertEqual(self.tv.state, "Active")


if __name__ == "__main__":
    unittest.main()
