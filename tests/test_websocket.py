import os
import socket
import struct
import unittest

from lgtv_companion.websocket import ConnectionClosed, WebSocket, mask


class WebSocketFramingTest(unittest.TestCase):
    """Drives the client against raw frames written to the other end of a socket pair."""

    def setUp(self):
        client_sock, self.server = socket.socketpair()
        self.addCleanup(self.server.close)
        self.ws = WebSocket(client_sock)
        self.addCleanup(self.ws.close)
        self.ws.settimeout(5)
        self.server.settimeout(5)

    def send_frame(self, opcode, payload, fin=True):
        b0 = (0x80 if fin else 0) | opcode
        n = len(payload)
        if n < 126:
            header = struct.pack("!BB", b0, n)
        elif n < 65536:
            header = struct.pack("!BBH", b0, 126, n)
        else:
            header = struct.pack("!BBQ", b0, 127, n)
        self.server.sendall(header + payload)

    def read(self, n):
        buf = b""
        while len(buf) < n:
            buf += self.server.recv(n - len(buf))
        return buf

    def read_frame(self):
        b0, b1 = self.read(2)
        self.assertTrue(b1 & 0x80, "client frames must be masked")
        n = b1 & 0x7F
        if n == 126:
            (n,) = struct.unpack("!H", self.read(2))
        elif n == 127:
            (n,) = struct.unpack("!Q", self.read(8))
        key = self.read(4)
        return b0 & 0x0F, mask(self.read(n), key)

    def test_mask_is_its_own_inverse(self):
        data, key = os.urandom(1001), os.urandom(4)
        self.assertNotEqual(mask(data, key), data)
        self.assertEqual(mask(mask(data, key), key), data)

    def test_sends_masked_text_frames_of_every_length_encoding(self):
        for size in (0, 5, 125, 126, 65535, 65536, 70000):
            self.ws.send("x" * size)
            self.assertEqual(self.read_frame(), (0x1, b"x" * size), size)

    def test_receives_every_length_encoding(self):
        for size in (0, 125, 126, 65535, 65536, 70000):
            self.send_frame(0x1, b"y" * size)
            self.assertEqual(self.ws.recv(), "y" * size)

    def test_reassembles_fragmented_messages(self):
        self.send_frame(0x1, b"hel", fin=False)
        self.send_frame(0x0, b"lo")
        self.assertEqual(self.ws.recv(), "hello")

    def test_answers_ping_with_pong(self):
        self.send_frame(0x9, b"are you there")
        self.send_frame(0x1, b"after ping")
        self.assertEqual(self.ws.recv(), "after ping")
        self.assertEqual(self.read_frame(), (0xA, b"are you there"))

    def test_close_frame_raises(self):
        self.send_frame(0x8, struct.pack("!H", 1000))
        with self.assertRaises(ConnectionClosed):
            self.ws.recv()

    def test_peer_hangup_raises(self):
        self.server.close()
        with self.assertRaises(ConnectionClosed):
            self.ws.recv()


if __name__ == "__main__":
    unittest.main()
