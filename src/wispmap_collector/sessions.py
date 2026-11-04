"""Subscribers online, counted from the router's sessions (release 1.8, the poll's "sessions" dict, slow cadence).

Counts only: no customer names, addresses or MACs leave the network. A source counts only when this router
actually runs that kind of server (every v7 router has the menus, so an empty menu would read as "0 subscribers" on
core routers); otherwise it is None:
  - PPPoE: /ppp/active rows with service=pppoe, if a PPPoE server is configured (/interface/pppoe-server/server);
    other PPP services (L2TP, SSTP, ...) separately, when there are any;
  - hotspot: /ip/hotspot/active, if a hotspot server is configured (/ip/hotspot);
  - DHCP: bound leases in /ip/dhcp-server/lease, if a DHCP server is configured (/ip/dhcp-server). On a WISP's
    customer LAN that is its DHCP customers, but also its own gear on that LAN.
"""
from librouteros.exceptions import MultiTrapError, TrapError

_MISSING = (TrapError, MultiTrapError)


def _rows(api, *path) -> list | None:
    try:
        return list(api.path(*path))
    except _MISSING:
        return None


def poll_sessions(api) -> dict | None:
    out = {"pppoe": None, "ppp_other": None, "hotspot": None, "dhcp_bound": None}
    active = _rows(api, "ppp", "active")
    if active is not None:
        services = [str(r.get("service") or "").lower() for r in active]
        pppoe = sum(1 for s in services if s == "pppoe")
        if pppoe or _rows(api, "interface", "pppoe-server", "server"):
            out["pppoe"] = pppoe
        if len(services) - pppoe:
            out["ppp_other"] = len(services) - pppoe
    if _rows(api, "ip", "hotspot"):
        out["hotspot"] = len(_rows(api, "ip", "hotspot", "active") or [])
    if _rows(api, "ip", "dhcp-server"):
        out["dhcp_bound"] = sum(1 for r in _rows(api, "ip", "dhcp-server", "lease") or []
                                if str(r.get("status") or "").lower() == "bound")
    return out if any(v is not None for v in out.values()) else None
