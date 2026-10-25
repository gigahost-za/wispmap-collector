import asyncio
import errno
import json

import pytest
from librouteros.exceptions import MultiTrapError, TrapError

from tests.fakes import FakeApi
from wispmap_collector import runner
from wispmap_collector.config import Config, Device
from wispmap_collector.runner import Poller, classify, emit, ping_cycle, poll_cycle


def make_cfg(*devices):
    return Config(ping_s=30, fast_s=60, username="u", password="p", devices=list(devices))


class Clock:
    """A fake time.monotonic() the test can move forward."""
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


class GrowingApi(FakeApi):
    """Real CHR fixture data, but every /interface read adds `step` bytes to rx-byte."""
    def __init__(self, step=0):
        super().__init__("chr")
        self.step, self.reads, self.closed = step, 0, False

    def path(self, *parts):
        rows = list(super().path(*parts))
        if parts == ("interface",):
            for r in rows:
                if r.get("rx-byte") is not None:
                    r["rx-byte"] += self.step * self.reads
            self.reads += 1
        return iter(rows)

    def close(self):
        self.closed = True


class BrokenApi(GrowingApi):
    def path(self, *parts):
        raise ConnectionResetError(errno.ECONNRESET, "Connection reset by peer")


# ---------- classify ----------

@pytest.mark.parametrize("exc,expected", [
    (TimeoutError("timed out"), "timeout"),
    (ConnectionRefusedError(errno.ECONNREFUSED, "Connection refused"), "refused"),
    (TrapError("invalid user name or password (6)"), "auth_failed"),
    (MultiTrapError(TrapError("invalid user name or password (6)"), TrapError("x")), "auth_failed"),
    (OSError(errno.EHOSTUNREACH, "No route to host"), "unreachable"),
    (TrapError("not enough permissions (9)"), "error"),
    (ValueError("boom"), "error"),
])
def test_classify(exc, expected):
    assert classify(exc) == expected


# ---------- Poller ----------

def test_poll_ok_then_rates_and_session_reuse():
    api, connects, clock = GrowingApi(step=7500), [], Clock()

    def fake_connect(**kw):
        connects.append(kw)
        return api

    d = Device("core", "10.255.255.1")
    p = Poller(make_cfg(d), connect=fake_connect, clock=clock)

    r1 = p.poll(d)
    assert r1["status"] == "ok" and r1["error"] is None
    assert r1["system"]["ros_version"] == "7.23.8"
    e1 = {i["name"]: i for i in r1["interfaces"]}["ether1"]
    assert e1["rx_bps"] is None                          # first reading: no rate yet

    clock.t = 60.0
    r2 = p.poll(d)
    e1 = {i["name"]: i for i in r2["interfaces"]}["ether1"]
    assert e1["rx_bps"] == 1000.0                        # 7500 bytes * 8 / 60 s
    assert e1["tx_bps"] == 0.0                           # tx counter didn't move

    assert len(connects) == 1                            # one session, reused
    assert connects[0]["host"] == "10.255.255.1" and connects[0]["password"] == "p"


def test_poll_output_shape():
    d = Device("core", "h")
    r = Poller(make_cfg(d), connect=lambda **kw: GrowingApi(), clock=Clock()).poll(d)
    assert set(r) == {"type", "device_id", "ts", "cadence", "status", "error", "duration_ms",
                      "system", "interfaces"}
    assert set(r["interfaces"][0]) == {"name", "type", "mac", "running", "disabled", "link_downs",
                                       "rx_bps", "tx_bps", "rx_pps", "tx_pps", "rx_err_ps",
                                       "tx_err_ps", "rx_drop_ps", "tx_drop_ps"}


def test_connect_failure_then_reconnect():
    calls = {"n": 0}

    def flaky(**kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise TimeoutError("timed out")
        return GrowingApi()

    d = Device("core", "h")
    p = Poller(make_cfg(d), connect=flaky, clock=Clock())
    r1 = p.poll(d)
    assert r1["status"] == "timeout" and r1["error"] == "timed out"
    assert "system" not in r1                            # error results carry no data
    assert p.poll(d)["status"] == "ok"                   # next cycle reconnects by itself
    assert calls["n"] == 2


def test_broken_session_is_closed_and_dropped():
    api = BrokenApi()
    d = Device("core", "h")
    p = Poller(make_cfg(d), connect=lambda **kw: api, clock=Clock())
    r = p.poll(d)
    assert r["status"] == "error" and "reset" in r["error"]
    assert api.closed is True and p.sessions == {}


def test_poll_cycle_polls_all_devices_concurrently():
    a, b = Device("a", "h1"), Device("b", "h2")
    p = Poller(make_cfg(a, b), connect=lambda **kw: GrowingApi(), clock=Clock())
    out = asyncio.run(poll_cycle(p, [a, b]))
    assert [r["device_id"] for r in out] == ["a", "b"]
    assert all(r["status"] == "ok" for r in out)


def test_session_renewed_after_max_age():
    connects, clock = [], Clock()

    def fake_connect(**kw):
        connects.append(clock.t)
        return GrowingApi()

    d = Device("core", "h")
    p = Poller(make_cfg(d), connect=fake_connect, clock=clock, session_max_age_s=3600)
    p.poll(d)
    clock.t = 1800.0
    p.poll(d)
    assert connects == [0.0]                             # still young: reused
    clock.t = 3700.0
    p.poll(d)
    assert connects == [0.0, 3700.0]                     # too old: fresh login


def test_renewal_surfaces_changed_password():
    """Lab finding: RouterOS keeps old sessions after a password change; only a new login fails."""
    clock, logins = Clock(), {"n": 0}

    def connect(**kw):
        logins["n"] += 1
        if logins["n"] > 1:
            raise TrapError("invalid user name or password (6)")
        return GrowingApi()

    d = Device("core", "h")
    p = Poller(make_cfg(d), connect=connect, clock=clock, session_max_age_s=3600)
    assert p.poll(d)["status"] == "ok"
    clock.t = 600.0
    assert p.poll(d)["status"] == "ok"                   # old session hides the change
    clock.t = 4000.0
    assert p.poll(d)["status"] == "auth_failed"          # renewal reveals it


# ---------- ping ----------

def test_ping_cycle(monkeypatch):
    async def fake_run_ping(hosts, **kw):
        assert hosts == ["10.0.0.1", "10.0.0.2"]         # ping=False device is not pinged
        return {"10.0.0.1": {"sent": 3, "recv": 3, "rtt_min_ms": 1.0, "rtt_avg_ms": 1.5, "rtt_max_ms": 2.0}}

    monkeypatch.setattr(runner, "run_ping", fake_run_ping)
    cfg = make_cfg(Device("a", "10.0.0.1"), Device("b", "10.0.0.2"), Device("c", "10.0.0.3", ping=False))
    out = asyncio.run(ping_cycle(cfg))
    assert [r["device_id"] for r in out] == ["a", "b"]
    assert out[0]["recv"] == 3
    assert out[1]["recv"] == 0 and out[1]["rtt_avg_ms"] is None     # missing from fping = total loss
    assert all(r["type"] == "ping" and r["ts"].endswith("+00:00") for r in out)


# ---------- emit ----------

def test_emit_writes_one_json_line(capsys):
    emit({"type": "ping", "device_id": "a"})
    out = capsys.readouterr().out
    assert out.endswith("\n") and out.count("\n") == 1
    assert json.loads(out) == {"type": "ping", "device_id": "a"}
