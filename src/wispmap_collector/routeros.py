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
