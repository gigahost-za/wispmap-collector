"""Config sync: hello + GET /config from the server, with a local cache (spec doc 03 §4).

The device list now comes from the server. The last good config is cached on disk, so a collector
that restarts while the server (or the internet link) is down keeps monitoring the same devices.
"""
import asyncio
import json
import os
import platform
import socket
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

from .common import VERSION, log
from .config import Config, ConfigError, Device

HELLO_EVERY_S = 6 * 3600
CHECK_EVERY_S = 60
MAX_SKEW_S = 30
_DEVICE_FIELDS = {"id", "name", "kind", "host", "api_port", "api_tls", "credential_profile", "ping", "poll"}


def device_from_server(d: dict) -> Device | None:
    """Server JSON -> Device. Unknown keys are ignored (a newer server may send more);
    a device of an unknown kind is skipped with a warning instead of breaking the whole config."""
    try:
        return Device(**{k: v for k, v in d.items() if k in _DEVICE_FIELDS})
    except (ConfigError, TypeError) as exc:
        log(f"skipping device {d.get('name') or d.get('id')}: {exc}")
        return None


class ConfigSync:
    def __init__(self, client, cfg: Config, cache_path: str | Path, clock=time.monotonic):
        self.client, self.cfg, self.cache_path, self.clock = client, cfg, Path(cache_path), clock
        self.etag: str | None = None
        self.version: int | None = None
        self.last_hello: float | None = None
        self.wake = asyncio.Event()
        self.problem: str | None = None

    # ----- applying -----
    def apply(self, body: dict) -> None:
        devices = [d for d in map(device_from_server, body.get("devices", [])) if d]
        intervals = body.get("intervals", {})
        self.cfg.ping_s = intervals.get("ping_s", self.cfg.ping_s)
        self.cfg.fast_s = intervals.get("fast_s", self.cfg.fast_s)
        self.cfg.push_s = intervals.get("push_s", self.cfg.push_s)
        self.cfg.neighbors_s = intervals.get("neighbors_s", self.cfg.neighbors_s)
        self.cfg.devices = devices        # swap the whole list: the loops pick it up on their next cycle
        self.version = body.get("config_version")
        polled = sum(d.poll for d in devices)
        log(f"config v{self.version}: {len(devices)} devices ({polled} polled), "
            f"ping {self.cfg.ping_s}s, poll {self.cfg.fast_s}s, push {self.cfg.push_s}s")

    def _write_cache(self, body: dict) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.cache_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(body))
        os.replace(tmp, self.cache_path)          # atomic: a crash never leaves half a file

    def load_cache(self) -> bool:
        try:
            body = json.loads(self.cache_path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return False
        log(f"server unreachable: using cached config from {self.cache_path}")
        self.apply(body)
        return True

    # ----- talking to the server -----
    async def hello(self) -> None:
        resp = await self.client.hello({
            "version": VERSION, "hostname": socket.gethostname(), "arch": platform.machine(),
            "platform": platform.system().lower(), "capabilities": ["ping", "routeros_api"],
            "credential_profiles": ["default"],
        })
        self.last_hello = self.clock()
        server_time = datetime.fromisoformat(resp["server_time"])
        skew = abs((datetime.now(timezone.utc) - server_time).total_seconds())
        if skew > MAX_SKEW_S:
            log(f"WARNING: clock differs from the server by {skew:.0f}s: fix NTP (sample times come from here)")
        log(f"connected to {self.client.http.base_url.host} as a collector of {resp['org_name']!r}")

    async def refresh(self) -> bool:
        """GET /config. True if a new config was applied, False on 304 (unchanged)."""
        body, etag = await self.client.config(self.etag)
        if body is None:
            return False
        self.apply(body)
        self.etag = etag
        self._write_cache(body)
        return True

    def notify(self, server_version: int) -> None:
        """Called by the uplink with config_version from each ingest reply: re-fetch now if it moved."""
        if server_version != self.version:
            self.wake.set()

    async def startup(self) -> None:
        try:
            await self.hello()
            await self.refresh()
        except httpx.HTTPError as exc:
            self._report(f"startup: {_describe(exc)}")
            if not self.load_cache():
                log("no cached config yet: monitoring nothing until the server answers")

    async def run(self) -> None:
        while True:
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=CHECK_EVERY_S)
            except TimeoutError:
                pass
            self.wake.clear()
            try:
                if self.last_hello is None or self.clock() - self.last_hello > HELLO_EVERY_S:
                    await self.hello()
                await self.refresh()
                self.problem = None
            except httpx.HTTPError as exc:
                self._report(f"config: {_describe(exc)}")

    def _report(self, why: str) -> None:
        if why != self.problem:
            log(why)
            self.problem = why


def _describe(exc: httpx.HTTPError) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        hint = ": check WISPMAP_TOKEN" if code in (401, 403) else ""
        return f"server answered HTTP {code}{hint}"
    return f"server unreachable ({type(exc).__name__})"
