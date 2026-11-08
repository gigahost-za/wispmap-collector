"""BGP state of a RouterOS device: one row per peer (session), with routes received counted on the router.

RouterOS 7: /routing/bgp/{instance,connection,session}. A session row exists only for an enabled connection; its
`established` key is ABSENT (not false) while down; `prefix-count` lags (seen stale in both directions in the lab), so
received routes are counted with `/ip/route/print count-only where bgp gateway=<peer>`: one small answer per peer, even
with a full table. RouterOS 6: /routing/bgp/peer (built from MikroTik's manual; no real capture yet).
"""
import re

from .parsing import parse_duration

MAX_PEERS = 200
_SESSION_SUFFIX = re.compile(r"-\d+$")          # v7 names a connection's sessions "<connection>-1", "-2", ...
_IP = re.compile(r"^[0-9a-fA-F:.]+$")


def _as(v) -> int | None:
    try:
        return int(str(v).strip()) if v not in (None, "") else None
    except ValueError:
        return None


def _addr(v) -> str | None:
    """'10.0.10.1' / '10.0.10.1/32' -> '10.0.10.1'; ranges ('10.0.0.0/24', listening connections) -> None."""
    v = (v or "").strip()
    if v.endswith(("/32", "/128")):
        v = v.rsplit("/", 1)[0]
    return v if v and _IP.match(v) else None


def _flag(v) -> bool:
    return v is True or str(v).lower() in ("true", "yes")


def count_routes(api, gateway: str, active_only: bool = False) -> int | None:
    """BGP routes in the main table whose gateway is this peer, counted by the router."""
    words = ["=count-only=", "?bgp=true", f"?gateway={gateway}"] + (["?active=true"] if active_only else [])
    raw = getattr(api, "rawCmd", None)
    if raw is not None:
        for row in raw("/ip/route/print", *words):
            if "ret" in row:
                return int(row["ret"])
        return None
    rows = (r for r in api.path("ip", "route") if r.get("bgp") and r.get("gateway") == gateway)  # test fakes
    return sum(1 for r in rows if r.get("active") or not active_only)


def _with_routes(api, row: dict) -> dict:
    addr = row.get("remote_address")
    if row.get("established") and addr:
        try:
            row["routes"] = count_routes(api, addr)
            row["routes_active"] = count_routes(api, addr, active_only=True)
        except Exception:  # noqa: BLE001, S110 - counts are a bonus: the session state is still good data
            pass
    return row


def _v7(api, connections: list[dict]) -> list[dict]:
    instances = {i.get("name"): _as(i.get("as")) for i in api.path("routing", "bgp", "instance")}
    sessions = list(api.path("routing", "bgp", "session"))
    by_conn: dict[str, list[dict]] = {}
    for s in sessions:
        by_conn.setdefault(_SESSION_SUFFIX.sub("", s.get("name") or ""), []).append(s)
    rows = []
    for c in connections:
        local_as = _as(c.get("as")) or instances.get(c.get("instance"))
        disabled = _flag(c.get("disabled"))
        mine = by_conn.pop(c.get("name") or "", [])
        if not mine:                                  # disabled, or (re)starting: no session row at all
            rows.append({"name": c.get("name"), "remote_address": _addr(c.get("remote.address")),
                         "remote_as": _as(c.get("remote.as")), "local_as": local_as, "remote_id": None,
                         "disabled": disabled, "established": False, "state": "disabled" if disabled else "idle",
                         "uptime_s": None, "last_started": None, "last_stopped": None, "prefix_count": None})
            continue
        for s in mine:
            est = _flag(s.get("established"))
            rows.append({"name": c.get("name"), "remote_address": _addr(s.get("remote.address")) or
                         _addr(c.get("remote.address")), "remote_as": _as(s.get("remote.as") or c.get("remote.as")),
                         "local_as": _as(s.get("local.as")) or local_as, "remote_id": s.get("remote.id") or None,
                         "disabled": disabled, "established": est, "state": "established" if est else "down",
                         "uptime_s": parse_duration(s.get("uptime")) if est else None,
                         "last_started": s.get("last-started") or None, "last_stopped": s.get("last-stopped") or None,
                         "prefix_count": _as(s.get("prefix-count"))})
    for name, rest in by_conn.items():               # sessions of connections we didn't list (shouldn't happen)
        for s in rest:
            est = _flag(s.get("established"))
            rows.append({"name": name or s.get("name"), "remote_address": _addr(s.get("remote.address")),
                         "remote_as": _as(s.get("remote.as")), "local_as": _as(s.get("local.as")),
                         "remote_id": s.get("remote.id") or None, "disabled": False, "established": est,
                         "state": "established" if est else "down",
                         "uptime_s": parse_duration(s.get("uptime")) if est else None,
                         "last_started": s.get("last-started") or None, "last_stopped": s.get("last-stopped") or None,
                         "prefix_count": _as(s.get("prefix-count"))})
    return rows


def _v6(api, peers: list[dict]) -> list[dict]:
    instances = {i.get("name"): _as(i.get("as")) for i in api.path("routing", "bgp", "instance")}
    rows = []
    for p in peers:
        state = (p.get("state") or "").lower() or None
        disabled = _flag(p.get("disabled"))
        est = state == "established" and not disabled
        rows.append({"name": p.get("name"), "remote_address": _addr(p.get("remote-address")),
                     "remote_as": _as(p.get("remote-as")), "local_as": instances.get(p.get("instance") or "default"),
                     "remote_id": p.get("remote-id") or None, "disabled": disabled, "established": est,
                     "state": "disabled" if disabled else (state or "idle"),
                     "uptime_s": parse_duration(p.get("uptime")) if est else None,
                     "last_started": None, "last_stopped": None, "prefix_count": _as(p.get("prefix-count"))})
    return rows


def poll_bgp(api) -> dict | None:
    """{"peers": [...]} or None when the router has no BGP configured.
    Raises librouteros TrapError/MultiTrapError when it has no BGP menu at all (caller: None)."""
    from librouteros.exceptions import MultiTrapError, TrapError

    try:
        connections = list(api.path("routing", "bgp", "connection"))
        rows = _v7(api, connections) if connections else []
    except (TrapError, MultiTrapError):              # RouterOS 6: no /routing/bgp/connection
        peers = list(api.path("routing", "bgp", "peer"))
        rows = _v6(api, peers) if peers else []
    if not rows:
        return None
    for r in rows:
        r["ebgp"] = None if r["remote_as"] is None or r["local_as"] is None else r["remote_as"] != r["local_as"]
    return {"peers": [_with_routes(api, r) for r in rows[:MAX_PEERS]]}
