"""Router security facts for the security check (release 1.8.1; the poll's "security" dict, slow cadence).

Read-only prints of the settings MikroTik's own hardening advice is about. Facts only: the server judges them
(known-vulnerable versions change over time, so that list lives on the server). No user names or passwords are sent:
  - services: /ip/service (name, port, disabled, whether "available from" is restricted), configured ones only:
    RouterOS 7.23+ also lists dynamic rows there (btest, discover, and every live API connection with its address);
  - users: a default "admin" that is enabled, how many users have full rights, and the rights of the group the
    collector itself logs in with (it should only need read, api and test);
  - update: /system/package/update (channel; installed and latest version if the router has checked);
  - dns_remote (an open resolver), socks and proxy (often switched on by malware), mac_winbox (MAC Winbox allowed on).
Each part is None when it can't be read.
"""
from librouteros.exceptions import MultiTrapError, TrapError

from .parsing import parse_version

_MISSING = (TrapError, MultiTrapError)
MAX_SERVICES = 16


def _true(v) -> bool:
    return v in (True, "true", "yes")


def _first(api, *path) -> dict | None:
    try:
        rows = list(api.path(*path))
    except _MISSING:
        return None
    return rows[0] if rows else None


def poll_security(api, username: str) -> dict | None:
    out = {"services": None, "default_admin": None, "full_users": None, "own_policy": None, "update": None,
           "dns_remote": None, "socks": None, "proxy": None, "mac_winbox": None}
    try:
        out["services"] = [{"name": str(r.get("name"))[:24], "port": _port(r.get("port")),
                            "disabled": _true(r.get("disabled")), "restricted": bool(str(r.get("address") or "").strip())}
                           for r in api.path("ip", "service")
                           if not _true(r.get("dynamic"))][:MAX_SERVICES]   # 7.23+ also lists live connections
    except _MISSING:
        pass
    try:
        users = list(api.path("user"))
        groups = {str(g.get("name")): str(g.get("policy") or "") for g in api.path("user", "group")}
    except _MISSING:
        users, groups = None, {}
    if users is not None:
        enabled = [u for u in users if not _true(u.get("disabled"))]
        out["default_admin"] = any(str(u.get("name")) == "admin" for u in enabled)
        out["full_users"] = sum(1 for u in enabled if str(u.get("group")) == "full")
        own = next((u for u in users if str(u.get("name")) == username), None)
        if own is not None and str(own.get("group")) in groups:
            out["own_policy"] = sorted(p for p in groups[str(own.get("group"))].split(",") if p and not p.startswith("!"))
    upd = _first(api, "system", "package", "update")
    if upd is not None:
        out["update"] = {"channel": (str(upd.get("channel"))[:24] if upd.get("channel") else None),
                         "installed": parse_version(upd.get("installed-version")),
                         "latest": parse_version(upd.get("latest-version"))}
    dns = _first(api, "ip", "dns")
    if dns is not None:
        out["dns_remote"] = _true(dns.get("allow-remote-requests"))
    socks = _first(api, "ip", "socks")
    if socks is not None:
        out["socks"] = _true(socks.get("enabled"))
    proxy = _first(api, "ip", "proxy")
    if proxy is not None:
        out["proxy"] = _true(proxy.get("enabled"))
    mac = _first(api, "tool", "mac-server", "mac-winbox")
    if mac is not None:
        out["mac_winbox"] = str(mac.get("allowed-interface-list") or "")[:32] or None
    return out if any(v is not None for v in out.values()) else None


def _port(raw) -> int | None:
    try:
        p = int(raw)
    except (TypeError, ValueError):
        return None
    return p if 0 < p < 65536 else None
