"""Scale: the real librouteros client against tools/fakeros.py, and slow routers not blocking each other."""
import asyncio
import importlib.util
import socket
import threading
import time
from pathlib import Path

import pytest

from wispmap_collector.config import Config, Device
from wispmap_collector.runner import (BufferedTransport, Poller, connect_buffered, poll_cycle,
                                     poll_workers, raise_open_files)

spec = importlib.util.spec_from_file_location("fakeros", Path(__file__).parents[1] / "tools" / "fakeros.py")
fakeros = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fakeros)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def fake_port():
    port, ready = free_port(), threading.Event()
    threading.Thread(target=lambda: asyncio.run(fakeros.serve(port, ready.set)), daemon=True).start()
    assert ready.wait(5)
    return port


def cfg(*devices):
    return Config(ping_s=30, fast_s=60, username="wispmon", password="x", devices=list(devices))


def test_real_client_against_the_fake_router(fake_port):
    d = Device(id="r1", host="127.21.0.1", api_port=fake_port)
    p = Poller(cfg(d))
    first = p.poll(d)
    assert first["status"] == "ok" and first["system"]["board_name"] == "RB4011iGS+"
    assert first["system"]["uptime_s"] > 3600 and first["system"]["ros_version"] == "7.16.2"
    assert {i["name"] for i in first["interfaces"]} >= {"ether1", "wlan1", "lo"}
    assert first["routing"]["routes_via"] == {"ether1": 10, "ether2": 10}
    assert [n["state"] for n in first["routing"]["ospf_neighbors"]] == ["full", "full"]
    time.sleep(1.1)
    second = p.poll(d)
    ether1 = next(i for i in second["interfaces"] if i["name"] == "ether1")
    assert ether1["rx_bps"] and ether1["rx_bps"] > 0                     # counters grow: real rates
    p.close()


def test_core_router_routes_and_broken_routers(fake_port):
    core = Device(id="c", host="127.25.0.1", api_port=fake_port)
    dropped = Device(id="d", host="127.24.0.1", api_port=fake_port)
    refused = Device(id="r", host="127.21.0.9", api_port=free_port())    # nothing listens there
    p = Poller(cfg(core, dropped, refused))
    out = {r["device_id"]: r for r in asyncio.run(poll_cycle(p, [core, dropped, refused]))}
    assert sum(out["c"]["routing"]["routes_via"].values()) == 2000
    assert out["d"]["status"] == "error" and out["r"]["status"] == "refused"
    p.close()


def test_slow_routers_do_not_queue_behind_each_other(monkeypatch):
    """100 routers that each take 0.3 s: with Python's default 8 threads that is ~4 s, with the poll pool ~0.5 s."""
    class SlowApi:
        def path(self, *p):
            time.sleep(0.3 if p == ("system", "resource") else 0)
            if p == ("system", "resource"):
                return iter([{"uptime": "1h", "version": "7.16.2", "board-name": "x"}])
            return iter([])

        def close(self):
            pass

    devices = [Device(id=f"d{i}", host=f"10.0.0.{i}") for i in range(100)]
    p = Poller(cfg(*devices), connect=lambda **kw: SlowApi())
    t0 = time.monotonic()
    out = asyncio.run(poll_cycle(p, devices))
    assert len(out) == 100 and all(r["status"] == "ok" for r in out)
    assert time.monotonic() - t0 < 2.0
    p.close()


def test_poll_threads_setting(monkeypatch):
    assert poll_workers() == 64
    monkeypatch.setenv("WISPMAP_POLL_THREADS", "200")
    assert poll_workers() == 200
    monkeypatch.setenv("WISPMAP_POLL_THREADS", "lots")
    assert poll_workers() == 64
    monkeypatch.setenv("WISPMAP_POLL_THREADS", "0")
    assert poll_workers() == 1


def test_open_files_limit_is_raised():
    resource = pytest.importorskip("resource")
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    try:
        resource.setrlimit(resource.RLIMIT_NOFILE, (min(1024, hard), hard))       # the usual default
        got = raise_open_files()
        assert got == min(65536, hard) and got > 1024 or hard <= 1024
    finally:
        resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))


def test_compose_raises_open_files_too():
    text = (Path(__file__).parents[1] / "compose.yaml").read_text()
    assert "nofile: { soft: 65536, hard: 65536 }" in text


def test_buffered_transport_cuts_words_across_chunks():
    from librouteros.exceptions import ConnectionClosed

    class Sock:
        def __init__(self, chunks):
            self.chunks, self.calls = list(chunks), 0

        def recv(self, n):
            self.calls += 1
            return self.chunks.pop(0) if self.chunks else b""

    t = BufferedTransport(Sock([b"abc", b"defgh", b"ij"]))
    assert [t.read(2), t.read(4), t.read(1), t.read(3)] == [b"ab", b"cdef", b"g", b"hij"]
    with pytest.raises(ConnectionClosed):
        t.read(1)
    s = Sock([b"x" * 5000])
    t = BufferedTransport(s)
    assert all(t.read(10) == b"x" * 10 for _ in range(500)) and s.calls == 1     # one recv, 500 words


def test_sessions_use_buffered_reads(fake_port):
    api = connect_buffered(host="127.21.0.3", port=fake_port, username="u", password="p", timeout=5)
    assert isinstance(api.protocol.transport, BufferedTransport)
    assert next(iter(api.path("system", "resource")))["board-name"] == "RB4011iGS+"
    api.close()
