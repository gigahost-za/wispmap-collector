"""SNMP drivers (0.8.0): a device's SNMP answers {oid: value} -> the same normalised poll data RouterOS gives
(system, interfaces, wireless, health), plus "ups" for UPSs. Pure functions; snmppoll.py does the reading.

Families, told apart by the vendor tables a device answers (Ubiquiti radios report a generic Linux sysObjectID):
  airmax    Ubiquiti airMAX (airOS 6/8)       UBNT-AirMAX-MIB   1.3.6.1.4.1.41112.1.4
  airfiber  Ubiquiti airFiber (AF5/24/...)    UBNT-AirFIBER-MIB 1.3.6.1.4.1.41112.1.3
  ltu       Ubiquiti airFiber LTU / 5XHD      UBNT-AFLTU-MIB    1.3.6.1.4.1.41112.1.10
  af60      Ubiquiti airFiber 60              UI-AF60-MIB       1.3.6.1.4.1.41112.1.11
  epmp      Cambium ePMP                      CAMBIUM-PMP80211-MIB 1.3.6.1.4.1.17713.21
  apc / cyberpower / ups   UPSs: APC PowerNet, CyberPower CPS-MIB, else the standard UPS-MIB (RFC 1628)
  generic   anything else: system + interfaces
OIDs and units were checked against the vendors' MIBs and recordings of real devices.
"""
import re

from .parsing import parse_mac

UBNT = "1.3.6.1.4.1.41112.1"
AIRMAX, AIRFIBER, LTU, AF60 = f"{UBNT}.4", f"{UBNT}.3", f"{UBNT}.10.1", f"{UBNT}.11.1"
EPMP = "1.3.6.1.4.1.17713.21"
UPS_MIB, APC_UPS, CPS_UPS = "1.3.6.1.2.1.33.1", "1.3.6.1.4.1.318.1.1.1", "1.3.6.1.4.1.3808.1.1.1"

# (family, OID whose presence identifies it), most specific first
FAMILIES = (("airmax", f"{AIRMAX}.1"), ("airfiber", f"{AIRFIBER}.1"), ("ltu", f"{LTU}.2"), ("af60", f"{AF60}.1"),
            ("epmp", f"{EPMP}.1.1"), ("apc", f"{APC_UPS}.2"), ("cyberpower", f"{CPS_UPS}.2"),
            ("ups", f"{UPS_MIB}.2"))

SYS = {"descr": "1.3.6.1.2.1.1.1.0", "object_id": "1.3.6.1.2.1.1.2.0", "uptime": "1.3.6.1.2.1.1.3.0",
       "name": "1.3.6.1.2.1.1.5.0"}
IF = "1.3.6.1.2.1.2.2.1"
IFX = "1.3.6.1.2.1.31.1.1.1"
IF_COLUMNS = {2: "descr", 3: "type", 6: "mac", 7: "admin", 8: "oper", 10: "in_octets", 11: "in_ucast", 13: "in_discards",
              14: "in_errors", 16: "out_octets", 17: "out_ucast", 19: "out_discards", 20: "out_errors"}
IFX_COLUMNS = {1: "name", 6: "hc_in_octets", 7: "hc_in_ucast", 10: "hc_out_octets", 11: "hc_out_ucast"}
IF_TYPES = {6: "ether", 24: "loopback", 71: "wlan", 117: "ether", 131: "tunnel", 53: "vlan", 135: "vlan", 209: "bridge"}
MAX_INTERFACES = 64
MAX_CLIENTS = 512

