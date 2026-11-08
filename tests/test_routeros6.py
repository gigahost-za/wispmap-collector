"""Every read against real RouterOS 6.49.23 captures (Part 11c lab): until now v6 support was built from the manual."""
from tests.fakes import FakeApi
from wispmap_collector.bgp import poll_bgp
from wispmap_collector.health import poll_health
from wispmap_collector.routeros import poll_interfaces, poll_system
from wispmap_collector.routing import poll_default_route, poll_routing
from wispmap_collector.wireless import poll_wireless


def test_v6_system_and_interfaces():
    api = FakeApi("chr-v6/twr-c")
    s = poll_system(api)
    assert s["ros_version"] == "6.49.23" and s["board_name"] == "CHR"
    assert {i["name"] for i in poll_interfaces(api)} >= {"ether1", "ether2", "lo"}
    assert poll_health(api) is None and poll_wireless(api) is None


def test_v6_tower_routing_counts_routes_per_interface():
    r = poll_routing(FakeApi("chr-v6/twr-c"))
    assert {i["interface"]: (i["state"], i["passive"]) for i in r["ospf_interfaces"]} == {
        "ether1": ("point-to-point", False), "ether2": ("point-to-point", False), "lo": ("passive", True)}
    assert {n["router_id"] for n in r["ospf_neighbors"]} == {"10.255.255.2", "10.255.255.6"}
    assert r["routes_via"].get("ether1", 0) > 0 and r["routes_via"].get("ether2", 0) > 0     # was {} before 0.7.0
    assert r["default_route"] == {"gateway": "10.0.5.1", "interface": "ether1", "protocol": "ospf"}


def test_v6_pop_bgp_and_default_route():
    api = FakeApi("chr-v6/pop3")
    peers = {p["name"]: p for p in poll_bgp(api)["peers"]}
    up, ib = peers["upstream-c"], peers["ibgp-core"]
    assert (up["state"], up["remote_as"], up["local_as"], up["ebgp"], up["routes"], up["routes_active"]) == \
        ("established", 64520, 64496, True, 66, 66)
    assert (ib["ebgp"], ib["remote_address"], ib["routes"]) == (False, "10.255.255.1", 257)
    assert poll_default_route(api) == {"gateway": "10.0.12.1", "interface": "ether2", "protocol": "bgp"}
