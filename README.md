# LGTV Companion for Linux

A Linux port of [LGTV Companion](https://github.com/JPersson77/LGTVCompanion): it keeps an LG
webOS TV (used as a PC monitor) powered on and off together with your PC.

This first version covers the core feature set:

| When the PC...           | the TV...                                              |
|--------------------------|--------------------------------------------------------|
| boots                    | is powered on (Wake-on-LAN), before anyone logs in     |
| suspends / hibernates    | is powered off                                         |
| resumes                  | is powered on                                          |
| shuts down               | is powered off                                         |
| reboots                  | is left alone                                          |

Plus a command line for manual control (`on`, `off`, `screen-off`, `input 2`, ...), multiple
TVs, and the optional "only turn the TV off if it is showing the PC's HDMI input" and "switch
to the PC's input on power-on" settings.

Not ported yet: blanking the screen when the user is idle or the desktop turns the display off,
remote-streaming detection, multi-monitor topology, and the GUI.

## Requirements

- systemd. Version 255 or newer is needed to tell a reboot from a shutdown. On older versions
  the TV is also turned off for a reboot and turned back on when the PC comes up.
- Python 3.10+ and PyGObject (`python3-gi`), which Ubuntu desktop includes. No pip packages.
- The TV must be on the same subnet as the PC, because Wake-on-LAN is a broadcast.

## TV setup

1. Enable **Turn on via Wi-Fi**. This is needed even when the TV uses a network cable.
   - C1–C4 (2021+): *All Settings → General → Devices → External Devices → TV On With
     Mobile → Turn on via Wi-Fi*
   - CX (2020): *All Settings → Connection → Mobile Connection Management → TV On with Mobile*

   Pairing also tries to switch this setting on for you.
2. If the TV is on Wi-Fi, also enable **Always Ready** (2022+) or **Quick Start+** (2021 and
   older) so that its network card stays awake in standby.

## Install

With the TV switched on:

```sh
python3 -m lgtv_companion scan     # optional: find TVs on the network
python3 -m lgtv_companion pair     # or: pair 192.168.0.50 — then accept the prompt on the TV
sudo ./install.sh                  # installs /usr/local/bin/lgtv-companion and starts the service
```

`install.sh` installs a systemd **system** service, `lgtv-companion@<you>.service`, that runs
as your user and reads your `~/.config/lgtv-companion/config.json`. It is a system service so
that it can turn the TV on at boot, before the login screen. You don't need to restart it after
pairing or editing the config.

Remove it with `sudo ./install.sh --uninstall`.

## Usage

```
lgtv-companion scan                    find LG TVs on the local network
lgtv-companion pair [IP] [--name N]    pair with a TV
lgtv-companion list                    show paired TVs
lgtv-companion remove NAME             forget a TV
lgtv-companion on|off                  power on / off
lgtv-companion screen-off|screen-on    blank / unblank the panel (the TV stays on)
lgtv-companion input N                 switch to HDMI N
lgtv-companion status                  power state and current input
lgtv-companion daemon                  the service itself (normally started by systemd)
```

Device commands act on every enabled TV, or on the one picked with `-d NAME` (or `-d IP`).
Add `-v` for protocol-level debug output.

Logs from the service: `journalctl -u lgtv-companion@$USER -f`

## Configuration

`~/.config/lgtv-companion/config.json` is written by `pair` and can be edited by hand:

```json
{
  "power_on_timeout": 40,
  "devices": [
    {
      "name": "OLED55C1PUB",
      "ip": "192.168.0.50",
      "macs": ["aa:bb:cc:dd:ee:ff"],
      "client_key": "…",
      "use_tls": true,
      "enabled": true,
      "hdmi_input": null,
      "set_input_on_power_on": false,
      "check_input_before_power_off": false
    }
  ]
}
```

| Setting | Meaning |
|---|---|
| `power_on_timeout` | How many seconds to keep sending Wake-on-LAN and polling while turning a TV on. |
| `macs` | MAC address(es) for Wake-on-LAN. `pair` fills this in. Add the other one if the TV switches between Wi-Fi and Ethernet. |
| `use_tls` | Use the encrypted port 3001. Firmware from 2022 onwards requires it; `pair` falls back to port 3000 for older TVs. |
| `enabled` | `false` excludes the TV from automatic power control and from commands run without `-d`. |
| `hdmi_input` | The HDMI input (1–4) that the PC is connected to. The two options below use it. |
| `set_input_on_power_on` | Switch to `hdmi_input` after turning the TV on. |
| `check_input_before_power_off` | On sleep/shutdown, turn the TV off only if it is showing `hdmi_input`. This avoids switching it off while someone is watching something else. Manual `off` ignores this. |

The file has mode 0600, because anyone who has a client key can control the TV.

## How it works

- **Power events** come from systemd-logind over D-Bus (`PrepareForSleep` and
  `PrepareForShutdownWithMetadata`). The daemon holds *delay* inhibitor locks, so suspend and
  shutdown wait until the TV has been told to turn off (logind caps the wait at
  `InhibitDelayMaxSec`). Run `systemd-inhibit --list` to see them.
- If something shuts the system down without going through logind, the daemon notices when
  systemd stops it and turns the TV off then. A reboot is still skipped in that case.
- **Talking to the TV** uses the webOS "second screen" WebSocket API (SSAP). This repo contains
  a small WebSocket client, so no third-party libraries are needed. The registration manifest
  and request URIs are the ones upstream LGTV Companion uses.
- **Power off** checks the TV's power state first. On some models `system/turnOff` is a
  toggle, so sending it to a TV in standby would turn the TV *on*.
- **Power on** sends Wake-on-LAN every second to 255.255.255.255, to the TV's subnet
  broadcast address and to the TV's own IP. Meanwhile it polls the TV until the TV reports
  `Active`. The polling also handles a TV that is still reachable in standby (power is
  toggled) or one whose screen is blanked (it is unblanked).

## Development

```sh
python3 -m unittest            # runs against an in-process fake TV and a fake logind
python3 -m lgtv_companion -v status
```

## Credits

Behaviour and protocol details come from [LGTV Companion](https://github.com/JPersson77/LGTVCompanion)
by Jörgen Persson (MIT License).
