"""Wireless: access points, PtP links and their clients (spec: release 1.8, the poll's "wireless" dict).

RouterOS has three wireless systems, read in turn; a router has the menus of the ones it runs (TrapError otherwise):
  - legacy `wireless` package (v6, and v7 on older radios): /interface/wireless + its registration-table
    (signal-strength "-62dBm@6Mbps", signal-to-noise, tx-ccq, rates "130Mbps-20MHz/2S/SGI");
  - `wifi` (v7.13+, wifiwave2 before that): /interface/wifi + registration-table (signal, rates; no SNR or CCQ);
  - 60 GHz `w60g`: /interface/w60g, its station table (multipoint APs) or `monitor once` per interface (PtP):
    rssi, tx-phy-rate, signal (a link quality %).
Values may come as numbers or with units; everything is normalised here, nonsense readings become None.
Fixtures are written from the manuals (tests/fixtures/docs-*): replace them with real captures (--capture).
"""
import re

from librouteros.exceptions import MultiTrapError, TrapError

from .parsing import parse_duration, parse_mac

MAX_INTERFACES = 32
MAX_CLIENTS = 512
_NUM = re.compile(r"^\s*(-?\d+(?:\.\d+)?)")
_RATE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(G|M|k)?bps", re.IGNORECASE)
_MISSING = (TrapError, MultiTrapError)


def poll_wireless(api) -> dict | None:
    """{"interfaces": [...], "clients": [...]}, or None if this router has no wireless at all."""
    interfaces, clients, found = [], [], False
    for reader in (_legacy, _wifi, _w60g):
        try:
            ifs, cls = reader(api)
        except _MISSING:
            continue                                    # this router doesn't run that wireless system
        found = True
        interfaces += ifs
        clients += cls
    if not found or not interfaces:
        return None
    counts = {}
    for c in clients:
        counts[c["interface"]] = counts.get(c["interface"], 0) + 1
    for i in interfaces:
        i["clients"] = counts.get(i["name"], 0)
    return {"interfaces": interfaces[:MAX_INTERFACES], "clients": clients[:MAX_CLIENTS]}


# ---- the three systems ----

def _legacy(api):
    ifs = [_interface(r, "legacy", r.get("mode"), r.get("ssid"), r.get("band"), r.get("frequency"),
                      r.get("channel-width")) for r in api.path("interface", "wireless")]
    clients = [_client(r, "legacy", signal=r.get("signal-strength"), tx_signal=r.get("tx-signal-strength"),
                       snr=r.get("signal-to-noise"), ccq=r.get("tx-ccq"), name=r.get("radio-name") or r.get("comment"),
                       ip=r.get("last-ip")) for r in api.path("interface", "wireless", "registration-table")]
    return ifs, clients


def _wifi(api):
    try:
        rows = list(api.path("interface", "wifi"))
        menu = ("interface", "wifi")
    except _MISSING:
        rows = list(api.path("interface", "wifiwave2"))          # v7 before 7.13
        menu = ("interface", "wifiwave2")
    ifs = [_interface(r, "wifi", r.get("configuration.mode"), r.get("configuration.ssid"), r.get("channel.band"),
                      r.get("channel.frequency"), r.get("channel.width")) for r in rows]
    clients = [_client(r, "wifi", signal=r.get("signal"), name=r.get("comment"), ip=r.get("last-ip"))
               for r in api.path(*menu, "registration-table")]
    return ifs, clients


def _w60g(api):
    rows = list(api.path("interface", "w60g"))
    ifs = [_interface(r, "w60g", r.get("mode"), r.get("ssid"), "60ghz", r.get("frequency"), None) for r in rows]
    try:
        stations = list(api.path("interface", "w60g", "station"))
    except _MISSING:
        stations = []
    if stations:                                                    # multipoint: one row per station
        return ifs, [_w60g_client(r, r.get("interface")) for r in stations
                     if r.get("connected", True) not in (False, "false")]
    clients = []
    for r in rows:                                                  # PtP: monitor each running interface on its own
        if r.get("running") not in (True, "true"):
            continue
        for m in api.path("interface", "w60g")("monitor", numbers=str(r.get(".id") or r.get("name")), once=""):
            if m.get("connected") in (True, "true"):
                clients.append(_w60g_client(m, r.get("name")))
    return ifs, clients


