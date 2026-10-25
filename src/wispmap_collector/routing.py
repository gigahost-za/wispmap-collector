"""Routing state of a RouterOS device: OSPF interfaces, OSPF neighbours, and which interfaces active routes use.

Only small, filtered reads: a core router with full BGP tables must not send its whole /ip/route every minute,
so routes are counted per outgoing interface on the router side of the query (active OSPF routes only).
"""
from .parsing import parse_duration

try:                                       # librouteros >= 3: server-side filtered queries
    from librouteros.query import Key
except ImportError:                        # pragma: no cover
    Key = None

MAX_ROUTES = 50000                         # counting stops here (a WISP's OSPF table is far smaller)


def route_interface(r: dict) -> str | None:
    """Outgoing interface of a route: v7 "10.0.1.2%ether2"; v6 gateway-status "10.0.1.2 reachable via  ether2"."""
    for key in ("immediate-gw", "gateway"):
        gw = r.get(key) or ""
        if "%" in gw:
            return gw.rsplit("%", 1)[1] or None
    status = r.get("gateway-status") or ""
    if " via " in status:
        return status.rsplit(" via ", 1)[1].strip().split(",")[0] or None
    return None


def _active_ospf_routes(api):
    path = api.path("ip", "route")
    if Key is not None and hasattr(path, "select"):
        return path.select(Key("dst-address"), Key("gateway"), Key("immediate-gw"), Key("gateway-status")) \
                   .where(Key("active") == True, Key("ospf") == True)                      # noqa: E712
    return (r for r in path if r.get("active") and r.get("ospf"))   # test fakes / libraries without queries


def poll_routing(api) -> dict:
    """{"ospf_interfaces": [...], "ospf_neighbors": [...], "routes_via": {iface: count}}.
    Raises librouteros TrapError/MultiTrapError when the router has no such menu (caller decides)."""
    interfaces = []
    for i in api.path("routing", "ospf", "interface"):
        if not i.get("interface"):
            continue
        interfaces.append({
            "interface": i["interface"],
            "cost": i.get("cost"),
            "state": (i.get("state") or "").lower() or None,          # ptp / dr / backup / passive / down …
            "passive": i.get("state") == "passive" or bool(i.get("passive")),
        })
    neighbors = []
    for n in api.path("routing", "ospf", "neighbor"):
        neighbors.append({
            "interface": (n.get("interface") or "").split(",")[0] or None,
            "address": n.get("address") or None,
            "router_id": n.get("router-id") or None,
            "state": (n.get("state") or "").lower() or None,          # full / init / 2-way / exstart / loading …
            "state_changes": n.get("state-changes"),
            "adjacency_s": parse_duration(n.get("adjacency")),
        })
    via: dict[str, int] = {}
    for count, r in enumerate(_active_ospf_routes(api)):
        if count >= MAX_ROUTES:
            break
        itf = route_interface(r)
        if itf:
            via[itf] = via.get(itf, 0) + 1
    return {"ospf_interfaces": interfaces, "ospf_neighbors": neighbors, "routes_via": via}
