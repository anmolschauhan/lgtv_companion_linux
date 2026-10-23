"""A fake LG webOS TV for tests: speaks SSAP over a real (optionally TLS) WebSocket."""

import base64
import hashlib
import json
import re
import socket
import struct
import threading

from lgtv_companion.websocket import _ACCEPT_GUID, mask

POWER_STATE = "com.webos.service.tvpower/power/getPowerState"


class FakeTV:
    def __init__(self, *, state="Active", client_key="valid-key", on_prompt="accept",
                 app="com.webos.app.hdmi1", tls_context=None):
        self.state = state            # "Off" means unreachable
        self.client_key = client_key
        self.on_prompt = on_prompt    # "accept", "reject" or "ignore"
        self.app = app
        self.unsupported: set[str] = set()   # URIs that answer with an error
        self.requests: list[tuple[str, dict]] = []
        self.prompts = 0
        self._tls = tls_context
        self._server = socket.create_server(("127.0.0.1", 0))
        self.port = self._server.getsockname()[1]
        threading.Thread(target=self._accept_loop, daemon=True).start()

    def uris(self) -> list[str]:
        return [uri for uri, _ in self.requests]

    def close(self) -> None:
        self._server.close()

    def _accept_loop(self):
        while True:
            try:
                conn, _ = self._server.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn):
        try:
            if self.state == "Off":
                return
            if self._tls:
                conn = self._tls.wrap_socket(conn, server_side=True)
            self._handshake(conn)
            while True:
                msg = json.loads(self._recv(conn))
                for reply in self._handle(msg):
                    self._send(conn, reply)
        except (OSError, ValueError):
            pass
        finally:
            conn.close()

    def _handle(self, msg):
        msg_id = msg["id"]
        if msg["type"] == "register":
            key = msg["payload"].get("client-key")
            if key and key == self.client_key:
                return [{"type": "registered", "id": msg_id, "payload": {"client-key": key}}]
            self.prompts += 1
            replies = [{"type": "response", "id": msg_id,
                        "payload": {"pairingType": "PROMPT", "returnValue": True}}]
            if self.on_prompt == "accept":
                self.client_key = "new-key"
                replies.append({"type": "registered", "id": msg_id,
                                "payload": {"client-key": self.client_key}})
            elif self.on_prompt == "reject":
                replies.append({"type": "error", "id": msg_id, "error": "403 User rejected pairing",
                                "payload": {}})
            return replies

        uri = msg["uri"].removeprefix("ssap://")
        payload = msg.get("payload", {})
        self.requests.append((uri, payload))

        def ok(**extra):
            return [{"type": "response", "id": msg_id, "payload": {"returnValue": True, **extra}}]

        if uri in self.unsupported:
            return [{"type": "error", "id": msg_id, "error": "404 no such service or method",
                     "payload": {}}]
        if uri == POWER_STATE:
            return ok(state=self.state)
        if uri == "system/turnOff":  # a toggle, like on real TVs
            self.state = "Active" if self.state == "Active Standby" else "Active Standby"
            return ok()
        if uri.endswith("power/turnOnScreen"):
            self.state = "Active"
            return ok()
        if uri.endswith("power/turnOffScreen"):
            self.state = "Screen Off"
            return ok()
        if uri == "com.webos.applicationManager/getForegroundAppInfo":
            return ok(appId=self.app)
        if uri == "system.launcher/launch":
            self.app = payload["id"]
            return ok()
        if uri == "system/getSystemInfo":
            return ok(modelName="OLED55C1")
        if uri == "system.notifications/createAlert":
            return ok(alertId="com.webos.service.apiadapter-42")
        return ok()

    # --- server side of the WebSocket protocol ---

    def _handshake(self, conn):
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = conn.recv(4096)
            if not chunk:
                raise ConnectionError("closed during handshake")
            data += chunk
        key = re.search(rb"Sec-WebSocket-Key: (\S+)", data).group(1)
        accept = base64.b64encode(hashlib.sha1(key + _ACCEPT_GUID).digest())
        conn.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                     b"Connection: Upgrade\r\nSec-WebSocket-Accept: " + accept + b"\r\n\r\n")

    def _recv(self, conn) -> str:
        b0, b1 = self._read(conn, 2)
        n = b1 & 0x7F
        if n == 126:
            (n,) = struct.unpack("!H", self._read(conn, 2))
        elif n == 127:
            (n,) = struct.unpack("!Q", self._read(conn, 8))
        if not b1 & 0x80:
            raise ConnectionError("client frame not masked")
        key = self._read(conn, 4)
        payload = mask(self._read(conn, n), key)
        if b0 & 0x0F == 0x8:
            raise ConnectionError("client closed")
        return payload.decode()

    @staticmethod
    def _read(conn, n) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = conn.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("closed")
            buf += chunk
        return buf

    @staticmethod
    def _send(conn, msg):
        data = json.dumps(msg).encode()
        conn.sendall(struct.pack("!BB", 0x81, 126) + struct.pack("!H", len(data)) + data
                     if len(data) >= 126 else struct.pack("!BB", 0x81, len(data)) + data)