def _w60g_client(r, interface):
    return _client({**r, "interface": interface or r.get("interface") or r.get("name")}, "w60g",
                   signal=r.get("rssi"), tx_rate=r.get("tx-phy-rate"), rx_rate=None, quality=r.get("signal"),
                   mac=r.get("remote-address") or r.get("mac-address"))


# ---- normalising ----

def _interface(r, kind, mode, ssid, band, freq, width) -> dict:
    return {"name": str(r.get("name"))[:64], "kind": kind, "mode": mode_of(mode),
            "ssid": (str(ssid)[:64] if ssid not in (None, "") else None), "band": (str(band)[:32] if band else None),
            "frequency_mhz": frequency(freq), "width_mhz": width_mhz(width),
            "running": r.get("running") in (True, "true"), "disabled": r.get("disabled") in (True, "true")}


_UNSET = object()


def _client(r, kind, *, signal, tx_signal=None, snr=None, ccq=None, name=None, ip=None, quality=None,
            tx_rate=_UNSET, rx_rate=_UNSET, mac=None) -> dict:
    return {"interface": str(r.get("interface") or r.get("name"))[:64], "kind": kind,
            "mac": parse_mac(mac or r.get("mac-address")),
            "name": (str(name).strip()[:64] or None) if name else None,
            "ip": (str(ip)[:45] if ip else None),
            "signal_dbm": dbm(signal), "tx_signal_dbm": dbm(tx_signal), "snr_db": snr_db(snr),
            "tx_rate_mbps": mbps(r.get("tx-rate") if tx_rate is _UNSET else tx_rate),
            "rx_rate_mbps": mbps(r.get("rx-rate") if rx_rate is _UNSET else rx_rate),
            "ccq_pct": percent(ccq), "quality_pct": percent(quality),
            "uptime_s": parse_duration(r.get("uptime"))}


def _number(raw) -> float | None:
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    m = _NUM.match(str(raw))
    return float(m.group(1)) if m else None


def _in(v, low, high):
    return v if v is not None and low <= v <= high else None


def dbm(raw) -> float | None:
    """"-62dBm@6Mbps", "-62", -62 -> -62.0; a signal is always between -120 and 0 dBm."""
    return _in(_number(raw), -120, 0)


def snr_db(raw) -> float | None:
    return _in(_number(raw), 0, 90)


def percent(raw) -> float | None:
    return _in(_number(raw), 0, 100)


def mbps(raw) -> float | None:
    """"130Mbps-20MHz/2S/SGI" -> 130.0, "1.3Gbps" -> 1300.0, "54" -> 54.0."""
    if raw is None or raw == "" or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return _in(round(float(raw), 1), 0, 100_000)
    m = _RATE.match(str(raw))
    if m:
        scale = {"g": 1000, "m": 1, "k": 0.001}[(m.group(2) or "m").lower()]
        return _in(round(float(m.group(1)) * scale, 1), 0, 100_000)
    return _in(_number(raw), 0, 100_000)


def frequency(raw) -> int | None:
    """"5180", "5180MHz", 5180, "5180/20-Ce/an" -> 5180; "auto" or nonsense -> None."""
    v = _number(raw)
    return int(v) if v is not None and 900 <= v <= 72_000 else None


def width_mhz(raw) -> int | None:
    """"20/40/80mhz-Ceee" -> 80 (the widest the radio may use), "20mhz" -> 20, "2160MHz" -> 2160."""
    if raw in (None, ""):
        return None
    nums = [int(n) for n in re.findall(r"\d+", str(raw).split("-")[0]) if 5 <= int(n) <= 2160]
    return max(nums) if nums else None


def mode_of(raw) -> str | None:
    """Access point or station, whichever system it came from (ap-bridge, bridge, ap, station-bridge, ...)."""
    if not raw:
        return None
    m = str(raw).lower()
    if m.startswith("station"):
        return "station"
    if m in ("ap-bridge", "bridge", "ap", "wds-slave", "nstreme-dual-slave"):
        return "ap"
    return m[:24]
