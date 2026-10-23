"""Follows the desktop's display power state, so the TVs can go off when the screen blanks.

GNOME's compositor (Mutter) publishes it as the PowerSaveMode property on the user's session
bus. The daemon is a system service, but it runs as the user, so it can connect to that bus.
The bus only exists while the user is logged in, so keep trying, and start over at logout.
"""

import logging
import os

from gi.repository import Gio, GLib

log = logging.getLogger(__name__)

MUTTER = "org.gnome.Mutter.DisplayConfig"
MUTTER_PATH = "/org/gnome/Mutter/DisplayConfig"
MUTTER_IFACE = "org.gnome.Mutter.DisplayConfig"
PROPERTIES_IFACE = "org.freedesktop.DBus.Properties"
DBUS = "org.freedesktop.DBus"
DBUS_PATH = "/org/freedesktop/DBus"

RETRY_SECONDS = 5.0


def session_bus_address() -> str:
    if address := os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
        return address
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return f"unix:path={runtime_dir}/bus"


def connect_session_bus() -> Gio.DBusConnection:
    return Gio.DBusConnection.new_for_address_sync(
        session_bus_address(),
        Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
        None, None)


def _is_on(power_save_mode: int) -> bool | None:
    # 0 on; 1 standby, 2 suspend, 3 off; -1 unknown.
    if power_save_mode == 0:
        return True
    if power_save_mode in (1, 2, 3):
        return False
    return None


class DisplayWatcher:
    """Calls `on_change(True)` when the desktop turns its displays on, `on_change(False)` when
    it turns them off. The state at login (or when the daemon starts) is only recorded."""

    def __init__(self, on_change, connect=connect_session_bus):
        self.on_change = on_change
        self._connect = connect
        self.bus = None
        self.display_on: bool | None = None

    def start(self) -> None:
        self._try_connect()

    def _try_connect(self) -> bool:
        try:
            bus = self._connect()
        except GLib.Error as e:
            log.debug("No session bus (%s); retrying in %ss", e.message, RETRY_SECONDS)
            self._retry()
            return GLib.SOURCE_REMOVE
        bus.set_exit_on_close(False)
        bus.connect("closed", self._on_closed)
        bus.signal_subscribe(MUTTER, PROPERTIES_IFACE, "PropertiesChanged", MUTTER_PATH,
                             MUTTER_IFACE, Gio.DBusSignalFlags.NONE, self._on_properties_changed)
        bus.signal_subscribe(DBUS, DBUS, "NameOwnerChanged", DBUS_PATH, MUTTER,
                             Gio.DBusSignalFlags.NONE, self._on_mutter_owner_changed)
        self.bus = bus
        log.debug("Connected to the session bus")
        self._read_state()
        return GLib.SOURCE_REMOVE

    def _retry(self) -> None:
        GLib.timeout_add(int(RETRY_SECONDS * 1000), self._try_connect)

    def _on_closed(self, _bus, _remote_peer_vanished, _error) -> None:
        log.debug("Session bus closed (logged out?)")
        self.bus = None
        self.display_on = None
        self._retry()

    def _on_mutter_owner_changed(self, _bus, _sender, _path, _iface, _signal, params) -> None:
        _name, _old_owner, new_owner = params.unpack()
        if new_owner:  # GNOME Shell (re)started
            self._read_state()
        else:
            self.display_on = None

    def _read_state(self) -> None:
        try:
            (mode,) = self.bus.call_sync(
                MUTTER, MUTTER_PATH, PROPERTIES_IFACE, "Get",
                GLib.Variant("(ss)", (MUTTER_IFACE, "PowerSaveMode")),
                GLib.VariantType("(v)"), Gio.DBusCallFlags.NO_AUTO_START, 5000, None).unpack()
        except GLib.Error as e:
            log.debug("Display power state not available (%s); it is only followed on GNOME",
                      e.message)
            return
        self.display_on = _is_on(mode)
        log.info("Following the desktop's display power state (now %s)",
                 {True: "on", False: "off", None: "unknown"}[self.display_on])

    def _on_properties_changed(self, _bus, _sender, _path, _iface, _signal, params) -> None:
        _, changed, _ = params.unpack()
        if "PowerSaveMode" not in changed:
            return
        on = _is_on(changed["PowerSaveMode"])
        if on is None or on == self.display_on:
            return
        self.display_on = on
        self.on_change(on)
