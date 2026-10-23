"""TV operations on top of the SSAP client: pairing, power on/off, screen blanking and inputs.

The power logic follows upstream LGTV Companion: always check the TV's power state first,
because "system/turnOff" toggles power on some models rather than only turning it off.
"""

import logging
import threading
import time

from . import net
from .config import Device
from .webos import PairingError, WebOSClient, WebOSError

log = logging.getLogger(__name__)

CONNECT_TIMEOUT = 3.0
RETRY_INTERVAL = 1.0
# Power states in which the TV counts as on ("Screen Off" is on, with the panel blanked).
ON_STATES = {"Active", "Screen Off", "Screen Saver"}

URI_POWER_STATE = "com.webos.service.tvpower/power/getPowerState"
URI_FOREGROUND_APP = "com.webos.applicationManager/getForegroundAppInfo"


def hdmi_app(number: int) -> str:
    return f"com.webos.app.hdmi{number}"


class TV:
    def __init__(self, device: Device):
        self.device = device

    @property
    def name(self) -> str:
        return self.device.name

    def connect(self, timeout: float = CONNECT_TIMEOUT) -> WebOSClient:
        if not self.device.client_key:
            raise PairingError(f"{self.name} is not paired; run `lgtv-companion pair`")
        return WebOSClient.connect(self.device.ip, self.device.client_key,
                                   use_tls=self.device.use_tls, timeout=timeout)

    def power_off(self, forced: bool = True) -> bool:
        """Turn the TV off. Unless `forced`, respects check_input_before_power_off.

        Returns False if the TV was deliberately left on.
        """
        try:
            client = self.connect()
        except PairingError:
            raise
        except (OSError, WebOSError) as e:
            log.info("%s: not reachable (%s); assuming it is already off", self.name, e)
            return True
        with client:
            state, _ = self._power_state(client)
            if state is not None and state not in ON_STATES:
                log.info("%s: already off (%s)", self.name, state)
                return True
            if not forced and not self._showing_pc_input(client):
                return False
            log.info("%s: powering off", self.name)
            try:
                client.request("system/turnOff")
            except OSError as e:  # the TV may drop the connection while it shuts down
                log.debug("%s: connection ended after turnOff: %s", self.name, e)
        return True

    def power_on(self, timeout: float = 40, cancel: threading.Event | None = None) -> bool:
        """Wake the TV and wait until it is on. Returns False on timeout or cancellation."""
        cancel = cancel or threading.Event()
        stop_wol = threading.Event()
        if self.device.macs:
            threading.Thread(target=self._send_wol_until, args=(stop_wol,), daemon=True).start()
        else:
            log.warning("%s: no MAC address configured, so it can only be woken from network standby",
                        self.name)
        deadline = time.monotonic() + timeout
        try:
            while not cancel.is_set() and (remaining := deadline - time.monotonic()) > 0:
                try:
                    with self.connect(timeout=min(CONNECT_TIMEOUT, remaining)) as client:
                        if self._wait_until_on(client, deadline, cancel):
                            log.info("%s: powered on", self.name)
                            self._apply_input(client, cancel)
                            return True
                except PairingError:
                    raise
                except (OSError, WebOSError) as e:
                    log.debug("%s: not ready yet: %s", self.name, e)
                cancel.wait(RETRY_INTERVAL)
        finally:
            stop_wol.set()
        if not cancel.is_set():
            log.warning("%s: did not power on within %ss", self.name, timeout)
        return False

    def screen_off(self) -> bool:
        """Blank the panel while the TV stays on (used for OLED burn-in protection)."""
        with self.connect() as client:
            self._set_screen(client, on=False)
        log.info("%s: screen off", self.name)
        return True

    def screen_on(self) -> bool:
        with self.connect() as client:
            self._set_screen(client, on=True)
        log.info("%s: screen on", self.name)
        return True

    def set_input(self, number: int) -> bool:
        with self.connect() as client:
            client.request("system.launcher/launch", {"id": hdmi_app(number)})
        log.info("%s: switched to HDMI %s", self.name, number)
        return True

    def status(self) -> dict:
        with self.connect() as client:
            state, processing = self._power_state(client)
            try:
                app = client.request(URI_FOREGROUND_APP).get("appId", "")
            except WebOSError:
                app = ""
        return {"state": state or "unknown", "processing": processing or "", "app": app}

    def _power_state(self, client: WebOSClient) -> tuple[str | None, str | None]:
        """("Active", None), ("Screen Off", None), ("Active Standby", None) ... or (None, None)
        if this TV doesn't report it."""
        try:
            body = client.request(URI_POWER_STATE)
        except WebOSError:
            return None, None
        return body.get("state"), body.get("processing")

    def _wait_until_on(self, client: WebOSClient, deadline: float, cancel: threading.Event) -> bool:
        last_toggle = -float("inf")
        while not cancel.is_set() and time.monotonic() < deadline:
            state, processing = self._power_state(client)
            if state == "Screen Off":
                self._set_screen(client, on=True)
                return True
            if state is None or (state in ON_STATES and not processing):
                return True
            # With Quick Start+ the TV stays reachable in standby, and turnOff toggles it back
            # on. Leave time for the state to change so a second toggle doesn't undo the first.
            if state == "Active Standby" and not processing and time.monotonic() - last_toggle > 5:
                log.info("%s: waking from network standby", self.name)
                client.request("system/turnOff")
                last_toggle = time.monotonic()
            cancel.wait(RETRY_INTERVAL)
        return False

    def _set_screen(self, client: WebOSClient, on: bool) -> None:
        method = "turnOnScreen" if on else "turnOffScreen"
        try:
            client.request(f"com.webos.service.tvpower/power/{method}")
        except WebOSError as e:
            # Newer firmware no longer allows this over SSAP; go through luna instead.
            log.debug("%s: SSAP %s failed (%s); retrying via luna", self.name, method, e)
            client.luna_request(f"luna://com.webos.service.tvpower/power/{method}",
                                {"standbyMode": "active"})

    def _showing_pc_input(self, client: WebOSClient) -> bool:
        d = self.device
        if not (d.check_input_before_power_off and d.hdmi_input):
            return True
        try:
            app = client.request(URI_FOREGROUND_APP).get("appId", "")
        except WebOSError as e:
            log.warning("%s: could not read the current input (%s); turning off anyway", self.name, e)
            return True
        if app == hdmi_app(d.hdmi_input):
            return True
        log.info("%s: showing %s rather than HDMI %s; leaving it on",
                 self.name, app or "another app", d.hdmi_input)
        return False

    def _apply_input(self, client: WebOSClient, cancel: threading.Event) -> None:
        d = self.device
        if not (d.set_input_on_power_on and d.hdmi_input):
            return
        if cancel.wait(1.0):  # a TV that has just woken up can ignore an immediate app launch
            return
        try:
            client.request("system.launcher/launch", {"id": hdmi_app(d.hdmi_input)})
            log.info("%s: switched to HDMI %s", self.name, d.hdmi_input)
        except (OSError, WebOSError) as e:
            log.warning("%s: could not switch to HDMI %s: %s", self.name, d.hdmi_input, e)

    def _send_wol_until(self, stop: threading.Event) -> None:
        while not stop.is_set():
            net.send_wol(self.device.macs, self.device.ip)
            stop.wait(1.0)


