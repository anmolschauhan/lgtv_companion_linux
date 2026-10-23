import os
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock

from lgtv_companion import net, webos
from lgtv_companion.config import Device
from lgtv_companion.tv import TV, pair
from lgtv_companion.webos import PairingError, WebOSClient

from .fake_tv import FakeTV


def closed_port():
    with socket.create_server(("127.0.0.1", 0)) as s:
        return s.getsockname()[1]


class TVTestCase(unittest.TestCase):
    def start_tv(self, **kwargs) -> FakeTV:
        tv = FakeTV(**kwargs)
        self.addCleanup(tv.close)
        patcher = mock.patch.multiple(webos, PORT_PLAIN=tv.port, PORT_TLS=closed_port())
        patcher.start()
        self.addCleanup(patcher.stop)
        return tv

    def device(self, **kwargs) -> Device:
        return Device(**{"name": "TV", "ip": "127.0.0.1", "client_key": "valid-key",
                         "use_tls": False, **kwargs})


class PairingTest(TVTestCase):
    def test_pair_falls_back_to_plain_websocket_and_enables_wol(self):
        fake = self.start_tv(client_key="")
        prompts = []
        with mock.patch.object(net, "arp_lookup", return_value="aa:bb:cc:dd:ee:ff"):
            device = pair("127.0.0.1", pairing_timeout=5, on_prompt=lambda: prompts.append(1))
        self.assertEqual(prompts, [1])
        self.assertEqual((device.client_key, device.use_tls), ("new-key", False))
        self.assertEqual((device.name, device.macs), ("OLED55C1", ["aa:bb:cc:dd:ee:ff"]))
        alert = dict(fake.requests)["system.notifications/createAlert"]
        self.assertEqual(alert["onclose"]["uri"], "luna://com.webos.settingsservice/setSystemSettings")
        self.assertIn("system.notifications/closeAlert", fake.uris())

    def test_rejected_prompt_raises(self):
        self.start_tv(client_key="", on_prompt="reject")
        with self.assertRaisesRegex(PairingError, "rejected"):
            pair("127.0.0.1", pairing_timeout=5)

    def test_known_key_registers_without_prompt(self):
        fake = self.start_tv()
        with WebOSClient.connect("127.0.0.1", "valid-key", use_tls=False) as client:
            self.assertEqual(client.client_key, "valid-key")
        self.assertEqual(fake.prompts, 0)

    def test_stale_key_fails_without_waiting_for_the_prompt(self):
        self.start_tv(on_prompt="ignore")
        start = time.monotonic()
        with self.assertRaisesRegex(PairingError, "pair"):
            WebOSClient.connect("127.0.0.1", "stale-key", use_tls=False, timeout=0.5)
        self.assertLess(time.monotonic() - start, 3)


class PowerOffTest(TVTestCase):
    def test_turns_off_an_active_tv(self):
        fake = self.start_tv(state="Active")
        self.assertTrue(TV(self.device()).power_off())
        self.assertEqual(fake.uris(), [webos_uri("getPowerState"), "system/turnOff"])

    def test_leaves_a_tv_in_standby_alone(self):
        # turnOff is a toggle on some models: sending it now would turn the TV *on*.
        fake = self.start_tv(state="Active Standby")
        self.assertTrue(TV(self.device()).power_off())
        self.assertNotIn("system/turnOff", fake.uris())
        self.assertEqual(fake.state, "Active Standby")

    def test_unreachable_tv_counts_as_off(self):
        self.start_tv(state="Off")
        self.assertTrue(TV(self.device()).power_off())

    def test_input_check_leaves_tv_on_when_showing_something_else(self):
        fake = self.start_tv(app="com.webos.app.hdmi2")
        device = self.device(hdmi_input=1, check_input_before_power_off=True)
        self.assertFalse(TV(device).power_off(forced=False))
        self.assertNotIn("system/turnOff", fake.uris())
        # A manual (forced) power-off ignores the check.
        self.assertTrue(TV(device).power_off(forced=True))
        self.assertIn("system/turnOff", fake.uris())

    def test_input_check_turns_off_when_showing_the_pc(self):
        fake = self.start_tv(app="com.webos.app.hdmi1")
        device = self.device(hdmi_input=1, check_input_before_power_off=True)
        self.assertTrue(TV(device).power_off(forced=False))
        self.assertIn("system/turnOff", fake.uris())


