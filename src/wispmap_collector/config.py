"""Collector configuration: device list + credentials (.env).

Server mode gets the device list from the server's `GET /config` (see sync.py).
Local mode (no WISPMAP_URL/WISPMAP_TOKEN, or --local) reads it from devices.toml.
"""
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

KINDS = {"routeros", "ping_only"}


class ConfigError(ValueError):
    """The configuration is invalid; the message says what to fix."""


@dataclass
class Device:
    id: str
    host: str
    kind: str = "routeros"
    api_port: int = 8728
    ping: bool = True
    poll: bool = True
    name: str = ""
    api_tls: bool = False
    credential_profile: str = "default"

    def __post_init__(self):
        if self.kind not in KINDS:
            raise ConfigError(f"device {self.id!r}: unknown kind {self.kind!r} (use one of {sorted(KINDS)})")
        if self.kind == "ping_only":
            self.poll = False            # nothing to poll over the API


@dataclass
class Config:
    ping_s: int
    fast_s: int
    username: str
    password: str = field(repr=False)    # never shows up in print()/logs
    devices: list[Device] = field(default_factory=list)
    push_s: int = 30                     # how often the uplink sends a batch (server mode)


def load_credentials() -> tuple[str, str]:
    """The 'default' credential profile: the RouterOS monitoring user from .env (never sent to the server)."""
    try:
        return os.environ["WISPMON_USER"], os.environ["WISPMON_PASSWORD"]
    except KeyError as missing:
        raise ConfigError(f"{missing.args[0]} is not set (put it in .env)") from None


def load_config(path: str | Path = "devices.toml", env_file: str | Path = ".env") -> Config:
    load_dotenv(env_file)                # explicit path: find_dotenv() magic breaks under stdin/Docker
    with open(path, "rb") as f:          # tomllib wants bytes
        raw = tomllib.load(f)

    username, password = load_credentials()

    devices = []
    for d in raw.get("devices", []):
        try:
            devices.append(Device(**d))
        except TypeError as e:           # unknown or missing keys in a [[devices]] entry
            raise ConfigError(f"bad device entry {d}: {e}") from None

    ids = [d.id for d in devices]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        raise ConfigError(f"duplicate device id(s): {dupes}")

    intervals = raw.get("intervals", {})
    return Config(
        ping_s=intervals.get("ping_s", 30),
        fast_s=intervals.get("fast_s", 60),
        username=username,
        password=password,
        devices=devices,
    )
