"""Background service: follows systemd-logind power events and drives the TVs to match.

    boot, resume             -> power the TVs on
    suspend, shutdown        -> power the TVs off
    reboot                   -> leave the TVs alone

logind "delay" inhibitor locks hold suspend and shutdown back (for at most
InhibitDelayMaxSec) until the TVs have been told to turn off.
"""

import logging
import os
import signal
import threading
from pathlib import Path

from gi.repository import Gio, GLib

from .config import Config
from .tv import TV, run_on_all

log = logging.getLogger(__name__)

LOGIND = "org.freedesktop.login1"
LOGIND_PATH = "/org/freedesktop/login1"
LOGIND_IFACE = "org.freedesktop.login1.Manager"
SYSTEMD = "org.freedesktop.systemd1"
SYSTEMD_PATH = "/org/freedesktop/systemd1"
SYSTEMD_IFACE = "org.freedesktop.systemd1.Manager"

REBOOT_TYPES = {"reboot", "kexec", "soft-reboot"}
REBOOT_TARGETS = {"reboot.target", "kexec.target", "soft-reboot.target"}


class Daemon:
    def __init__(self, config_path: Path, bus: Gio.DBusConnection | None = None,
                 state_dir: Path | None = None):
        self.config_path = config_path
        self.bus = bus or Gio.bus_get_sync(Gio.BusType.SYSTEM)
        self.state_dir = state_dir or Path(
            os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "lgtv-companion"
        self.loop = GLib.MainLoop()
        self.locks: dict[str, int] = {}  # "sleep"/"shutdown" -> inhibitor fd
        self.preparing: set[str] = set()  # "sleep"/"shutdown" while logind is waiting for us
        self.shutdown_announced = False
        self._busy = threading.Lock()
        self._cancel = threading.Event()

    def run(self) -> None:
        self.start()
        self.loop.run()

    def start(self) -> None:
        # systemd >= 255 also says whether a shutdown is a reboot; older versions don't.
        shutdown_signal = ("PrepareForShutdownWithMetadata"
                           if self._logind_has_signal("PrepareForShutdownWithMetadata")
                           else "PrepareForShutdown")
        for name in ("PrepareForSleep", shutdown_signal):
            self.bus.signal_subscribe(LOGIND, LOGIND_IFACE, name, LOGIND_PATH, None,
                                      Gio.DBusSignalFlags.NONE, self._on_logind_signal)
        self._inhibit("sleep")
        self._inhibit("shutdown")
        for signum in (signal.SIGTERM, signal.SIGINT):
            GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signum, self._on_stop)

        devices = self._config().select()
        if devices:
            log.info("Managing %s", ", ".join(f"{d.name} ({d.ip})" for d in devices))
        else:
            log.warning("No paired TVs yet; run `lgtv-companion pair` (no restart needed)")
        if self._first_start_this_boot():
            self._dispatch("boot", self._power_on_all)

    # --- event handling -------------------------------------------------------------------

    def _on_logind_signal(self, _conn, _sender, _path, _iface, signal_name, params) -> None:
        args = params.unpack()
        starting = args[0]
        if signal_name == "PrepareForSleep":
            if starting:
                self.preparing.add("sleep")
                self._dispatch("suspend", self._power_off_all, release="sleep")
            else:
                self.preparing.discard("sleep")
                self._inhibit("sleep")
                self._dispatch("resume", self._power_on_all)
        elif starting:
            self.shutdown_announced = True
            self.preparing.add("shutdown")
            kind = args[1].get("type", "") if len(args) > 1 else ""
            if kind in REBOOT_TYPES:
                log.info("Event: %s; leaving TVs as they are", kind)
                self._release("shutdown")
            else:
                self._dispatch(f"shutdown ({kind or 'type unknown'})", self._power_off_all,
                               release="shutdown")
        else:
            # logind aborted the shutdown after announcing it.
            self.shutdown_announced = False
            self.preparing.discard("shutdown")
            self._inhibit("shutdown")
            self._dispatch("shutdown cancelled", self._power_on_all)

    def _on_stop(self) -> bool:
        # Shutdowns that bypass logind (e.g. `systemctl start poweroff.target`) aren't
        # announced, so check whether this stop is part of one, like upstream's "unsure" event.
        if not self.shutdown_announced and self._system_is_powering_off():
            log.info("Event: unannounced shutdown")
            self._cancel.set()
            with self._busy:
                self._power_off_all(threading.Event())
        self.loop.quit()
        return GLib.SOURCE_REMOVE

    def _dispatch(self, event: str, action, release: str | None = None) -> None:
        """Run `action` on a worker thread. A newer event cancels an older one still running
        (e.g. resuming while still trying to wake the TV); `release` is the inhibitor lock to
        drop once the action is done."""
        log.info("Event: %s", event)
        self._cancel.set()
        cancel = self._cancel = threading.Event()

        def work():
            try:
                with self._busy:
                    if not cancel.is_set():
                        action(cancel)
            except Exception:
                log.exception("Handling %s failed", event)
            finally:
                if release:
                    GLib.idle_add(self._done_preparing, release)

        threading.Thread(target=work, name=event, daemon=True).start()

    def _config(self) -> Config:
        # Re-read on every event so edits and new pairings apply without a restart.
        return Config.load(self.config_path)

    def _power_off_all(self, cancel: threading.Event) -> None:
        run_on_all([TV(d) for d in self._config().select()], lambda tv: tv.power_off(forced=False))

    def _power_on_all(self, cancel: threading.Event) -> None:
        config = self._config()
        run_on_all([TV(d) for d in config.select()],
                   lambda tv: tv.power_on(config.power_on_timeout, cancel))

    # --- logind / systemd plumbing ---------------------------------------------------------

    def _inhibit(self, what: str) -> None:
        if what in self.locks:
            return
        try:
            reply, fd_list = self.bus.call_with_unix_fd_list_sync(
                LOGIND, LOGIND_PATH, LOGIND_IFACE, "Inhibit",
                GLib.Variant("(ssss)", (what, "LGTV Companion", f"Turn off TVs before {what}", "delay")),
                GLib.VariantType("(h)"), Gio.DBusCallFlags.NONE, -1, None, None)
        except GLib.Error as e:
            log.warning("Could not take the %s inhibitor lock (%s); the TVs may not get turned "
                        "off before %s", what, e.message, what)
            return
        fds = fd_list.steal_fds()
        (index,) = reply.unpack()
        for i, fd in enumerate(fds):
            if i == index:
                self.locks[what] = fd
            else:
                os.close(fd)

    def _done_preparing(self, what: str) -> None:
        """The TVs have been dealt with: let the suspend/shutdown go ahead."""
        self._release(what)
        # If we took longer than logind waits, the system may already be back up; the lock
        # is needed again for next time.
        if what not in self.preparing:
            self._inhibit(what)

    def _release(self, what: str) -> None:
        fd = self.locks.pop(what, None)
        if fd is not None:
            os.close(fd)

    def _logind_has_signal(self, name: str) -> bool:
        try:
            (xml,) = self.bus.call_sync(
                LOGIND, LOGIND_PATH, "org.freedesktop.DBus.Introspectable", "Introspect", None,
                GLib.VariantType("(s)"), Gio.DBusCallFlags.NONE, -1, None).unpack()
        except GLib.Error:
            return False
        iface = Gio.DBusNodeInfo.new_for_xml(xml).lookup_interface(LOGIND_IFACE)
        return iface is not None and iface.lookup_signal(name) is not None

    def _system_is_powering_off(self) -> bool:
        try:
            (state,) = self.bus.call_sync(
                SYSTEMD, SYSTEMD_PATH, "org.freedesktop.DBus.Properties", "Get",
                GLib.Variant("(ss)", (SYSTEMD_IFACE, "SystemState")),
                GLib.VariantType("(v)"), Gio.DBusCallFlags.NONE, -1, None).unpack()
            if state != "stopping":
                return False
            (jobs,) = self.bus.call_sync(
                SYSTEMD, SYSTEMD_PATH, SYSTEMD_IFACE, "ListJobs", None,
                GLib.VariantType("(a(usssoo))"), Gio.DBusCallFlags.NONE, -1, None).unpack()
        except GLib.Error as e:
            log.debug("Could not query systemd state: %s", e.message)
            return False
        return not {unit for _, unit, *_ in jobs} & REBOOT_TARGETS

    def _first_start_this_boot(self) -> bool:
        """True only for the first start after each boot, so `systemctl restart` doesn't wake the TV."""
        try:
            boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        except OSError:
            return True
        marker = self.state_dir / "last_boot_id"
        try:
            if marker.read_text().strip() == boot_id:
                return False
        except OSError:
            pass
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            marker.write_text(boot_id + "\n")
        except OSError as e:
            log.warning("Could not write %s: %s", marker, e)
        return True
