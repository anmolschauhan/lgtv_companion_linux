import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from lgtv_companion.config import Config, Device


class ConfigTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "sub" / "config.json"

    def test_missing_file_gives_empty_config(self):
        config = Config.load(self.path)
        self.assertEqual(config.devices, [])
        self.assertEqual(config.power_on_timeout, 40)

    def test_round_trip_and_private_permissions(self):
        config = Config(path=self.path, power_on_timeout=25, devices=[
            Device(name="Living room", ip="192.168.0.50", macs=["aa:bb:cc:dd:ee:ff"],
                   client_key="secret", hdmi_input=2, check_input_before_power_off=True)])
        config.save()
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        self.assertEqual(Config.load(self.path), config)

    def test_unknown_keys_are_ignored(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps(
            {"devices": [{"name": "TV", "ip": "10.0.0.2", "from_the_future": 1}]}))
        self.assertEqual(Config.load(self.path).devices, [Device(name="TV", ip="10.0.0.2")])

    def test_invalid_files_raise_value_error(self):
        self.path.parent.mkdir(parents=True)
        for text in ("{not json", json.dumps({"devices": [{"name": "no ip"}]})):
            self.path.write_text(text)
            with self.assertRaises(ValueError):
                Config.load(self.path)

    def test_select(self):
        a = Device(name="A", ip="10.0.0.1")
        b = Device(name="B", ip="10.0.0.2", enabled=False)
        config = Config(devices=[a, b], path=self.path)
        self.assertEqual(config.select(), [a])
        self.assertEqual(config.select("B"), [b])
        self.assertEqual(config.select("10.0.0.1"), [a])
        self.assertEqual(config.select("C"), [])


if __name__ == "__main__":
    unittest.main()
