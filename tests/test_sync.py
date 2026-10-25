import asyncio
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from tests.fakes import FakeApi
from wispmap_collector.config import Config, Device
from wispmap_collector.runner import Poller
from wispmap_collector.server import ServerClient
from wispmap_collector.sync import ConfigSync, device_from_server

SERVER_DEVICES = [
    {"id": "u1", "name": "CHR-CORE", "kind": "routeros", "host": "10.255.255.1", "api_port": 8728,
     "api_tls": False, "credential_profile": "default", "ping": True, "poll": True, "added_in_v9": "x"},
    {"id": "u2", "name": "gw", "kind": "ping_only", "host": "192.168.1.254", "api_port": 8728,
     "api_tls": False, "credential_profile": "default", "ping": True, "poll": True},
]


class FakeConfigServer:
    def __init__(self, devices=SERVER_DEVICES, version=5, down=False, status=200, server_time=None):
        self.devices, self.version, self.down, self.status = devices, version, down, status
        self.server_time = server_time
        self.seen_etags = []

    def __call__(self, request):
        if self.down:
            raise httpx.ConnectError("down", request=request)
        if self.status != 200:
            return httpx.Response(self.status, json={"detail": "nope"})
        if request.url.path.endswith("/hello"):
            st = self.server_time or datetime.now(timezone.utc)
            return httpx.Response(200, json={"collector_id": "c1", "org_name": "Gigahost WISP",
                                             "server_time": st.isoformat(), "config_version": self.version})
        etag = f'"{self.version}"'
        self.seen_etags.append(request.headers.get("If-None-Match"))
        if request.headers.get("If-None-Match") == etag:
            return httpx.Response(304, headers={"ETag": etag})
        return httpx.Response(200, headers={"ETag": etag}, json={
            "config_version": self.version, "intervals": {"ping_s": 10, "fast_s": 20, "push_s": 15},
            "devices": self.devices})


@pytest.fixture
def make(tmp_path):
    def _make(**kw):
        server = FakeConfigServer(**kw)
        client = ServerClient("http://server.test", "wmc_t_s", transport=httpx.MockTransport(server))
        cfg = Config(ping_s=30, fast_s=60, username="u", password="p")
        return ConfigSync(client, cfg, tmp_path / "config.json"), server, cfg
    return _make


def test_device_from_server_ignores_unknown_keys_and_applies_rules():
    core, gw = (device_from_server(d) for d in SERVER_DEVICES)
    assert (core.id, core.name, core.host) == ("u1", "CHR-CORE", "10.255.255.1")
    assert gw.poll is False                                    # ping_only -> nothing to poll


def test_unknown_kind_is_skipped_not_fatal(capsys):
    assert device_from_server({"id": "x", "name": "sw", "kind": "swos", "host": "10.0.0.9"}) is None
    assert "skipping device sw" in capsys.readouterr().err


def test_startup_applies_config_and_writes_cache(make, tmp_path):
    sync, _, cfg = make()
    asyncio.run(sync.startup())
    assert [d.name for d in cfg.devices] == ["CHR-CORE", "gw"]
    assert (cfg.ping_s, cfg.fast_s, cfg.push_s, sync.version) == (10, 20, 15, 5)
    assert json.loads((tmp_path / "config.json").read_text())["config_version"] == 5


def test_refresh_sends_etag_and_304_changes_nothing(make):
    sync, server, cfg = make()

    async def scenario():
        await sync.startup()
        before = cfg.devices
        assert await sync.refresh() is False                    # 304
        assert cfg.devices is before and server.seen_etags[-1] == '"5"'
        server.version, server.devices = 6, SERVER_DEVICES[:1]
        assert await sync.refresh() is True                     # new version -> new device list
        assert [d.name for d in cfg.devices] == ["CHR-CORE"]
    asyncio.run(scenario())


def test_server_down_at_startup_uses_cached_config(make):
    sync, _, _ = make()
    asyncio.run(sync.startup())                                 # first run: caches v5
    sync2, _, cfg2 = make(down=True)
    asyncio.run(sync2.startup())
    assert [d.name for d in cfg2.devices] == ["CHR-CORE", "gw"]


def test_server_down_and_no_cache_monitors_nothing_but_runs(make, capsys):
    sync, _, cfg = make(down=True)
    asyncio.run(sync.startup())
    assert cfg.devices == [] and "no cached config" in capsys.readouterr().err


def test_bad_token_at_startup_says_so(make, capsys):
    sync, _, _ = make(status=401)
    asyncio.run(sync.startup())
    assert "WISPMAP_TOKEN" in capsys.readouterr().err


def test_clock_skew_warning(make, capsys):
    sync, _, _ = make(server_time=datetime.now(timezone.utc) - timedelta(minutes=5))
    asyncio.run(sync.hello())
    assert "clock differs" in capsys.readouterr().err


def test_notify_wakes_only_on_a_new_version(make):
    sync, _, _ = make()
    asyncio.run(sync.startup())
    sync.notify(5)
    assert not sync.wake.is_set()
    sync.notify(6)
    assert sync.wake.is_set()


# ---------- poller side of a reload ----------

def test_poller_sync_drops_sessions_of_removed_or_moved_devices():
    opened = []

    def connect(**kw):
        api = FakeApi("chr")
        opened.append(kw["host"])
        return api
    a, b, c = Device("a", "10.0.0.1"), Device("b", "10.0.0.2"), Device("c", "10.0.0.3")
    p = Poller(Config(ping_s=30, fast_s=60, username="u", password="p", devices=[a, b, c]), connect=connect)
    for d in (a, b, c):
        p.poll(d)
    b_moved = Device("b", "10.0.0.22")
    p.sync([a, b_moved])                                        # c removed, b's address changed
    assert set(p.sessions) == {"a"}


def test_unknown_credential_profile_is_a_poll_error():
    d = Device("x", "10.0.0.1", credential_profile="legacy-v6")
    p = Poller(Config(ping_s=30, fast_s=60, username="u", password="p", devices=[d]),
               connect=lambda **kw: FakeApi("chr"))
    r = p.poll(d)
    assert r["status"] == "error" and "unknown credential profile 'legacy-v6'" in r["error"]
