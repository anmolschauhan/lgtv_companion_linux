"""Client for the webOS "second screen" API (SSAP) that LG TVs serve on ports 3001 (TLS) and 3000."""

import itertools
import json
import logging
import time

from .websocket import WebSocket

log = logging.getLogger(__name__)

PORT_TLS = 3001
PORT_PLAIN = 3000

# The unsigned manifest upstream LGTV Companion registers with (LG_HANDSHAKE_*_V3).
PERMISSIONS = [
    "LAUNCH", "LAUNCH_WEBAPP", "APP_TO_APP", "CLOSE", "TEST_OPEN", "TEST_PROTECTED",
    "CONTROL_AUDIO", "CONTROL_DISPLAY", "CONTROL_INPUT_JOYSTICK", "CONTROL_INPUT_MEDIA_RECORDING",
    "CONTROL_INPUT_MEDIA_PLAYBACK", "CONTROL_INPUT_TV", "CONTROL_POWER", "CONTROL_TV_SCREEN",
    "READ_APP_STATUS", "READ_CURRENT_CHANNEL", "READ_INPUT_DEVICE_LIST", "READ_NETWORK_STATE",
    "READ_RUNNING_APPS", "READ_TV_CHANNEL_LIST", "WRITE_NOTIFICATION_TOAST", "READ_POWER_STATE",
    "READ_COUNTRY_INFO", "READ_SETTINGS", "CONTROL_INPUT_TEXT", "CONTROL_MOUSE_AND_KEYBOARD",
    "WRITE_SETTINGS", "WRITE_NOTIFICATION_ALERT", "READ_INSTALLED_APPS", "READ_UPDATE_INFO",
]


class WebOSError(Exception):
    """The TV rejected a request or sent something unexpected."""


class PairingError(WebOSError):
    """Pairing was rejected, timed out, or the stored client key is no longer valid."""


class WebOSClient:
    """A registered SSAP session with one TV. Use as a context manager."""

    def __init__(self, ws: WebSocket, timeout: float):
        self._ws = ws
        self.timeout = timeout
        self.client_key = ""
        self._ids = itertools.count(1)

    @classmethod
    def connect(cls, host: str, client_key: str | None = None, *, use_tls: bool = True,
                timeout: float = 5.0, pairing_timeout: float | None = None,
                on_prompt=None) -> "WebOSClient":
        """Connect and register.

        Without `pairing_timeout`, an invalid or missing client key fails with PairingError
        after `timeout`. With it, the pairing prompt on the TV is waited for (calling
        `on_prompt` once it is shown) and the new key ends up in `client.client_key`.
        """
        port = PORT_TLS if use_tls else PORT_PLAIN
        client = cls(WebSocket.connect(host, port, use_tls=use_tls, timeout=timeout), timeout)
        try:
            client.client_key = client._register(client_key, pairing_timeout, on_prompt)
        except BaseException:
            client.close()
            raise
        return client

    def request(self, uri: str, payload: dict | None = None) -> dict:
        """Call an SSAP endpoint (e.g. "system/turnOff") and return the response payload."""
        if "://" not in uri:
            uri = "ssap://" + uri
        req_id = f"req_{next(self._ids)}"
        self._send({"type": "request", "id": req_id, "uri": uri, "payload": payload or {}})
        deadline = time.monotonic() + self.timeout
        while True:
            msg = self._recv(deadline)
            if msg.get("id") != req_id:
                continue
            body = msg.get("payload") or {}
            if msg.get("type") == "error" or body.get("returnValue") is False:
                raise WebOSError(f"{uri} failed: {msg.get('error') or body.get('errorText') or body}")
            return body

    def luna_request(self, uri: str, params: dict) -> None:
        """Call a luna:// service that SSAP doesn't expose.

        An alert's close action can call any luna service, so create an alert that calls `uri`
        and dismiss it straight away (the same trick upstream LGTV Companion uses).
        """
        action = {"uri": uri, "params": params}
        alert = self.request("system.notifications/createAlert", {
            "message": " ",
            "buttons": [{"label": "", "onClick": uri, "params": params}],
            "onclose": action,
            "onfail": action,
        })
        if alert_id := alert.get("alertId"):
            self.request("system.notifications/closeAlert", {"alertId": alert_id})

    def close(self) -> None:
        self._ws.close()

    def __enter__(self) -> "WebOSClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _register(self, client_key, pairing_timeout, on_prompt) -> str:
        payload = {
            "forcePairing": False,
            "pairingType": "PROMPT",
            "manifest": {"manifestVersion": 1, "permissions": PERMISSIONS},
        }
        if client_key:
            payload["client-key"] = client_key
        self._send({"type": "register", "id": "register_0", "payload": payload})

        deadline = time.monotonic() + self.timeout
        prompted = False
        while True:
            try:
                msg = self._recv(deadline)
            except TimeoutError:
                if prompted:
                    raise PairingError("the pairing prompt on the TV was not accepted in time; "
                                       "run `lgtv-companion pair` to pair again") from None
                raise
            if msg.get("id") != "register_0":
                continue
            kind, body = msg.get("type"), msg.get("payload") or {}
            if kind == "registered":
                if not body.get("client-key"):
                    raise WebOSError("the TV did not return a client key")
                return body["client-key"]
            if kind == "error":
                raise PairingError(f"pairing rejected: {msg.get('error') or 'cancelled on the TV'}")
            if kind == "response" and body.get("pairingType") == "PROMPT" and not prompted:
                # The TV only prompts when it doesn't recognise the key.
                prompted = True
                if pairing_timeout is not None:
                    if on_prompt:
                        on_prompt()
                    deadline = time.monotonic() + pairing_timeout

    def _send(self, msg: dict) -> None:
        text = json.dumps(msg)
        log.debug("> %s", msg["type"] if msg["type"] == "register" else text)
        self._ws.send(text)

    def _recv(self, deadline: float) -> dict:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("timed out waiting for the TV")
        self._ws.settimeout(remaining)
        raw = self._ws.recv()
        log.debug("< %s", raw)
        try:
            msg = json.loads(raw)
        except ValueError:
            raise WebOSError(f"invalid JSON from TV: {raw[:200]!r}") from None
        return msg if isinstance(msg, dict) else {}
