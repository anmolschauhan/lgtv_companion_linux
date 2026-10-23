import socket
import struct
import tempfile
import unittest

from lgtv_companion import net


def route_hex(addr):
    return format(struct.unpack("=I", socket.inet_aton(addr))[0], "08X")


class NetTest(unittest.TestCase):
    def write_temp(self, text):
        f = tempfile.NamedTemporaryFile("w", delete=False)
        self.addCleanup(lambda: __import__("os").unlink(f.name))
        f.write(text)
        f.close()
        return f.name

    def test_normalize_mac(self):
        for mac in ("AA:BB:CC:DD:EE:FF", "aa-bb-cc-dd-ee-ff", "aabb.ccdd.eeff"):
            self.assertEqual(net.normalize_mac(mac), "aa:bb:cc:dd:ee:ff")
        with self.assertRaises(ValueError):
            net.normalize_mac("aa:bb:cc")

    def test_magic_packet(self):
        packet = net.magic_packet("01:23:45:67:89:ab")
        self.assertEqual(len(packet), 102)
        self.assertEqual(packet[:6], b"\xff" * 6)
        self.assertEqual(packet[6:], bytes.fromhex("0123456789ab") * 16)

    def test_subnet_broadcast_uses_most_specific_on_link_route(self):
        header = "Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\tMTU\tWindow\tIRTT\n"
        rows = [
            ("eth0", "0.0.0.0", "192.168.0.1", "0003", "0.0.0.0"),       # default route
            ("eth0", "192.168.0.0", "0.0.0.0", "0001", "255.255.255.0"),  # on-link /24
            ("eth0", "192.168.0.0", "0.0.0.0", "0001", "255.255.0.0"),    # broader on-link
            ("vpn0", "10.8.0.0", "10.8.0.1", "0003", "255.255.0.0"),      # via a gateway
            ("eth1", "172.16.0.0", "0.0.0.0", "0000", "255.255.0.0"),     # route is down
        ]
        table = header + "".join(
            f"{iface}\t{route_hex(dest)}\t{route_hex(gw)}\t{flags}\t0\t0\t100\t{route_hex(mask)}\t0\t0\t0\n"
            for iface, dest, gw, flags, mask in rows)
        path = self.write_temp(table)
        self.assertEqual(net.subnet_broadcast("192.168.0.50", path), "192.168.0.255")
        self.assertIsNone(net.subnet_broadcast("10.8.3.4", path))
        self.assertIsNone(net.subnet_broadcast("172.16.0.9", path))
        self.assertIsNone(net.subnet_broadcast("8.8.8.8", path))
        self.assertIsNone(net.subnet_broadcast("192.168.0.50", "/nonexistent"))

    def test_arp_lookup(self):
        path = self.write_temp(
            "IP address       HW type     Flags       HW address            Mask     Device\n"
            "192.168.0.1      0x1         0x2         AA:AA:AA:AA:AA:AA     *        eth0\n"
            "192.168.0.50     0x1         0x2         a8:23:fe:01:02:03     *        eth0\n"
            "192.168.0.60     0x1         0x0         00:00:00:00:00:00     *        eth0\n")
        self.assertEqual(net.arp_lookup("192.168.0.50", path), "a8:23:fe:01:02:03")
        self.assertEqual(net.arp_lookup("192.168.0.1", path), "aa:aa:aa:aa:aa:aa")
        self.assertIsNone(net.arp_lookup("192.168.0.60", path))
        self.assertIsNone(net.arp_lookup("192.168.0.99", path))


if __name__ == "__main__":
    unittest.main()
