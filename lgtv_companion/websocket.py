"""A minimal, blocking WebSocket client (RFC 6455).

webOS TVs only need text messages over a single connection, so this covers exactly that:
the opening handshake, masked client frames, fragmented messages, ping/pong and close.
"""

import base64
import hashlib
import os
import socket
import ssl
import struct

_ACCEPT_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OP_CONT, OP_TEXT, OP_BINARY, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA


class WebSocketError(OSError):
    """The handshake failed or the peer broke the protocol."""


class ConnectionClosed(WebSocketError):
    """The peer closed the connection."""


def mask(data: bytes, key: bytes) -> bytes:
    """XOR `data` with the 4-byte `key` (masking and unmasking are the same operation)."""
    n = len(data)
    if not n:
        return data
    repeated = (key * (n // 4 + 1))[:n]
    return (int.from_bytes(data, "big") ^ int.from_bytes(repeated, "big")).to_bytes(n, "big")


def _tls_context() -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    # The TV presents a self-signed certificate, so there is nothing to verify it against.
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    # Older TV firmware uses keys/ciphers that OpenSSL 3 rejects at its default security level.
    ctx.set_ciphers("DEFAULT:@SECLEVEL=0")
    return ctx


class WebSocket:
    def __init__(self, sock: socket.socket):
        self._sock = sock
        self._buf = bytearray()

    @classmethod
    def connect(cls, host: str, port: int, *, use_tls: bool, timeout: float) -> "WebSocket":
        sock = socket.create_connection((host, port), timeout=timeout)
        try:
            if use_tls:
                sock = _tls_context().wrap_socket(sock, server_hostname=host)
            ws = cls(sock)
            ws._handshake(host, port)
        except BaseException:
            sock.close()
            raise
        return ws

    def settimeout(self, timeout: float | None) -> None:
        self._sock.settimeout(timeout)

    def send(self, text: str) -> None:
        self._send_frame(OP_TEXT, text.encode())

    def recv(self) -> str:
        """Block until a complete text message arrives. Control frames are handled inline."""
        parts = []
        while True:
            fin, opcode, payload = self._recv_frame()
            if opcode == OP_PING:
                self._send_frame(OP_PONG, payload)
            elif opcode == OP_CLOSE:
                self.close()
                raise ConnectionClosed("connection closed by peer")
            elif opcode in (OP_TEXT, OP_BINARY, OP_CONT):
                parts.append(payload)
                if fin:
                    return b"".join(parts).decode("utf-8", errors="replace")

    def close(self) -> None:
        try:
            self._send_frame(OP_CLOSE, struct.pack("!H", 1000))
        except OSError:
            pass
        self._sock.close()

    def _handshake(self, host: str, port: int) -> None:
        key = base64.b64encode(os.urandom(16)).decode()
        request = (
            "GET / HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        )
        self._sock.sendall(request.encode())
        while (end := self._buf.find(b"\r\n\r\n")) < 0:
            if len(self._buf) > 16384:
                raise WebSocketError("oversized handshake response")
            self._fill()
        head = bytes(self._buf[:end]).decode("latin-1")
        del self._buf[: end + 4]

        status_line, *header_lines = head.split("\r\n")
        if status_line.split(" ")[1:2] != ["101"]:
            raise WebSocketError(f"handshake rejected: {status_line}")
        headers = {}
        for line in header_lines:
            name, _, value = line.partition(":")
            headers[name.strip().lower()] = value.strip()
        expected = base64.b64encode(hashlib.sha1(key.encode() + _ACCEPT_GUID).digest()).decode()
        if headers.get("sec-websocket-accept") != expected:
            raise WebSocketError("handshake failed: bad Sec-WebSocket-Accept")

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        n = len(payload)
        if n < 126:
            header = struct.pack("!BB", 0x80 | opcode, 0x80 | n)
        elif n < 1 << 16:
            header = struct.pack("!BBH", 0x80 | opcode, 0x80 | 126, n)
        else:
            header = struct.pack("!BBQ", 0x80 | opcode, 0x80 | 127, n)
        key = os.urandom(4)
        self._sock.sendall(header + key + mask(payload, key))

    def _recv_frame(self) -> tuple[bool, int, bytes]:
        b0, b1 = self._read(2)
        n = b1 & 0x7F
        if n == 126:
            (n,) = struct.unpack("!H", self._read(2))
        elif n == 127:
            (n,) = struct.unpack("!Q", self._read(8))
        key = self._read(4) if b1 & 0x80 else None
        payload = self._read(n)
        if key:
            payload = mask(payload, key)
        return bool(b0 & 0x80), b0 & 0x0F, payload

    def _read(self, n: int) -> bytes:
        while len(self._buf) < n:
            self._fill()
        data = bytes(self._buf[:n])
        del self._buf[:n]
        return data

    def _fill(self) -> None:
        chunk = self._sock.recv(65536)
        if not chunk:
            raise ConnectionClosed("connection closed by peer")
        self._buf += chunk