# What to read per family (bases to walk; station tables only the columns used, so a busy AP stays cheap)
WALKS = {
    "airmax": [f"{AIRMAX}.1", f"{AIRMAX}.5", f"{AIRMAX}.6", f"{AIRMAX}.8"] +
              [f"{AIRMAX}.7.1.{c}" for c in (2, 3, 4, 6, 8, 10, 11, 12, 15)],
    "airfiber": [f"{AIRFIBER}.1.1", f"{AIRFIBER}.2.1"],
    "ltu": [f"{LTU}.2", f"{LTU}.3"] + [f"{LTU}.4.1.{c}" for c in (3, 4, 5, 6, 13, 16, 17, 24, 25)],
    "af60": [f"{AF60}.1", f"{AF60}.2"] + [f"{AF60}.3.1.{c}" for c in (3, 4, 7, 8, 14, 17, 18, 20)],
    "epmp": [f"{EPMP}.1.1", f"{EPMP}.1.2.1", f"{EPMP}.1.2.2", f"{EPMP}.1.2.10", f"{EPMP}.2.1.64", f"{EPMP}.3.8.2.1",
             f"{EPMP}.3.8.2.2", f"{EPMP}.3.8.2.5"] + [f"{EPMP}.1.2.30.1.{c}" for c in (1, 4, 5, 6, 10, 18, 19, 20)],
    "apc": [f"{APC_UPS}.1.1", f"{APC_UPS}.1.2", f"{APC_UPS}.2", f"{APC_UPS}.3.2", f"{APC_UPS}.3.3", f"{APC_UPS}.4.1", f"{APC_UPS}.4.2",
            f"{APC_UPS}.4.3"],
    "cyberpower": [f"{CPS_UPS}.1.1", f"{CPS_UPS}.1.2", f"{CPS_UPS}.2", f"{CPS_UPS}.3.2", f"{CPS_UPS}.4.1", f"{CPS_UPS}.4.2"],
    "ups": [f"{UPS_MIB}.1", f"{UPS_MIB}.2", f"{UPS_MIB}.3.3.1.3", f"{UPS_MIB}.4.1", f"{UPS_MIB}.4.4.1.5"],
    "generic": [],
}
HOST_RESOURCES_CPU = "1.3.6.1.2.1.25.3.3.1.2"          # hrProcessorLoad per CPU (%)


# ---------- value helpers ----------

def text(v) -> str | None:
    if v is None:
        return None
    if isinstance(v, bytes):
        v = v.decode("utf-8", "replace")
    v = str(v).strip().strip("\x00")
    return v or None


def mac(v) -> str | None:
    """A MAC from 6 raw octets or from text ("aa:bb:..", "AABBCC..")."""
    if isinstance(v, bytes) and len(v) == 6:
        return None if v == b"\x00" * 6 else ":".join(f"{b:02x}" for b in v)
    t = text(v)
    if not t:
        return None
    digits = re.sub(r"[^0-9a-fA-F]", "", t)
    if len(digits) != 12:
        return None
    return parse_mac(":".join(digits[i:i + 2] for i in range(0, 12, 2)))


def num(v, low=None, high=None, scale=1.0, nd=1):
    """A number (from int or text) times scale, or None outside [low, high] (devices use -1 / 0 for "unknown")."""
    if v is None:
        return None
    if isinstance(v, bytes):
        v = text(v)
    try:
        x = float(v) * scale
    except (TypeError, ValueError):
        return None
    if (low is not None and x < low) or (high is not None and x > high):
        return None
    return round(x, nd) if nd is not None else x


def col(data: dict, base: str) -> dict:
    """{index (the OID part after base): value} for one table column."""
    p = base + "."
    return {k[len(p):]: v for k, v in data.items() if k.startswith(p)}


def index_mac(index: str) -> str | None:
    """A table index ending in a MAC as six numbers (UBNT station tables)."""
    parts = index.split(".")
    if len(parts) < 6:
        return None
    try:
        return mac(bytes(int(p) for p in parts[-6:]))
    except ValueError:
        return None


def mbps(v, unit_bps: float) -> float | None:
    x = num(v, 0, 1e12, scale=unit_bps / 1e6)
    return None if x is None else round(x, 1)


# ---------- detection, system, interfaces ----------

def detect(data: dict) -> str:
    for family, oid in FAMILIES:
        if any(k.startswith(oid + ".") or k == oid for k in data):
            return family
    return "generic"


VENDORS = {"airmax": "Ubiquiti", "airfiber": "Ubiquiti", "ltu": "Ubiquiti", "af60": "Ubiquiti", "epmp": "Cambium",
           "apc": "APC", "cyberpower": "CyberPower"}


