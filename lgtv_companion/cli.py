"""Command-line interface: `lgtv-companion COMMAND`."""

import argparse
import logging
import os

from . import __version__, net
from .config import Config, default_path
from .tv import TV, pair, run_on_all
from .webos import WebOSError

log = logging.getLogger("lgtv_companion")


def cmd_scan(args, config: Config) -> int:
    print("Searching for LG webOS TVs (the TV must be on)...")
    tvs = net.discover()
    if not tvs:
        log.error("No TVs found. Check the TV is on and on this network, or pair by IP address.")
        return 1
    for tv in tvs:
        print(f"  {tv['ip']:<15}  {tv['name']}  {tv['model']}")
    return 0


def cmd_pair(args, config: Config) -> int:
    ip = args.ip
    if not ip:
        print("Searching for LG webOS TVs (the TV must be on)...")
        tvs = net.discover()
        if len(tvs) != 1:
            for tv in tvs:
                print(f"  {tv['ip']:<15}  {tv['name']}  {tv['model']}")
            log.error("Found %s TVs; run `lgtv-companion pair IP` with the TV's IP address.",
                      "no" if not tvs else len(tvs))
            return 1
        ip = tvs[0]["ip"]

    print(f"Connecting to {ip}...")
    device = pair(ip, on_prompt=lambda: print("Accept the prompt on the TV screen (waiting up to 60 s)..."))

    existing = next((d for d in config.devices if d.ip == ip), None)
    if existing:  # re-pairing keeps the TV's other options
        existing.client_key, existing.use_tls = device.client_key, device.use_tls
        existing.macs = list(dict.fromkeys(existing.macs + device.macs))  # Wi-Fi and Ethernet
        existing.name = args.name or existing.name
        device = existing
    else:
        device.name = _unique_name(config, args.name or device.name)
        config.devices.append(device)
    config.save()

    print(f"Paired with {device.name} ({device.ip}).")
    if device.macs:
        print(f"  MAC address: {', '.join(device.macs)}")
    else:
        log.warning("Could not find the TV's MAC address, so it can't be woken from full standby. "
                    "Add it as \"macs\": [\"aa:bb:cc:dd:ee:ff\"] in %s", config.path)
    print(f"  Settings saved to {config.path}")
    return 0


def _unique_name(config: Config, name: str) -> str:
    taken = {d.name for d in config.devices}
    candidate, n = name, 2
    while candidate in taken:
        candidate, n = f"{name} {n}", n + 1
    return candidate


def cmd_list(args, config: Config) -> int:
    if not config.devices:
        print(f"No paired TVs (config: {config.path}).")
        return 0
    for d in config.devices:
        notes = [", ".join(d.macs) or "no MAC", "TLS" if d.use_tls else "no TLS"]
        if d.hdmi_input:
            notes.append(f"PC on HDMI {d.hdmi_input}")
        if not d.enabled:
            notes.append("automatic power control disabled")
        print(f"{d.name}  {d.ip}  ({'; '.join(notes)})")
    return 0


def cmd_remove(args, config: Config) -> int:
    matches = config.select(args.name)
    if not matches:
        log.error("No TV named %r", args.name)
        return 1
    config.devices = [d for d in config.devices if d not in matches]
    config.save()
    print(f"Removed {', '.join(d.name for d in matches)}.")
    return 0


def _print_status(tv: TV) -> None:
    s = tv.status()
    line = f"{tv.name} ({tv.device.ip}): {s['state']}"
    if s["processing"]:
        line += f" ({s['processing']})"
    if s["app"]:
        line += f", showing {s['app']}"
    print(line)


DEVICE_ACTIONS = {
    "on": ("power on (Wake-on-LAN)", lambda tv, args, config: tv.power_on(config.power_on_timeout)),
    "off": ("power off", lambda tv, args, config: tv.power_off()),
    "screen-off": ("blank the screen; the TV stays on", lambda tv, args, config: tv.screen_off()),
    "screen-on": ("unblank the screen", lambda tv, args, config: tv.screen_on()),
    "input": ("switch to an HDMI input", lambda tv, args, config: tv.set_input(args.number)),
    "status": ("show power state and current input", lambda tv, args, config: _print_status(tv)),
}


def cmd_device_action(args, config: Config) -> int:
    devices = config.select(args.device)
    if not devices:
        if args.device:
            log.error("No TV named %r", args.device)
        else:
            log.error("No paired TVs; run `lgtv-companion pair` first")
        return 1
    action = DEVICE_ACTIONS[args.command][1]
    ok = run_on_all([TV(d) for d in devices], lambda tv: action(tv, args, config))
    return 0 if ok else 1


def cmd_daemon(args, config: Config) -> int:
    try:
        from .daemon import Daemon
    except ImportError as e:
        log.error("The daemon needs PyGObject (%s); install it with: sudo apt install python3-gi", e)
        return 1
    Daemon(config.path).run()
    return 0


class _CliFormatter(logging.Formatter):
    def format(self, record):
        msg = super().format(record)
        return msg if record.levelno <= logging.INFO else f"{record.levelname.lower()}: {msg}"


def _stderr_is_journal() -> bool:
    # systemd sets JOURNAL_STREAM to the device:inode of the journal stream it connected.
    try:
        dev, ino = os.environ["JOURNAL_STREAM"].split(":")
        st = os.fstat(2)
        return (st.st_dev, st.st_ino) == (int(dev), int(ino))
    except (KeyError, ValueError, OSError):
        return False


def _setup_logging(verbose: bool, daemon: bool) -> None:
    handler = logging.StreamHandler()
    if not daemon:
        handler.setFormatter(_CliFormatter("%(message)s"))
    elif _stderr_is_journal():  # journald adds its own timestamps
        handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s: %(message)s"))
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, handlers=[handler])


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lgtv-companion",
        description="Keep LG webOS TVs' power in sync with this PC (Linux port of LGTV Companion).")
    parser.add_argument("--config", default=default_path(), help="config file (default: %(default)s)")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    sub.add_parser("scan", help="find LG TVs on the local network").set_defaults(func=cmd_scan)
    p = sub.add_parser("pair", help="pair with a TV (accept the prompt on the TV)")
    p.add_argument("ip", nargs="?", help="the TV's IP address (default: find it automatically)")
    p.add_argument("--name", help="name to refer to this TV by (default: its model)")
    p.set_defaults(func=cmd_pair)
    sub.add_parser("list", help="show paired TVs").set_defaults(func=cmd_list)
    p = sub.add_parser("remove", help="forget a paired TV")
    p.add_argument("name", help="TV name or IP address")
    p.set_defaults(func=cmd_remove)

    for command, (help_text, _) in DEVICE_ACTIONS.items():
        p = sub.add_parser(command, help=help_text)
        if command == "input":
            p.add_argument("number", type=int, choices=range(1, 5), metavar="N", help="HDMI input 1-4")
        p.add_argument("-d", "--device", metavar="NAME",
                       help="TV name or IP address (default: all enabled TVs)")
        p.set_defaults(func=cmd_device_action)

    sub.add_parser("daemon", help="follow suspend/resume/shutdown (normally run by systemd)"
                   ).set_defaults(func=cmd_daemon)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _setup_logging(args.verbose, args.command == "daemon")
    try:
        config = Config.load(args.config)
    except ValueError as e:
        log.error("%s", e)
        return 1
    try:
        return args.func(args, config)
    except (OSError, WebOSError) as e:
        log.error("%s", e)
        return 1
    except KeyboardInterrupt:
        return 130
