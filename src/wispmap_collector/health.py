"""/system/health -> voltage, temperatures, power, PSU and fan state (spec doc 02: units V, °C, W).

Two shapes, both read the same way:
  RouterOS 7: one row per sensor  {"name": "voltage", "value": "24.3", "type": "V"}
  RouterOS 6: one row, a field per sensor  {"voltage": "24.1", "temperature": 35, "psu1-state": "ok", ...}
Rows/fields that aren't readings (CHR answers {"state": "disabled", ...}; v6 also has fan settings) are ignored.
librouteros turns whole numbers into int and "true"/"false" into bool, so values may be str, int or bool.
"""
import re

BOARD_TEMPS = ("temperature", "board-temperature1", "board-temperature", "switch-temperature")
NOT_STATES = {"state", "state-after-reboot"}          # /system/health settings, not a component's state


def poll_health(api) -> dict | None:
    readings: dict[str, tuple[object, str]] = {}
    for row in api.path("system", "health"):
        if "name" in row and "value" in row:          # v7
            readings[str(row["name"])] = (row["value"], str(row.get("type") or ""))
        else:                                          # v6 (or a settings row)
            for key, value in row.items():
                if not key.startswith("."):
                    readings[key] = (value, "")
    return normalise_health(readings)


def _num(value) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        return float(str(value).strip().rstrip("VCWA%").strip())
    except ValueError:
        return None


def normalise_health(readings: dict) -> dict | None:
    """None when the router has no sensors (CHR, x86) or reports nothing usable."""
    def num(name, low, high):
        value = _num(readings[name][0]) if name in readings else None
        return value if value is not None and low <= value <= high else None    # nonsense readings -> None

    def temp(name):
        if name not in readings:
            return None
        value, unit = readings[name]
        t = _num(value)
        if t is not None and unit.upper() in ("F", "°F"):        # /system/health/settings fahrenheit=yes
            t = (t - 32) * 5 / 9
        return round(t, 1) if t is not None and -40 <= t <= 150 else None

    states = {k[:-len("-state")]: str(v).strip().lower()[:24] for k, (v, _) in readings.items()
              if k.endswith("-state") and k not in NOT_STATES and not isinstance(v, bool)}
    fans = {}
    for k, (v, _) in readings.items():
        m = re.fullmatch(r"(fan\d*)-speed", k)
        if m and _num(v) is not None:
            fans[m.group(1)] = int(_num(v))
    out = {
        "voltage_v": num("voltage", 0.5, 100),
        "temperature_c": next((t for t in map(temp, BOARD_TEMPS) if t is not None), None),
        "cpu_temp_c": temp("cpu-temperature"),
        "power_w": num("power-consumption", 0, 5000),
        "states": dict(list(states.items())[:16]),
        "fans": dict(list(fans.items())[:16]),
    }
    return out if any(v not in (None, {}) for v in out.values()) else None