class PowerOnTest(TVTestCase):
    def test_already_on(self):
        fake = self.start_tv(state="Active")
        self.assertTrue(TV(self.device()).power_on(timeout=5))
        self.assertEqual(fake.uris(), [webos_uri("getPowerState")])

    def test_unblanks_screen_off(self):
        fake = self.start_tv(state="Screen Off")
        self.assertTrue(TV(self.device()).power_on(timeout=5))
        self.assertEqual(fake.state, "Active")

    def test_toggles_network_standby_once(self):
        fake = self.start_tv(state="Active Standby")
        self.assertTrue(TV(self.device()).power_on(timeout=10))
        self.assertEqual(fake.state, "Active")
        self.assertEqual(fake.uris().count("system/turnOff"), 1)

    def test_wakes_with_wol_then_switches_input(self):
        fake = self.start_tv(state="Off", app="com.webos.app.hdmi3")
        packets = []

        def fake_wol(macs, ip):
            packets.append((macs, ip))
            fake.state = "Active"

        device = self.device(macs=["aa:bb:cc:dd:ee:ff"], hdmi_input=2, set_input_on_power_on=True)
        with mock.patch.object(net, "send_wol", fake_wol):
            self.assertTrue(TV(device).power_on(timeout=10))
        self.assertEqual(packets[0], (["aa:bb:cc:dd:ee:ff"], "127.0.0.1"))
        self.assertEqual(fake.app, "com.webos.app.hdmi2")

    def test_gives_up_after_timeout(self):
        self.start_tv(state="Off")
        start = time.monotonic()
        with mock.patch.object(net, "send_wol"):
            self.assertFalse(TV(self.device(macs=["aa:bb:cc:dd:ee:ff"])).power_on(timeout=2))
        self.assertLess(time.monotonic() - start, 5)

    def test_cancel_stops_waiting(self):
        self.start_tv(state="Off")
        cancel = threading.Event()
        threading.Timer(0.5, cancel.set).start()
        start = time.monotonic()
        with mock.patch.object(net, "send_wol"):
            self.assertFalse(TV(self.device(macs=["aa:bb:cc:dd:ee:ff"])).power_on(30, cancel))
        self.assertLess(time.monotonic() - start, 5)


class ScreenTest(TVTestCase):
    def test_screen_off_and_on(self):
        fake = self.start_tv()
        TV(self.device()).screen_off()
        self.assertEqual(fake.state, "Screen Off")
        TV(self.device()).screen_on()
        self.assertEqual(fake.state, "Active")

    def test_screen_off_falls_back_to_luna(self):
        fake = self.start_tv()
        fake.unsupported.add(webos_uri("turnOffScreen"))
        TV(self.device()).screen_off()
        alert = dict(fake.requests)["system.notifications/createAlert"]
        self.assertEqual(alert["onclose"]["uri"], "luna://" + webos_uri("turnOffScreen"))

    def test_status(self):
        self.start_tv(state="Screen Off", app="com.webos.app.hdmi1")
        self.assertEqual(TV(self.device()).status(),
                         {"state": "Screen Off", "processing": "", "app": "com.webos.app.hdmi1"})


@unittest.skipUnless(shutil.which("openssl"), "needs openssl to make a test certificate")
class TLSTest(unittest.TestCase):
    def test_connects_over_tls_with_a_self_signed_certificate(self):
        with tempfile.TemporaryDirectory() as tmp:
            cert, key = os.path.join(tmp, "cert.pem"), os.path.join(tmp, "key.pem")
            subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                            "-subj", "/CN=fake-tv", "-keyout", key, "-out", cert],
                           check=True, capture_output=True)
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(cert, key)
        fake = FakeTV(tls_context=ctx)
        self.addCleanup(fake.close)
        with mock.patch.object(webos, "PORT_TLS", fake.port):
            device = Device(name="TV", ip="127.0.0.1", client_key="valid-key", use_tls=True)
            self.assertTrue(TV(device).power_off())
        self.assertIn("system/turnOff", fake.uris())


def webos_uri(method):
    return f"com.webos.service.tvpower/power/{method}"


if __name__ == "__main__":
    unittest.main()
