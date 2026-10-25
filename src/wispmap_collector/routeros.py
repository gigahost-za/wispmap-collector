"""RouterOS API polling + normalisation (raw RouterOS -> WISPMap fields)."""
from .parsing import parse_duration, parse_version, parse_mac

_COUNTERS = ("rx-byte", "tx-byte", "rx-packet", "tx-packet",
             "rx-error", "tx-error", "rx-drop", "tx-drop", "link-downs")


def poll_system(api) -> dict:
    r = next(iter(api.path("system", "resource")))
    total, free = r.get("total-memory"), r.get("free-memory")
    return {
        "uptime_s": parse_duration(r.get("uptime")),
        "cpu_pct": r.get("cpu-load"),
        "mem_used_pct": round((total - free) / total * 100, 1) if total else None,
        "ros_version": parse_version(r.get("version")),
        "board_name": r.get("board-name"),
    }


def poll_interfaces(api) -> list[dict]:
    out = []
    for i in api.path("interface"):
        row = {
            "name": i["name"],
            "type": i.get("type"),
            "mac": parse_mac(i.get("mac-address")),
            "running": bool(i.get("running", False)),
            "disabled": bool(i.get("disabled", False)),
        }
        for c in _COUNTERS:
            row[c.replace("-", "_")] = i.get(c)
        out.append(row)
    return out


def poll_neighbors(api) -> list[dict]:
    """/ip/neighbor: what each interface sees via MNDP/LLDP/CDP. Used to suggest links (spec doc 03 §Link matcher).

    interface = OUR interface; mac = the REMOTE interface's MAC; remote_interface = its name.
    """
    out = []
    for n in api.path("ip", "neighbor"):
        local = (n.get("interface") or "").split(",")[0]       # v7 bridged ports: "ether2,bridge1" -> "ether2"
        out.append({
            "interface": local or None,
            "mac": parse_mac(n.get("mac-address")),
            "address": n.get("address") or n.get("address4") or None,
            "identity": n.get("identity") or None,           # "" (LLDP from PCs / ISP routers) -> None
            "platform": n.get("platform") or None,
            "version": parse_version(n.get("version")),
            "board": n.get("board") or None,
            "remote_interface": n.get("interface-name") or None,
            "discovered_by": n.get("discovered-by") or None,
        })
    return out
