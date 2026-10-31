"""/interface/ethernet/poe -> PoE-out per port (spec doc 03: the poll's "poe" list).

Two reads: the port list (print: name, poe-out mode auto-on / forced-on / off) and the live readings
(monitor <ids> once: poe-out-status, -voltage, -current, -power). Routers without PoE-out have no such menu
(TrapError: the caller sends None). Values may come as numbers or with units ("24.1V", "254mA", "6.1W"),
the status as RouterOS writes it ("powered-on", "short-circuit"); both are normalised here.
"""
import re

MAX_PORTS = 64
_NUM = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*([a-zA-Z]*)\s*$")


def poll_poe(api) -> list[dict] | None:
    ports = list(api.path("interface", "ethernet", "poe"))
    if not ports:
        return None
    ids = ",".join(str(p.get(".id") or p.get("name")) for p in ports)
    monitor = api.path("interface", "ethernet", "poe")("monitor", numbers=ids, once="")
    readings = {str(r.get("name")): r for r in monitor}
    return [normalise_port(p, readings.get(str(p.get("name")), {})) for p in ports][:MAX_PORTS]


def _value(raw) -> tuple[float, str] | None:
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return float(raw), ""
    m = _NUM.match(str(raw))
    return (float(m.group(1)), m.group(2).lower()) if m else None


def _in(value, low, high):
    return value if value is not None and low <= value <= high else None    # nonsense readings -> None


def volts(raw) -> float | None:
    v = _value(raw)
    return _in(round(v[0], 2), 0, 60) if v and v[1] in ("", "v") else None


def milliamps(raw) -> float | None:
    v = _value(raw)
    if not v or v[1] not in ("", "ma", "a"):
        return None
    return _in(round(v[0] * 1000 if v[1] == "a" else v[0], 1), 0, 5000)    # RouterOS reports mA


def watts(raw) -> float | None:
    v = _value(raw)
    return _in(round(v[0], 2), 0, 120) if v and v[1] in ("", "w") else None


def status(raw) -> str | None:
    if raw is None or raw == "":
        return None
    return str(raw).strip().lower().replace("-", "_").replace(" ", "_")[:32]


def normalise_port(port: dict, reading: dict) -> dict:
    return {"interface": str(port.get("name") or reading.get("name"))[:64],
            "mode": status(port.get("poe-out") or reading.get("poe-out")),
            "status": status(reading.get("poe-out-status")),
            "voltage_v": volts(reading.get("poe-out-voltage")),
            "current_ma": milliamps(reading.get("poe-out-current")),
            "power_w": watts(reading.get("poe-out-power"))}
