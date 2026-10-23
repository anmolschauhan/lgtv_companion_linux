"""Settings: the paired TVs and their options, stored as JSON in ~/.config/lgtv-companion/."""

import dataclasses
import json
import os
from dataclasses import dataclass, field
from pathlib import Path


def default_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(base) / "lgtv-companion" / "config.json"


@dataclass
class Device:
    name: str
    ip: str
    macs: list[str] = field(default_factory=list)
    client_key: str = ""
    use_tls: bool = True
    # Set to false to keep a paired TV out of automatic power management.
    enabled: bool = True
    # The TV's HDMI input (1-4) that this PC is plugged into; used by the two options below.
    hdmi_input: int | None = None
    # Switch the TV to `hdmi_input` after powering it on.
    set_input_on_power_on: bool = False
    # On sleep/shutdown, only turn the TV off if it is showing `hdmi_input`.
    check_input_before_power_off: bool = False


@dataclass
class Config:
    devices: list[Device] = field(default_factory=list)
    # How long to keep trying to wake a TV, in seconds.
    power_on_timeout: int = 40
    path: Path = field(default_factory=default_path, compare=False)

    @classmethod
    def load(cls, path: str | Path | None = None) -> "Config":
        path = Path(path) if path else default_path()
        try:
            data = json.loads(path.read_text())
        except FileNotFoundError:
            return cls(path=path)
        except ValueError as e:
            raise ValueError(f"{path}: invalid JSON: {e}") from None
        known = {f.name for f in dataclasses.fields(Device)}
        try:
            devices = [Device(**{k: v for k, v in d.items() if k in known})
                       for d in data.get("devices", [])]
        except TypeError as e:
            raise ValueError(f"{path}: invalid device entry: {e}") from None
        return cls(devices=devices, power_on_timeout=int(data.get("power_on_timeout", 40)), path=path)

    def save(self) -> None:
        data = {
            "power_on_timeout": self.power_on_timeout,
            "devices": [dataclasses.asdict(d) for d in self.devices],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        # 0600: the client keys let anyone on the network control the TV.
        with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        os.replace(tmp, self.path)

    def select(self, name: str | None = None) -> list[Device]:
        """The TV called `name` (or with that IP), or every enabled TV if no name is given."""
        if name is None:
            return [d for d in self.devices if d.enabled]
        return [d for d in self.devices if name in (d.name, d.ip)]
