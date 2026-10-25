"""Routing (OSPF) polling: real CHR 7.23 capture (CHR-CORE, L3 disabled), v6 shapes, failure handling."""
import asyncio

from librouteros.exceptions import TrapError

from tests.fakes import FakeApi
from wispmap_collector.config import Config, Device
from wispmap_collector.routing import poll_routing, route_interface
from wispmap_collector.runner import Poller, poll_cycle


class ListApi:
    def __init__(self, tables, fail=None):
        self.tables, self.fail = tables, fail

    def path(self, *parts):
        name = "/".join(parts)
        if name == self.fail:
            raise TrapError("no such command or directory (routing)")
        return iter(self.tables.get(name, []))

    def close(self):
        pass


def test_routing_from_real_chr_capture():
    r = poll_routing(FakeApi("chr"))
    assert {i["interface"]: (i["cost"], i["state"], i["passive"]) for i in r["ospf_interfaces"]} == {
        "lo": (1, "passive", True), "ether1": (1, "passive", True), "ether2": (1, "ptp", False)}
    assert r["ospf_neighbors"] == [{"interface": "ether2", "address": "10.0.1.2", "router_id": "10.255.255.2",
                                    "state": "full", "state_changes": 5, "adjacency_s": 2420}]
    assert r["routes_via"] == {"ether2": 3}                  # inactive and connected routes are not counted


def test_route_interface_v6_and_v7():
    assert route_interface({"immediate-gw": "10.0.1.2%ether2"}) == "ether2"
    assert route_interface({"gateway": "10.0.1.2%sfp1"}) == "sfp1"
    assert route_interface({"gateway": "10.0.1.2", "gateway-status": "10.0.1.2 reachable via  ether3"}) == "ether3"
    assert route_interface({"gateway": "lo"}) is None


def test_non_full_neighbour_is_reported_as_is():
    r = poll_routing(ListApi({"routing/ospf/interface": [{"interface": "ether3", "cost": 100, "state": "ptp"}],
                              "routing/ospf/neighbor": [{"interface": "ether3", "state": "ExStart",
                                                         "state-changes": 41}]}))
    assert r["ospf_neighbors"][0]["state"] == "exstart" and r["ospf_neighbors"][0]["state_changes"] == 41
    assert r["routes_via"] == {}


def cfg(*devices):
    return Config(ping_s=30, fast_s=60, username="u", password="p", devices=list(devices))


def test_poll_includes_routing():
    d = Device("core", "10.255.255.1")
    out = asyncio.run(poll_cycle(Poller(cfg(d), connect=lambda **kw: FakeApi("chr")), [d]))
    assert out[0]["status"] == "ok" and out[0]["routing"]["routes_via"] == {"ether2": 3}


def test_router_without_ospf_menu_still_polls_ok_and_keeps_session():
    d = Device("v6", "10.0.0.6")
    api = FakeApi("chr")
    original = api.path

    def path(*parts):
        if parts[0] == "routing":
            raise TrapError("no such command or directory (routing)")
        return original(*parts)
    api.path = path
    p = Poller(cfg(d), connect=lambda **kw: api)
    out = asyncio.run(poll_cycle(p, [d]))
    assert out[0]["status"] == "ok" and out[0]["routing"] is None and "v6" in p.sessions


def test_routing_read_timeout_drops_session_but_poll_counts():
    d = Device("r", "10.0.0.7")
    api = FakeApi("chr")
    original = api.path

    def path(*parts):
        if parts[0] == "routing":
            raise TimeoutError("timed out")
        return original(*parts)
    api.path = path
    p = Poller(cfg(d), connect=lambda **kw: api)
    out = asyncio.run(poll_cycle(p, [d]))
    assert out[0]["status"] == "ok" and out[0]["routing"] is None and "r" not in p.sessions
