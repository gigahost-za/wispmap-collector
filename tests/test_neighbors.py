import asyncio
import gzip
import json
import threading
import time

from tests.fakes import FakeApi
from wispmap_collector.config import Config, Device
from wispmap_collector.routeros import poll_neighbors
from wispmap_collector.runner import Poller, neighbor_cycle


class ListApi:
    def __init__(self, rows):
        self.rows = rows

    def path(self, *parts):
        return iter(self.rows)

    def close(self):
        pass


def test_neighbors_from_real_chr_capture():
    rows = poll_neighbors(FakeApi("chr"))
    twr = next(r for r in rows if r["interface"] == "ether2")
    assert twr == {"interface": "ether2", "mac": "08:00:27:a1:33:24", "address": "10.0.1.2",
                   "identity": "CHR-TWR-A", "platform": "MikroTik", "version": "7.23.8", "board": "CHR",
                   "remote_interface": "ether1", "discovered_by": "cdp,lldp,mndp"}
    lan = [r for r in rows if r["interface"] == "ether1"]
    assert len(lan) == 2 and all(r["identity"] is None and r["address"] is None for r in lan)  # LLDP-only: low confidence


def test_bridged_port_takes_first_interface_and_empty_strings_become_none():
    rows = poll_neighbors(ListApi([{"interface": "ether2,bridge1", "mac-address": "AA:BB:CC:00:00:01", "identity": "",
                                    "version": "", "address": ""}]))
    assert rows == [{"interface": "ether2", "mac": "aa:bb:cc:00:00:01", "address": None, "identity": None,
                     "platform": None, "version": None, "board": None, "remote_interface": None, "discovered_by": None}]


def cfg(*devices):
    return Config(ping_s=30, fast_s=60, username="u", password="p", devices=list(devices))


def test_neighbor_cycle_skips_unreadable_devices():
    good, bad = Device("good", "10.0.0.1"), Device("bad", "10.0.0.2")

    def connect(host, **kw):
        if host == "10.0.0.2":
            raise TimeoutError("timed out")
        return FakeApi("chr")
    p = Poller(cfg(good, bad), connect=connect)
    out = asyncio.run(neighbor_cycle(p, [good, bad]))
    assert [r["device_id"] for r in out] == ["good"]
    assert out[0]["type"] == "neighbors" and len(out[0]["neighbors"]) == 3
    assert "bad" not in p.sessions


def test_poll_and_neighbor_poll_never_share_a_session_at_the_same_time():
    """librouteros sessions aren't thread-safe: the per-device lock must serialise them."""
    active, overlaps = [0], []

    class SlowApi(FakeApi):
        def path(self, *parts):
            active[0] += 1
            if active[0] > 1:
                overlaps.append(parts)
            time.sleep(0.02)
            active[0] -= 1
            return super().path(*parts)
    d = Device("x", "10.0.0.1")
    p = Poller(cfg(d), connect=lambda **kw: SlowApi("chr"))
    threads = [threading.Thread(target=p.poll, args=(d,)) for _ in range(3)] + \
              [threading.Thread(target=p.poll_neighbors, args=(d,)) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert overlaps == []


def test_uplink_sends_neighbors(tmp_path):
    from wispmap_collector.buffer import Buffer
    from wispmap_collector.uplink import Uplink
    up = Uplink(client=None, buffer=Buffer(tmp_path / "q.sqlite3"))
    up.add({"type": "neighbors", "device_id": "d1", "ts": "t", "neighbors": [{"interface": "ether2"}]})
    assert up.flush() is not None                                  # neighbors alone are worth a batch
    data = json.loads(gzip.decompress(up.buffer.oldest().body))
    assert data["neighbors"] == [{"device_id": "d1", "ts": "t", "neighbors": [{"interface": "ether2"}]}]


def test_neighbors_interval_from_server_config(tmp_path):
    from wispmap_collector.sync import ConfigSync
    c = cfg()
    ConfigSync(client=None, cfg=c, cache_path=tmp_path / "c.json").apply(
        {"config_version": 1, "intervals": {"neighbors_s": 600}, "devices": []})
    assert c.neighbors_s == 600