def system(data: dict, family: str) -> dict:
    """{uptime_s, cpu_pct, mem_used_pct, ros_version, board_name, vendor, model, firmware}: ros_version is always
    None (not RouterOS); board_name = the model, so existing pages show it."""
    ticks = data.get(SYS["uptime"])
    uptime = int(ticks) // 100 if isinstance(ticks, int) else None
    model = firmware = cpu = mem = None
    if family == "airmax":
        model = text(_first(col(data, "1.2.840.10036.3.1.2.1.3")))          # dot11 product name
        firmware = text(_first(col(data, "1.2.840.10036.3.1.2.1.4")))
        cpu = num(data.get(f"{AIRMAX}.8.3.0"), 0, 100)
    elif family == "airfiber":
        firmware = text(_first(col(data, f"{AIRFIBER}.2.1.40")))
        up = num(_first(col(data, f"{AIRFIBER}.2.1.38")), 0, nd=None)
        uptime = uptime if uptime is not None else (int(up) if up is not None else None)
        model = "airFiber"
    elif family == "ltu":
        model, firmware = text(data.get(f"{LTU}.3.2.0")), text(data.get(f"{LTU}.3.4.0"))
        cpu, mem = num(data.get(f"{LTU}.3.6.0"), 0, 100), num(data.get(f"{LTU}.3.5.0"), 0, 100)
        up = num(data.get(f"{LTU}.3.7.0"), 0, nd=None)
        uptime = uptime if uptime is not None else (int(up) if up is not None else None)
    elif family == "af60":
        model, firmware = text(_first(col(data, f"{AF60}.2.2"))), text(_first(col(data, f"{AF60}.2.4")))
        cpu, mem = num(_first(col(data, f"{AF60}.2.6")), 0, 100), num(_first(col(data, f"{AF60}.2.5")), 0, 100)
        up = _first(col(data, f"{AF60}.2.7"))
        uptime = uptime if uptime is not None else (int(up) // 100 if isinstance(up, int) else None)
    elif family == "epmp":
        firmware = text(data.get(f"{EPMP}.1.1.17.0"))
        cpu = num(data.get(f"{EPMP}.2.1.64.0"), 0, 100, scale=0.1)       # sysCPUUsage: tenths of a percent
        model = "ePMP"
        if uptime is None:
            uptime = _dhms(text(data.get(f"{EPMP}.1.1.4.0")))
    elif family == "apc":
        model, firmware = text(data.get(f"{APC_UPS}.1.1.1.0")), text(data.get(f"{APC_UPS}.1.2.1.0"))
    elif family == "cyberpower":
        model, firmware = text(data.get(f"{CPS_UPS}.1.1.1.0")), text(data.get(f"{CPS_UPS}.1.2.1.0"))
    elif family == "ups":
        model = text(data.get(f"{UPS_MIB}.1.2.0"))
        firmware = text(data.get(f"{UPS_MIB}.1.3.0"))
    if cpu is None:
        loads = [num(v, 0, 100) for v in col(data, HOST_RESOURCES_CPU).values()]
        loads = [x for x in loads if x is not None]
        cpu = round(sum(loads) / len(loads), 1) if loads else None
    vendor = VENDORS.get(family) or (text(data.get(f"{UPS_MIB}.1.1.0")) if family == "ups" else None)
    return {"uptime_s": uptime, "cpu_pct": cpu, "mem_used_pct": mem, "ros_version": None,
            "board_name": (model or text(data.get(SYS["descr"])) or "")[:64] or None, "vendor": vendor,
            "model": model, "firmware": (firmware or None) and firmware[:64]}


def _first(column: dict):
    return next(iter(column.values()), None)


def _dhms(t: str | None) -> int | None:
    """ePMP's uptime text "dddd:hh:mm:ss"."""
    if not t:
        return None
    parts = re.findall(r"\d+", t)
    if len(parts) != 4:
        return None
    d, h, m, s = map(int, parts)
    return ((d * 24 + h) * 60 + m) * 60 + s


def interfaces(data: dict) -> list[dict]:
    """IF-MIB (+ ifXTable's 64-bit counters when present) -> raw interfaces with counters, like routeros.py."""
    rows: dict[str, dict] = {}
    for c, key in IF_COLUMNS.items():
        for idx, v in col(data, f"{IF}.{c}").items():
            rows.setdefault(idx, {})[key] = v
    for c, key in IFX_COLUMNS.items():
        for idx, v in col(data, f"{IFX}.{c}").items():
            rows.setdefault(idx, {})[key] = v
    out = []
    order = sorted(rows, key=lambda i: int(i) if i.isdigit() else 0)[:MAX_INTERFACES]
    names = _unique_names({i: (text(rows[i].get("name")), text(rows[i].get("descr"))) for i in order})
    for idx in order:
        r = rows[idx]
        name = names[idx]
        out.append({
            "name": name[:64], "type": IF_TYPES.get(r.get("type"), "other" if r.get("type") is not None else None),
            "mac": mac(r.get("mac")), "running": r.get("oper") == 1, "disabled": r.get("admin") == 2, "link_downs": None,
            "rx_byte": r.get("hc_in_octets", r.get("in_octets")), "tx_byte": r.get("hc_out_octets", r.get("out_octets")),
            "rx_packet": r.get("hc_in_ucast", r.get("in_ucast")), "tx_packet": r.get("hc_out_ucast", r.get("out_ucast")),
            "rx_error": r.get("in_errors"), "tx_error": r.get("out_errors"),
            "rx_drop": r.get("in_discards"), "tx_drop": r.get("out_discards"),
        })
    return out


def _unique_names(raw: dict) -> dict:
    """{ifIndex: name}: ifName, else ifDescr. When a name repeats (LTU lists eth0 twice; ePMP calls both radios
    "WLAN interface"), the copies use their ifDescr if that is unique, else the first keeps the name and the
    others become "name (ifIndex)"."""
    first = {i: n or d or f"if{i}" for i, (n, d) in raw.items()}
    seen_names = list(first.values())
    descrs = [d for _, d in raw.values() if d]
    out, taken = {}, set()
    for i, n in first.items():
        d = raw[i][1]
        if seen_names.count(n) > 1 and d and descrs.count(d) == 1 and d not in seen_names:
            name = d
        elif n in taken:
            name = f"{n} ({i})"
        else:
            name = n
        taken.add(name)
        out[i] = name[:64]
    return out

# ---------- wireless ----------

def _radio(name, kind, mode, ssid=None, freq=None, width=None, clients=0):
    return {"name": name, "kind": kind, "mode": mode, "ssid": (ssid or None) and ssid[:64], "band": None,
            "frequency_mhz": num(freq, 900, 100_000), "width_mhz": num(width, 1, 10_000, nd=0),
            "running": True, "disabled": False, "clients": clients}


def _client(iface, kind, mac_, name=None, ip=None, signal=None, tx_signal=None, snr=None, tx_rate=None, rx_rate=None,
            ccq=None, quality=None, uptime=None):
    return {"interface": iface, "kind": kind, "mac": mac_, "name": (text(name) or None) and text(name)[:64],
            "ip": ip if isinstance(ip, str) and ip not in ("0.0.0.0", "") else None,
            "signal_dbm": num(signal, -120, -1), "tx_signal_dbm": num(tx_signal, -120, -1), "snr_db": num(snr, 0, 90),
            "tx_rate_mbps": tx_rate, "rx_rate_mbps": rx_rate, "ccq_pct": num(ccq, 0, 100),
            "quality_pct": num(quality, 0, 100), "uptime_s": None if uptime is None else int(uptime)}


def _airmax_rate(v):
    """airOS station/link rates are bit/s (seen on airOS 8.5 and 8.7)."""
    return mbps(v, 1)


def wireless(data: dict, family: str, ifaces: list[dict]) -> dict | None:
    """{"interfaces": [radio], "clients": [...]} in the shape wireless.py gives for RouterOS, or None."""
    wlan = (next((i["name"] for i in ifaces if i["name"] in ("ath0", "wlan0", "air0") and i["running"]), None)
            or next((i["name"] for i in ifaces if i["type"] == "wlan"), None))
    if family == "airmax":
        modes = col(data, f"{AIRMAX}.1.1.2")                          # 1 sta, 2 ap, 3 ap-repeater, 4 ap-wds
        idx = next(iter(col(data, f"{AIRMAX}.5.1.2")), None) or next(iter(modes), "1")
        mode = {1: "station", 2: "ap", 3: "ap", 4: "ap"}.get(modes.get(idx, modes.get("1")))
        name = wlan or "ath0"
        clients = []
        macs = col(data, f"{AIRMAX}.7.1.3")
        for sidx in macs:
            m = index_mac(sidx)
            sig = data.get(f"{AIRMAX}.7.1.3.{sidx}")
            noise = data.get(f"{AIRMAX}.7.1.4.{sidx}")
            snr = (sig - noise) if isinstance(sig, int) and isinstance(noise, int) and noise < sig else None
            ticks = data.get(f"{AIRMAX}.7.1.15.{sidx}")
            clients.append(_client(name, "airmax", m, data.get(f"{AIRMAX}.7.1.2.{sidx}"),
                                   data.get(f"{AIRMAX}.7.1.10.{sidx}"), sig, None, snr,
                                   _airmax_rate(data.get(f"{AIRMAX}.7.1.11.{sidx}")),
                                   _airmax_rate(data.get(f"{AIRMAX}.7.1.12.{sidx}")),
                                   data.get(f"{AIRMAX}.7.1.6.{sidx}"), data.get(f"{AIRMAX}.7.1.8.{sidx}"),
                                   ticks // 100 if isinstance(ticks, int) else None))
        if mode == "station" and not clients:                       # a CPE / backhaul station: its AP, from WlStat
            sig, noise = data.get(f"{AIRMAX}.5.1.5.{idx}"), data.get(f"{AIRMAX}.5.1.8.{idx}")
            ap = mac(data.get(f"{AIRMAX}.5.1.4.{idx}"))
            if sig is not None or ap:
                snr = (sig - noise) if isinstance(sig, int) and isinstance(noise, int) and noise < sig else None
                clients.append(_client(name, "airmax", ap, signal=sig, snr=snr,
                                       tx_rate=_airmax_rate(data.get(f"{AIRMAX}.5.1.9.{idx}")),
                                       rx_rate=_airmax_rate(data.get(f"{AIRMAX}.5.1.10.{idx}")),
                                       ccq=data.get(f"{AIRMAX}.5.1.7.{idx}")))
        count = data.get(f"{AIRMAX}.5.1.15.{idx}")
        radio = _radio(name, "airmax", mode, text(data.get(f"{AIRMAX}.5.1.2.{idx}")),
                       data.get(f"{AIRMAX}.1.1.4.{idx}") or _first(col(data, f"{AIRMAX}.1.1.4")),
                       data.get(f"{AIRMAX}.5.1.14.{idx}"), count if isinstance(count, int) else len(clients))
        return {"interfaces": [radio], "clients": clients[:MAX_CLIENTS]}
    if family == "airfiber":
        i = next(iter(col(data, f"{AIRFIBER}.2.1.11")), "1")
        p = f"{AIRFIBER}.2.1"
        rx = [data.get(f"{p}.11.{i}") if data.get(f"{p}.12.{i}") != 2 else None,
              data.get(f"{p}.14.{i}") if data.get(f"{p}.15.{i}") != 2 else None]
        remote = [data.get(f"{p}.19.{i}") if data.get(f"{p}.20.{i}") != 2 else None,
                  data.get(f"{p}.22.{i}") if data.get(f"{p}.23.{i}") != 2 else None]
        sig = max((x for x in rx if isinstance(x, int) and x < 0), default=None)
        far = max((x for x in remote if isinstance(x, int) and x < 0), default=None)
        up = data.get(f"{p}.26.{i}") == 1                            # radioLinkState up(1)
        name = wlan or "air0"
        clients = [_client(name, "airfiber", None, signal=sig, tx_signal=far,
                           tx_rate=mbps(data.get(f"{p}.6.{i}"), 1), rx_rate=mbps(data.get(f"{p}.5.{i}"), 1),
                           uptime=num(data.get(f"{p}.44.{i}"), 0, nd=None))] if up or sig is not None else []
        freq = data.get(f"{AIRFIBER}.1.1.5.{i}")
        if isinstance(freq, int) and freq > 100_000:                  # AF11 says 11485000: kHz, not the MIB's MHz
            freq = freq // 1000
        radio = _radio(name, "airfiber", "ptp", freq=freq,
                       width=data.get(f"{AIRFIBER}.1.1.27.{i}"), clients=len(clients))
        return {"interfaces": [radio], "clients": clients}
    if family == "ltu":
        name = wlan or "ath0"
        clients = []
        for sidx in col(data, f"{LTU}.4.1.5"):
            p = f"{LTU}.4.1"
            sig = max((x for x in (data.get(f"{p}.5.{sidx}"), data.get(f"{p}.6.{sidx}")) if isinstance(x, int) and x < 0),
                      default=None)
            far = max((x for x in (data.get(f"{p}.16.{sidx}"), data.get(f"{p}.17.{sidx}")) if isinstance(x, int) and x < 0),
                      default=None)
            clients.append(_client(name, "ltu", index_mac(sidx), data.get(f"{p}.13.{sidx}"), data.get(f"{p}.25.{sidx}"),
                                   sig, far, None, mbps(data.get(f"{p}.3.{sidx}"), 1e3), mbps(data.get(f"{p}.4.{sidx}"), 1e3),
                                   uptime=num(data.get(f"{p}.24.{sidx}"), 0, nd=None)))
        role = data.get(f"{LTU}.2.1.0")
        radio = _radio(name, "ltu", "ap" if role == 0 else "station", text(data.get(f"{LTU}.2.5.0")),
                       data.get(f"{LTU}.2.2.0"), data.get(f"{LTU}.2.4.0"), len(clients))
        return {"interfaces": [radio], "clients": clients[:MAX_CLIENTS]}
    if family == "af60":
        name = wlan or "wlan0"
        clients = []
        p = f"{AF60}.3.1"
        for sidx in col(data, f"{p}.3"):
            ticks = data.get(f"{p}.17.{sidx}")
            clients.append(_client(name, "af60", index_mac(sidx), data.get(f"{p}.14.{sidx}"), data.get(f"{p}.20.{sidx}"),
                                   data.get(f"{p}.3.{sidx}"), data.get(f"{p}.18.{sidx}"), data.get(f"{p}.4.{sidx}"),
                                   mbps(data.get(f"{p}.7.{sidx}"), 1e3), mbps(data.get(f"{p}.8.{sidx}"), 1e3),
                                   uptime=ticks // 100 if isinstance(ticks, int) else None))
        role = _first(col(data, f"{AF60}.1.1"))
        radio = _radio(name, "af60", "ap" if role == 0 else "station", text(_first(col(data, f"{AF60}.1.4"))),
                       _first(col(data, f"{AF60}.1.2")), _first(col(data, f"{AF60}.1.3")), len(clients))
        return {"interfaces": [radio], "clients": clients[:MAX_CLIENTS]}
    if family == "epmp":
        name = wlan or "wlan0"
        p = f"{EPMP}.1.2.30.1"
        clients = [_client(name, "epmp", mac(v), data.get(f"{p}.18.{i}"), data.get(f"{p}.10.{i}"),
                           data.get(f"{p}.4.{i}"), data.get(f"{p}.5.{i}"), data.get(f"{p}.6.{i}"),
                           quality=data.get(f"{p}.20.{i}"))
                   for i, v in col(data, f"{p}.1").items()]
        mode = {1: "ap", 2: "station"}.get(data.get(f"{EPMP}.3.8.2.1.0"), "ap" if clients else None)
        width = ({1: 20, 2: 40, 3: 10, 4: 5}.get(data.get(f"{EPMP}.3.8.2.5.0"))   # AP: wirelessInterfaceHTMode
                 or {1: 20, 2: 40}.get(data.get(f"{EPMP}.1.2.2.0")))               # SM: cambiumSTAConnectedRFBandwidth
        radio = _radio(name, "epmp", mode, text(data.get(f"{EPMP}.3.8.2.2.0")) or text(data.get(f"{EPMP}.1.1.11.0")),
                       data.get(f"{EPMP}.1.2.1.0"), width, len(clients))
        return {"interfaces": [radio], "clients": clients[:MAX_CLIENTS]}
    return None


# ---------- health and UPS ----------

def health(data: dict, family: str, ups_: dict | None) -> dict | None:
    """Same keys as health.py. A UPS's battery voltage is its voltage_v, so a UPS can be a site's battery reading."""
    temp = None
    if family == "airmax":
        temp = num(data.get(f"{AIRMAX}.8.4.0"), -40, 150)
    elif family == "airfiber":
        temp = num(_first(col(data, f"{AIRFIBER}.2.1.8")), -40, 150)
    out = {"voltage_v": ups_.get("battery_v") if ups_ else None,
           "temperature_c": temp if temp is not None else (ups_.get("temperature_c") if ups_ else None),
           "cpu_temp_c": None, "power_w": None, "states": {}, "fans": {}}
    return out if any(v not in (None, {}) for v in out.values()) else None


def ups(data: dict, family: str) -> dict | None:
    """{source, on_battery, charge_pct, runtime_min, battery_v, input_v, load_pct, temperature_c, replace_battery}"""
    if family == "apc":
        a = APC_UPS
        status = data.get(f"{a}.4.1.1.0")                 # upsBasicOutputStatus: 2 onLine, 3 onBattery (others: boost, bypass, ...)
        hp_volt = num(data.get(f"{a}.2.3.4.0"), 1, 1000, 0.1)          # upsHighPrecBatteryActualVoltage (0.1 V)
        ticks = data.get(f"{a}.2.2.3.0")
        out = {"source": "apc", "on_battery": None if status is None else status == 3,
               "charge_pct": num(data.get(f"{a}.2.3.1.0"), 0, 100, 0.1) or num(data.get(f"{a}.2.2.1.0"), 0, 100),
               "runtime_min": ticks // 6000 if isinstance(ticks, int) and ticks >= 0 else None,
               "battery_v": hp_volt if hp_volt is not None else num(data.get(f"{a}.2.2.8.0"), 1, 1000),
               "input_v": num(data.get(f"{a}.3.3.1.0"), 0, 600, 0.1) or num(data.get(f"{a}.3.2.1.0"), 0, 600),
               "load_pct": num(data.get(f"{a}.4.3.3.0"), 0, 200, 0.1) or num(data.get(f"{a}.4.2.3.0"), 0, 200),
               "temperature_c": num(data.get(f"{a}.2.3.2.0"), -40, 150, 0.1) or num(data.get(f"{a}.2.2.2.0"), -40, 150),
               "replace_battery": None if data.get(f"{a}.2.2.4.0") is None else data.get(f"{a}.2.2.4.0") == 2}
    elif family == "cyberpower":
        c = CPS_UPS
        status = data.get(f"{c}.4.1.1.0")                 # upsBaseOutputStatus: 2 onLine, 3 onBattery, ...
        ticks = data.get(f"{c}.2.2.4.0")
        out = {"source": "cyberpower", "on_battery": None if status is None else status == 3,
               "charge_pct": num(data.get(f"{c}.2.2.1.0"), 0, 100),
               "runtime_min": ticks // 6000 if isinstance(ticks, int) and ticks >= 0 else None,
               "battery_v": num(data.get(f"{c}.2.2.2.0"), 1, 1000, 0.1),
               "input_v": num(data.get(f"{c}.3.2.1.0"), 0, 600, 0.1),
               "load_pct": num(data.get(f"{c}.4.2.3.0"), 0, 200),
               "temperature_c": num(data.get(f"{c}.2.2.3.0"), -40, 150),
               "replace_battery": None if data.get(f"{c}.2.2.5.0") is None else data.get(f"{c}.2.2.5.0") == 2}
    elif family == "ups":
        u = UPS_MIB
        source = data.get(f"{u}.4.1.0")                   # upsOutputSource: 3 normal, 5 battery, ...
        secs = data.get(f"{u}.2.2.0")                     # upsSecondsOnBattery (0 = on mains)
        out = {"source": "ups-mib",
               "on_battery": source == 5 if source is not None else (secs > 0 if isinstance(secs, int) else None),
               "charge_pct": num(data.get(f"{u}.2.4.0"), 0, 100),
               "runtime_min": num(data.get(f"{u}.2.3.0"), 0, 100_000, nd=0),
               "battery_v": num(data.get(f"{u}.2.5.0"), 1, 1000, 0.1),
               "input_v": num(_first(col(data, f"{u}.3.3.1.3")), 0, 600),
               "load_pct": num(_first(col(data, f"{u}.4.4.1.5")), 0, 200),
               "temperature_c": num(data.get(f"{u}.2.7.0"), -40, 150),
               "replace_battery": None}                  # the standard MIB only has low/depleted, not "replace"
    else:
        return None
    if out["runtime_min"] is not None:
        out["runtime_min"] = int(out["runtime_min"])
    return out if any(v is not None for k, v in out.items() if k != "source") else None
