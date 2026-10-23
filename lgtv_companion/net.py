"""Network helpers: Wake-on-LAN, neighbour-table lookups and SSDP discovery of webOS TVs."""

import ipaddress
import logging
import re
import socket
import struct
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

log = logging.getLogger(__name__)

SSDP_ADDR = ("239.255.255.250", 1900)
WEBOS_SSDP_TARGET = "urn:lge-com:service:webos-second-screen:1"


def normalize_mac(mac: str) -> str:
    digits = re.sub(r"[^0-9a-fA-F]", "", mac)
    if len(digits) != 12:
        raise ValueError(f"invalid MAC address: {mac!r}")
    return ":".join(digits[i:i + 2] for i in range(0, 12, 2)).lower()


def magic_packet(mac: str) -> bytes:
    return b"\xff" * 6 + bytes.fromhex(normalize_mac(mac).replace(":", "")) * 16


def subnet_broadcast(ip: str, route_table: str = "/proc/net/route") -> str | None:
    """Broadcast address of the directly connected IPv4 network that contains `ip`, if any."""
    target = ipaddress.IPv4Address(ip)
    try:
        with open(route_table) as f:
            rows = f.read().splitlines()[1:]
    except OSError:
        return None
    best = None
    for row in rows:
        fields = row.split()
        if len(fields) < 8:
            continue
        dest, gateway, mask = (_route_addr(fields[i]) for i in (1, 2, 7))
        is_up = int(fields[3], 16) & 0x1
        # Only on-link routes (no gateway); skip default and host routes.
        if not is_up or gateway != "0.0.0.0" or mask in ("0.0.0.0", "255.255.255.255"):
            continue
        network = ipaddress.IPv4Network(f"{dest}/{mask}", strict=False)
        if target in network and (best is None or network.prefixlen > best.prefixlen):
            best = network
    return str(best.broadcast_address) if best else None


def _route_addr(hex_value: str) -> str:
    # /proc/net/route prints each address as a host-endian integer.
    return socket.inet_ntoa(struct.pack("=I", int(hex_value, 16)))


def send_wol(macs: list[str], ip: str) -> None:
    """Send magic packets to the global broadcast, the TV's subnet broadcast and the TV itself."""
    targets = list(dict.fromkeys(t for t in ("255.255.255.255", subnet_broadcast(ip), ip) if t))
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        for mac in macs:
            packet = magic_packet(mac)
            for target in targets:
                try:
                    sock.sendto(packet, (target, 9))
                except OSError as e:  # e.g. network not up yet right after resume
                    log.debug("WOL packet to %s failed: %s", target, e)


def arp_lookup(ip: str, arp_table: str = "/proc/net/arp") -> str | None:
    """MAC address of `ip` from the kernel's neighbour table, if it has a complete entry."""
    try:
        with open(arp_table) as f:
            rows = f.read().splitlines()[1:]
    except OSError:
        return None
    for row in rows:
        fields = row.split()
        # Columns: IP address, HW type, Flags (0x2 = complete), HW address, Mask, Device
        if len(fields) >= 4 and fields[0] == ip and int(fields[2], 16) & 0x2:
            if fields[3] != "00:00:00:00:00:00":
                return fields[3].lower()
    return None


def discover(timeout: float = 3.0) -> list[dict]:
    """Find webOS TVs on the local network via SSDP. Returns [{"ip", "name", "model"}]."""
    query = (
        "M-SEARCH * HTTP/1.1\r\n"
        f"HOST: {SSDP_ADDR[0]}:{SSDP_ADDR[1]}\r\n"
        'MAN: "ssdp:discover"\r\n'
        "MX: 2\r\n"
        f"ST: {WEBOS_SSDP_TARGET}\r\n"
        "\r\n"
    ).encode()
    locations = {}
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP) as sock:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        for _ in range(2):  # UDP is lossy
            sock.sendto(query, SSDP_ADDR)
        deadline = time.monotonic() + timeout
        while (remaining := deadline - time.monotonic()) > 0:
            sock.settimeout(remaining)
            try:
                data, (addr, _) = sock.recvfrom(65507)
            except TimeoutError:
                break
            headers = _parse_headers(data)
            if headers.get("st") == WEBOS_SSDP_TARGET or "webos" in headers.get("server", "").lower():
                locations.setdefault(addr, headers.get("location", ""))
    tvs = []
    for ip, location in locations.items():
        name, model = _describe(ip, location)
        tvs.append({"ip": ip, "name": name or "LG TV", "model": model or ""})
    return sorted(tvs, key=lambda tv: ipaddress.IPv4Address(tv["ip"]))


def _parse_headers(data: bytes) -> dict:
    headers = {}
    for line in data.decode("latin-1").split("\r\n")[1:]:
        name, sep, value = line.partition(":")
        if sep:
            headers[name.strip().lower()] = value.strip()
    return headers


def _describe(ip: str, location: str) -> tuple[str | None, str | None]:
    """Friendly name and model from the TV's UPnP device description."""
    if urllib.parse.urlparse(location).hostname != ip:
        return None, None
    try:
        with urllib.request.urlopen(location, timeout=2) as resp:
            root = ET.fromstring(resp.read())
    except (OSError, ET.ParseError, ValueError):
        return None, None
    fields = {el.tag.rpartition("}")[2]: (el.text or "").strip() for el in root.iter()}
    return fields.get("friendlyName"), fields.get("modelNumber") or fields.get("modelName")
