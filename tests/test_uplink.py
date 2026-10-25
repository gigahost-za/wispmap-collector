import asyncio
import gzip
import json

import httpx
import pytest

from wispmap_collector.buffer import Buffer
from wispmap_collector.server import ServerClient
from wispmap_collector.uplink import Uplink


class Clock:
    """Fake monotonic clock; the uplink's sleep() moves it forward instantly."""
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    async def sleep(self, s):
        self.t += s


class FakeServer:
    """Plays the server's /ingest. `script` = what to answer for each request (status code or exception)."""
    def __init__(self, *script, config_version=5):
        self.script, self.config_version = list(script), config_version
        self.requests, self.accepted = [], []

    def __call__(self, request: httpx.Request):
        body = json.loads(gzip.decompress(request.content))
        self.requests.append(body)
        action = self.script.pop(0) if self.script else 202
        if isinstance(action, type) and issubclass(action, Exception):
            raise action("simulated", request=request)
        if action == 202:
            self.accepted.append(body)
        return httpx.Response(action, json={"accepted": action == 202, "config_version": self.config_version})


@pytest.fixture
def env(tmp_path):
    def make(*script, **kw):
        server = FakeServer(*script, **kw)
        clock = Clock()
        client = ServerClient("http://server.test", "wmc_test_secret", transport=httpx.MockTransport(server))
        up = Uplink(client, Buffer(tmp_path / "q.sqlite3"), clock=clock, sleep=clock.sleep, rand=lambda: 0.5)
        return up, server, clock
    return make


def ping(i):
    return {"type": "ping", "device_id": f"d{i}", "ts": "2026-10-25T10:00:00+00:00", "sent": 3, "recv": 3}


def poll(i):
    return {"type": "poll", "device_id": f"d{i}", "ts": "2026-10-25T10:00:00+00:00", "status": "ok"}


def queue(up, n):
    """n batches in the buffer, one ping each; returns their batch ids oldest-first."""
    ids = []
    for i in range(n):
        up.add(ping(i))
        ids.append(up.flush())
    return ids


# ---------- building batches ----------

def test_flush_builds_one_gzipped_batch(env, capsys):
    up, _, _ = env()
    up.add(ping(1))
    up.add(poll(1))
    up.add({"type": "collector_error", "ts": "x", "error": "boom"})       # logged, not sent
    batch_id = up.flush()
    item = up.buffer.oldest()
    data = json.loads(gzip.decompress(item.body))
    assert data["schema"] == 1 and data["batch_id"] == batch_id == item.batch_id
    assert len(data["ping"]) == 1 and len(data["polls"]) == 1
    assert "type" not in data["ping"][0] and "type" not in data["polls"][0]
    assert set(data["collector"]) == {"version", "uptime_s", "queue_depth", "dropped_batches"}
    assert "boom" in capsys.readouterr().err
    assert up.flush() is None                                            # nothing new -> no empty batch


# ---------- status handling ----------

def test_accepted_batch_is_deleted_and_config_version_reported(env):
    up, server, _ = env(202, config_version=7)
    seen = []
    up.on_config_version = seen.append
    queue(up, 1)
    asyncio.run(up.drain(29))
    assert up.buffer.depth() == 0 and len(server.accepted) == 1 and seen == [7]


def test_409_duplicate_counts_as_success(env):
    up, _, _ = env(409)
    queue(up, 1)
    asyncio.run(up.drain(29))
    assert up.buffer.depth() == 0


def test_server_error_keeps_batch_and_backs_off(env):
    up, server, clock = env(500, 503, 503)
    queue(up, 1)

    async def scenario():
        await up.drain(29)
        assert up.buffer.depth() == 1 and up.retry_at == clock.t + 2       # first retry after 2 s
        await up.drain(29)
        assert len(server.requests) == 1                                   # too early: nothing sent
        clock.t += 2
        await up.drain(29)
        assert len(server.requests) == 2 and up.backoff_s == 4             # doubles ...
        up.backoff_s = 300
        clock.t = up.retry_at
        await up.drain(29)
        assert up.backoff_s == 300                                         # ... up to 5 min
    asyncio.run(scenario())


def test_network_error_keeps_batch(env):
    up, _, _ = env(httpx.ConnectError)
    queue(up, 1)
    asyncio.run(up.drain(29))
    assert up.buffer.depth() == 1 and up.backoff_s == 2


def test_401_pauses_five_minutes(env, capsys):
    up, _, clock = env(401)
    queue(up, 1)
    asyncio.run(up.drain(29))
    assert up.buffer.depth() == 1 and up.retry_at == clock.t + 300
    assert "WISPMAP_TOKEN" in capsys.readouterr().err


def test_422_drops_poison_batch_and_continues(env):
    up, server, _ = env(422, 202)
    queue(up, 2)
    asyncio.run(up.drain(29))
    assert up.buffer.depth() == 0 and up.rejected == 1 and len(server.accepted) == 1


def test_413_splits_batch_without_losing_samples(env):
    up, server, _ = env(413, 202, 202)
    for item in (ping(1), ping(2), poll(3)):
        up.add(item)
    up.flush()
    asyncio.run(up.drain(29))
    assert len(server.accepted) == 2 and up.buffer.depth() == 0
    got = sorted(s["device_id"] for b in server.accepted for s in b["ping"] + b["polls"])
    assert got == ["d1", "d2", "d3"]


def test_newest_first_then_backlog_oldest_first(env):
    up, server, _ = env()
    b0, b1, b2 = queue(up, 3)
    asyncio.run(up.drain(29))
    assert [r["batch_id"] for r in server.requests] == [b2, b0, b1]


def test_catch_up_is_limited_to_one_per_second(env):
    up, server, _ = env()
    queue(up, 50)
    asyncio.run(up.drain(10))                       # 10 s budget: newest + 9 backlog at 1/s
    assert len(server.accepted) == 10 and up.buffer.depth() == 40


# ---------- the whole point ----------

def test_outage_then_recovery_loses_nothing(env):
    """Server down for 3 cycles: samples wait on disk, then all arrive exactly once."""
    up, server, clock = env(httpx.ConnectError, httpx.ConnectError, httpx.ConnectError)

    async def scenario():
        for i in range(6):
            up.add(ping(i))
            up.flush()
            await up.drain(29)
            clock.t += 30
    asyncio.run(scenario())
    got = [s["device_id"] for b in server.accepted for s in b["ping"]]
    assert sorted(got) == [f"d{i}" for i in range(6)] and up.buffer.depth() == 0


def test_batches_survive_a_collector_restart(env, tmp_path):
    up, _, _ = env(httpx.ConnectError)
    queue(up, 2)
    asyncio.run(up.drain(29))
    up.buffer.close()                                # collector stops while the server is down
    up2, server2, _ = env()                          # same buffer file, server back
    asyncio.run(up2.drain(29))
    assert len(server2.accepted) == 2


def test_token_not_in_client_repr():
    c = ServerClient("http://server.test", "wmc_abc_supersecret")
    assert "supersecret" not in repr(c)