def pair(ip: str, pairing_timeout: float = 60, on_prompt=None) -> Device:
    """Pair with the TV at `ip` (the user must accept a prompt on the TV) and return its settings.

    Tries TLS on port 3001 first, then plain WebSocket on port 3000 for older firmware.
    """
    error: Exception | None = None
    for use_tls in (True, False):
        try:
            client = WebOSClient.connect(ip, use_tls=use_tls, timeout=5,
                                         pairing_timeout=pairing_timeout, on_prompt=on_prompt)
        except OSError as e:
            log.debug("pairing over %s failed: %s", "TLS" if use_tls else "plain WebSocket", e)
            error = error or e
            continue
        with client:
            device = Device(name="LG TV", ip=ip, client_key=client.client_key, use_tls=use_tls)
            try:
                device.name = client.request("system/getSystemInfo").get("modelName") or device.name
            except WebOSError:
                pass
            try:
                # Same as upstream: switch on "Turn on via Wi-Fi/LAN" so Wake-on-LAN works.
                client.luna_request("luna://com.webos.settingsservice/setSystemSettings",
                                    {"category": "network", "settings": {"wolwowlOnOff": "true"}})
            except (OSError, WebOSError) as e:
                log.warning("Could not enable Wake-on-LAN on the TV (%s); "
                            "turn on \"Turn on via Wi-Fi\" in its settings", e)
        # We just talked to the TV, so the kernel's neighbour table now has its MAC address.
        if mac := net.arp_lookup(ip):
            device.macs = [mac]
        return device
    raise error


def run_on_all(tvs: list[TV], action) -> bool:
    """Run `action(tv)` for every TV in parallel. True if all succeeded (didn't return False)."""
    results = {}

    def run(tv):
        try:
            results[tv.name] = action(tv) is not False
        except (OSError, WebOSError) as e:
            log.error("%s: %s", tv.name, e)
            results[tv.name] = False
        except Exception:
            log.exception("%s: unexpected error", tv.name)
            results[tv.name] = False

    threads = [threading.Thread(target=run, args=(tv,), name=tv.name) for tv in tvs]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return all(results.values())
