"""BGP polling: real CHR 7.23 captures from the Part 11b lab (two POPs, two upstreams, iBGP), v6 shapes."""
import json
from pathlib import Path

import pytest
from librouteros.exceptions import TrapError

from wispmap_collector.bgp import count_routes, poll_bgp
from wispmap_collector.routing import poll_default_route

CAPS = Path(__file__).parent / "fixtures/chr/bgp"


class CaptureApi:
    """One router in one captured lab state; no rawCmd, so route counts come from the captured BGP routes."""

    def __init__(self, state, who, v6=None):
        self.d, self.who, self.v6 = (CAPS / state) if state else None, who, v6

    def path(self, *parts):
        name = "/".join(parts)
        if self.v6 is not None:
            if name not in self.v6:
                raise TrapError("no such command prefix")
            return iter(self.v6[name])
        files = {"routing/bgp/connection": "bgp_connection", "routing/bgp/instance": "bgp_instance",
                 "routing/bgp/session": "bgp_session", "ip/route": "ip_route_bgp"}
        return iter(json.loads((self.d / f"{self.who}_{files[name]}.json").read_text()))


def peers(state, who):
    return {p["name"]: p for p in poll_bgp(CaptureApi(state, who))["peers"]}


def test_baseline_both_pops():
    core = peers("0-baseline", "core")
    a, i = core["upstream-a"], core["ibgp-pop2"]
    assert (a["established"], a["state"], a["remote_as"], a["local_as"], a["ebgp"]) == (True, "established", 64500, 64496, True)
    assert (a["routes"], a["routes_active"], a["prefix_count"]) == (258, 258, 258)     # default + 257
    assert a["remote_address"] == "10.0.10.1" and a["uptime_s"] == 339 and a["last_stopped"] is None
    assert (i["ebgp"], i["routes"], i["routes_active"]) == (False, 129, 0)    # POP2's routes: known, not used
    pop2 = peers("0-baseline", "pop2")
    assert (pop2["upstream-b"]["remote_as"], pop2["upstream-b"]["routes"]) == (64510, 130)


def test_upstream_down_keeps_a_row_with_when_it_stopped():
    a = peers("1-upstream-a-down", "core")["upstream-a"]
    assert (a["established"], a["state"], a["uptime_s"]) == (False, "down", None)
    assert a["last_stopped"] == "2026-11-08 05:59:49" and "routes" not in a      # nothing counted while down


def test_routes_are_counted_not_taken_from_the_lagging_counter():
    b = peers("4-upstream-b-fewer-prefixes", "pop2")["upstream-b"]
    assert b["prefix_count"] == 130 and b["routes"] == 2       # RouterOS still said 130 after the cut to 2


def test_disabled_connection_has_no_session_but_still_a_row():
    core = peers("5-ibgp-down", "core")
    assert core["ibgp-pop2"]["state"] == "disabled" and core["ibgp-pop2"]["disabled"] is True
    assert peers("5-ibgp-down", "pop2")["ibgp-core"]["state"] == "down"      # the other end just sees it drop


def test_no_bgp_configured_is_none():
    api = CaptureApi(None, None, v6=None)
    api.path = lambda *p: iter([])
    assert poll_bgp(api) is None


def test_no_bgp_menu_raises_for_the_caller():
    with pytest.raises(TrapError):
        poll_bgp(CaptureApi(None, None, v6={}))


def test_routeros6_peers():
    v6 = {"routing/bgp/instance": [{"name": "default", "as": "65530", "router-id": "10.0.0.1"}],
          "routing/bgp/peer": [
              {"name": "transit", "instance": "default", "remote-address": "196.0.0.1", "remote-as": "37100",
               "state": "established", "uptime": "3d4h5m6s", "prefix-count": "2", "disabled": "false"},
              {"name": "ix", "instance": "default", "remote-address": "196.60.8.1", "remote-as": "65000",
               "state": "active", "prefix-count": "0", "disabled": "false"},
              {"name": "old", "instance": "default", "remote-address": "10.9.9.9", "remote-as": "65530",
               "state": "idle", "disabled": "true"}],
          "ip/route": [{"dst-address": "0.0.0.0/0", "gateway": "196.0.0.1", "bgp": True, "active": True},
                       {"dst-address": "8.8.8.0/24", "gateway": "196.0.0.1", "bgp": True, "active": False}]}
    p = {r["name"]: r for r in poll_bgp(CaptureApi(None, None, v6=v6))["peers"]}
    t = p["transit"]
    assert (t["established"], t["local_as"], t["ebgp"], t["uptime_s"], t["routes"], t["routes_active"]) == \
        (True, 65530, True, 273906, 2, 1)
    assert (p["ix"]["state"], p["ix"]["established"]) == ("active", False)
    assert (p["old"]["state"], p["old"]["ebgp"]) == ("disabled", False)


def test_count_uses_one_count_only_query_per_peer():
    class Raw:
        def __init__(self):
            self.calls = []

        def rawCmd(self, cmd, *words):
            self.calls.append((cmd, words))
            return iter([{"ret": 1000000}])
    api = Raw()
    assert count_routes(api, "196.0.0.1", active_only=True) == 1000000          # a full table: one number back
    assert api.calls == [("/ip/route/print", ("=count-only=", "?bgp=true", "?gateway=196.0.0.1", "?active=true"))]


class RouteApi:
    def __init__(self, rows):
        self.rows = rows

    def path(self, *parts):
        assert parts == ("ip", "route")
        return iter(self.rows)


def test_default_route_of_a_tower_and_a_pop():
    tower = RouteApi([{"dst-address": "0.0.0.0/0", "gateway": "10.0.1.1%ether1", "immediate-gw": "10.0.1.1%ether1",
                       "ospf": True, "active": True},
                      {"dst-address": "0.0.0.0/0", "gateway": "10.0.2.2%ether2", "ospf": True, "active": False}])
    assert poll_default_route(tower) == {"gateway": "10.0.1.1", "interface": "ether1", "protocol": "ospf"}
    pop = RouteApi([{"dst-address": "0.0.0.0/0", "gateway": "10.0.10.1", "immediate-gw": "10.0.10.1%ether4",
                     "bgp": True, "active": True, "distance": 20}])
    assert poll_default_route(pop) == {"gateway": "10.0.10.1", "interface": "ether4", "protocol": "bgp"}
    assert poll_default_route(RouteApi([])) is None
    v6 = RouteApi([{"dst-address": "0.0.0.0/0", "gateway": "192.168.88.1", "gateway-status":
                    "192.168.88.1 reachable via  ether1", "static": True, "active": True}])
    assert poll_default_route(v6) == {"gateway": "192.168.88.1", "interface": "ether1", "protocol": "static"}
