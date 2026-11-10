"""SNMP (0.8.0): credential profiles, the vendor drivers, the poller and --capture-snmp.

Fixtures are written by hand from the vendors' MIBs (OIDs, units, enums); values are made up."""
import asyncio

import pytest
from pysnmp.proto.rfc1902 import Counter64, Integer32, IpAddress, OctetString, TimeTicks
from pysnmp.proto.rfc1905 import NoSuchInstance

from wispmap_collector import snmpdev
from wispmap_collector.config import Device
from wispmap_collector.runner import classify, poll_cycle
from wispmap_collector.snmp import SnmpAuthError, SnmpTimeout, convert, snmp_profile
from wispmap_collector.snmpcapture import fake_mac, line
from wispmap_collector.snmppoll import SnmpPoller
from wispmap_collector.sync import device_from_server

U = "1.3.6.1.4.1.41112.1"
AM, AF, LTU, AF60 = f"{U}.4", f"{U}.3", f"{U}.10.1", f"{U}.11.1"
EP = "1.3.6.1.4.1.17713.21"
APC, CPS, UPS = "1.3.6.1.4.1.318.1.1.1", "1.3.6.1.4.1.3808.1.1.1", "1.3.6.1.2.1.33.1"
IF, IFX = "1.3.6.1.2.1.2.2.1", "1.3.6.1.2.1.31.1.1.1"
STA = (0x24, 0x5A, 0x4C, 0x10, 0x20, 0x30)                    # a station MAC as table-index octets
STA_IDX = ".".join(str(b) for b in STA)
STA_MAC = "24:5a:4c:10:20:30"
SYSTEM = {"1.3.6.1.2.1.1.1.0": b"Linux 4.14", "1.3.6.1.2.1.1.3.0": 123456, "1.3.6.1.2.1.1.5.0": b"twr-north"}


def ifrow(idx, name, typ=6, oper=1, admin=1, hc_in=None, in32=None, mac=b"\x24\x5a\x4c\x00\x00\x01"):
    d = {f"{IF}.2.{idx}": name.encode(), f"{IF}.3.{idx}": typ, f"{IF}.6.{idx}": mac, f"{IF}.7.{idx}": admin,
         f"{IF}.8.{idx}": oper, f"{IFX}.1.{idx}": name.encode()}
    if hc_in is not None:
        d[f"{IFX}.6.{idx}"] = hc_in
    if in32 is not None:
        d[f"{IF}.10.{idx}"] = in32
    return d


# ---------- credential profiles ----------

def test_profiles_from_env():
    env = {"SNMP_COMMUNITY": "public", "SNMP_COMMUNITY_TOWERS": "t0wers", "SNMP_V3_USER_UPS": "mon",
           "SNMP_V3_AUTH_KEY_UPS": "authkey123", "SNMP_V3_PRIV_KEY_UPS": "privkey123", "SNMP_V3_AUTH_PROTO_UPS": "sha256"}
    assert snmp_profile("default", env).version == "2c"
    assert snmp_profile("default", env).community == "public"
    assert snmp_profile("towers", env).community == "t0wers"
    v3 = snmp_profile("ups", env)
    assert (v3.version, v3.user, v3.auth_proto, v3.priv_proto) == ("3", "mon", "SHA256", "AES")
    assert "authkey123" not in repr(v3) and "privkey123" not in repr(v3)       # keys never end up in logs
    assert snmp_profile("missing", env) is None
    assert snmp_profile("ups", {**env, "SNMP_V3_PRIV_PROTO_UPS": "rot13"}) is None
    assert snmp_profile("east-pop", {"SNMP_COMMUNITY_EAST_POP": "x"}).community == "x"


def test_convert_keeps_bytes_and_drops_missing():
    assert convert(Integer32(-61)) == -61
    assert convert(OctetString(b"\x24\x5a\x4c\x00\x00\x01")) == b"\x24\x5a\x4c\x00\x00\x01"
    assert convert(IpAddress("10.1.2.3")) == "10.1.2.3"
    assert convert(Counter64(2 ** 40)) == 2 ** 40
    assert convert(TimeTicks(500)) == 500
    assert convert(NoSuchInstance()) is None


# ---------- drivers ----------

def test_detect_by_vendor_tables():
    assert snmpdev.detect({**SYSTEM, f"{AM}.1.1.4.1": 5780}) == "airmax"
    assert snmpdev.detect({f"{AF}.1.1.5.1": 24100}) == "airfiber"
    assert snmpdev.detect({f"{LTU}.2.2.0": 5840}) == "ltu"
    assert snmpdev.detect({f"{AF60}.1.2.0": 60480}) == "af60"
    assert snmpdev.detect({f"{EP}.1.1.17.0": b"4.7"}) == "epmp"
    assert snmpdev.detect({f"{APC}.2.2.1.0": 100}) == "apc"
    assert snmpdev.detect({f"{CPS}.2.2.1.0": 100}) == "cyberpower"
    assert snmpdev.detect({f"{UPS}.2.4.0": 100}) == "ups"
    assert snmpdev.detect(SYSTEM) == "generic"


def test_interfaces_prefer_64_bit_counters_and_unique_names():
    data = {**ifrow(1, "eth0", hc_in=5_000_000_000, in32=10), **ifrow(2, "ath0", typ=71, in32=99),
            **ifrow(7, "eth0", oper=2, admin=2)}
    ifs = {i["name"]: i for i in snmpdev.interfaces(data)}
    assert set(ifs) == {"eth0", "ath0", "eth0 (7)"}
    assert ifs["eth0"]["rx_byte"] == 5_000_000_000                   # ifHCInOctets wins over the 32-bit counter
    assert ifs["ath0"]["rx_byte"] == 99 and ifs["ath0"]["type"] == "wlan"
    assert ifs["eth0 (7)"]["running"] is False and ifs["eth0 (7)"]["disabled"] is True
    assert ifs["eth0"]["mac"] == "24:5a:4c:00:00:01"


def test_interfaces_use_a_unique_descr_when_names_repeat():
    data = {f"{IF}.2.1": b"WLAN interface 1", f"{IFX}.1.1": b"WLAN interface", f"{IF}.2.2": b"WLAN interface 2",
            f"{IFX}.1.2": b"WLAN interface"}
    assert [i["name"] for i in snmpdev.interfaces(data)] == ["WLAN interface 1", "WLAN interface 2"]


def airmax_ap():
    p = f"{AM}.7.1"
    return {**SYSTEM, **ifrow(5, "ath0"), "1.2.840.10036.3.1.2.1.3.5": b"Rocket 5AC Prism",
            "1.2.840.10036.3.1.2.1.4.5": b"XC.qca955x.v8.7.11", f"{AM}.1.1.2.1": 2, f"{AM}.1.1.4.1": 5640,
            f"{AM}.5.1.2.1": b"TWR-NORTH-S1", f"{AM}.5.1.14.1": 20, f"{AM}.5.1.15.1": 1, f"{AM}.8.3.0": 7,
            f"{AM}.8.4.0": 41,
            f"{p}.2.1.{STA_IDX}": b"cust-smit", f"{p}.3.1.{STA_IDX}": -62, f"{p}.4.1.{STA_IDX}": -95,
            f"{p}.6.1.{STA_IDX}": 92, f"{p}.8.1.{STA_IDX}": 88, f"{p}.10.1.{STA_IDX}": "10.20.0.7",
            f"{p}.11.1.{STA_IDX}": 144_400_000, f"{p}.12.1.{STA_IDX}": 130_000_000, f"{p}.15.1.{STA_IDX}": 360_000}


def test_airmax_access_point():
    data = airmax_ap()
    ifs = snmpdev.interfaces(data)
    w = snmpdev.wireless(data, "airmax", ifs)
    radio, = w["interfaces"]
    assert (radio["name"], radio["mode"], radio["ssid"], radio["frequency_mhz"], radio["width_mhz"],
            radio["clients"]) == ("ath0", "ap", "TWR-NORTH-S1", 5640, 20, 1)
    c, = w["clients"]
    assert c["mac"] == STA_MAC and c["name"] == "cust-smit" and c["ip"] == "10.20.0.7"
    assert (c["signal_dbm"], c["snr_db"], c["ccq_pct"], c["quality_pct"], c["uptime_s"]) == (-62, 33, 92, 88, 3600)
    assert (c["tx_rate_mbps"], c["rx_rate_mbps"]) == (144.4, 130.0)          # airOS reports bit/s
    s = snmpdev.system(data, "airmax")
    assert (s["vendor"], s["model"], s["firmware"], s["cpu_pct"], s["ros_version"]) == \
        ("Ubiquiti", "Rocket 5AC Prism", "XC.qca955x.v8.7.11", 7, None)
    assert s["board_name"] == "Rocket 5AC Prism" and s["uptime_s"] == 1234
    assert snmpdev.health(data, "airmax", None)["temperature_c"] == 41


def test_airmax_station_reports_its_access_point():
    data = {**SYSTEM, **ifrow(5, "ath0"), f"{AM}.1.1.2.1": 1, f"{AM}.5.1.4.1": bytes(STA),
            f"{AM}.5.1.5.1": -58, f"{AM}.5.1.8.1": -96, f"{AM}.5.1.7.1": 97, f"{AM}.5.1.9.1": 300_000_000}
    w = snmpdev.wireless(data, "airmax", snmpdev.interfaces(data))
    assert w["interfaces"][0]["mode"] == "station"
    c, = w["clients"]
    assert (c["mac"], c["signal_dbm"], c["snr_db"], c["tx_rate_mbps"]) == (STA_MAC, -58, 38, 300.0)


def test_airmax_unknown_mode_stays_unknown():
    data = {**SYSTEM, f"{AM}.1.1.4.1": 5780}
    assert snmpdev.wireless(data, "airmax", [])["interfaces"][0]["mode"] is None


def test_airfiber_link_and_khz_frequency():
    p = f"{AF}.2.1"
    data = {**SYSTEM, **ifrow(4, "air0"), f"{AF}.1.1.5.1": 11_245_000, f"{p}.5.1": 700_000_000,
            f"{p}.6.1": 650_000_000, f"{p}.8.1": 38, f"{p}.11.1": -48, f"{p}.12.1": 1, f"{p}.14.1": -51,
            f"{p}.15.1": 1, f"{p}.19.1": -47, f"{p}.20.1": 1, f"{p}.22.1": -90, f"{p}.23.1": 2, f"{p}.26.1": 1,
            f"{p}.40.1": b"v4.1.0", f"{p}.44.1": 86400}
    w = snmpdev.wireless(data, "airfiber", snmpdev.interfaces(data))
    assert w["interfaces"][0]["frequency_mhz"] == 11245            # AF11 reports kHz
    c, = w["clients"]
    assert (c["signal_dbm"], c["tx_signal_dbm"]) == (-48, -47)     # best valid chain; the invalid -90 is ignored
    assert (c["tx_rate_mbps"], c["rx_rate_mbps"], c["uptime_s"]) == (650.0, 700.0, 86400)
    assert snmpdev.health(data, "airfiber", None)["temperature_c"] == 38
    assert snmpdev.system(data, "airfiber")["firmware"] == "v4.1.0"


def test_airfiber_link_down_has_no_peer():
    p = f"{AF}.2.1"
    data = {f"{p}.11.1": -100, f"{p}.12.1": 2, f"{p}.15.1": 2, f"{p}.26.1": 0}
    assert snmpdev.wireless(data, "airfiber", [])["clients"] == []


def test_ltu_access_point():
    p = f"{LTU}.4.1"
    data = {**SYSTEM, **ifrow(4, "ath0"), f"{LTU}.2.1.0": 0, f"{LTU}.2.2.0": 5860, f"{LTU}.2.4.0": 40,
            f"{LTU}.2.5.0": b"LTU-EAST", f"{LTU}.3.2.0": b"LTU Rocket", f"{LTU}.3.4.0": b"v2.5.1",
            f"{LTU}.3.5.0": 48, f"{LTU}.3.6.0": 12,
            f"{p}.3.{STA_IDX}": 210_000, f"{p}.4.{STA_IDX}": 190_000, f"{p}.5.{STA_IDX}": -66, f"{p}.6.{STA_IDX}": -64,
            f"{p}.13.{STA_IDX}": b"LTU-LR-dlamini", f"{p}.16.{STA_IDX}": -63, f"{p}.17.{STA_IDX}": -65,
            f"{p}.24.{STA_IDX}": 7200, f"{p}.25.{STA_IDX}": "10.30.0.9"}
    w = snmpdev.wireless(data, "ltu", snmpdev.interfaces(data))
    r = w["interfaces"][0]
    assert (r["mode"], r["ssid"], r["frequency_mhz"], r["width_mhz"], r["clients"]) == ("ap", "LTU-EAST", 5860, 40, 1)
    c, = w["clients"]
    assert (c["mac"], c["name"], c["signal_dbm"], c["tx_signal_dbm"], c["tx_rate_mbps"], c["uptime_s"]) == \
        (STA_MAC, "LTU-LR-dlamini", -64, -63, 210.0, 7200)
    s = snmpdev.system(data, "ltu")
    assert (s["model"], s["cpu_pct"], s["mem_used_pct"]) == ("LTU Rocket", 12, 48)


def test_af60_link():
    p = f"{AF60}.3.1"
    data = {**SYSTEM, **ifrow(1, "wlan0"), f"{AF60}.1.1.1": 1, f"{AF60}.1.2.1": 62640, f"{AF60}.1.3.1": 2160,
            f"{p}.3.{STA_IDX}": -52, f"{p}.4.{STA_IDX}": 21, f"{p}.7.{STA_IDX}": 1_800_000, f"{p}.8.{STA_IDX}": 1_700_000,
            f"{p}.17.{STA_IDX}": 900_000, f"{p}.18.{STA_IDX}": -53}
    w = snmpdev.wireless(data, "af60", snmpdev.interfaces(data))
    assert w["interfaces"][0]["mode"] == "station" and w["interfaces"][0]["width_mhz"] == 2160
    c, = w["clients"]
    assert (c["signal_dbm"], c["snr_db"], c["tx_signal_dbm"], c["tx_rate_mbps"], c["uptime_s"]) == \
        (-52, 21, -53, 1800.0, 9000)


def test_epmp_access_point():
    p = f"{EP}.1.2.30.1"
    data = {**SYSTEM, f"{IF}.2.1": b"WLAN interface 1", f"{IF}.3.1": 71, f"{EP}.3.8.2.1.0": 1,
            f"{EP}.3.8.2.2.0": b"EPMP-WEST", f"{EP}.3.8.2.5.0": 2, f"{EP}.1.2.1.0": 5520, f"{EP}.1.1.17.0": b"4.8.1",
            f"{EP}.2.1.64.0": 375, f"{EP}.1.1.4.0": b"0002:03:04:05",
            f"{p}.1.1": b"00:04:56:AB:CD:01", f"{p}.4.1": -67, f"{p}.5.1": -63, f"{p}.6.1": 29,
            f"{p}.10.1": "10.40.0.5", f"{p}.18.1": b"sm-naidoo", f"{p}.20.1": 96,
            f"{p}.1.2": b"00:04:56:AB:CD:02", f"{p}.4.2": -80, f"{p}.10.2": "0.0.0.0"}
    w = snmpdev.wireless(data, "epmp", snmpdev.interfaces(data))
    r = w["interfaces"][0]
    assert (r["mode"], r["ssid"], r["frequency_mhz"], r["width_mhz"], r["clients"]) == ("ap", "EPMP-WEST", 5520, 40, 2)
    a, b = sorted(w["clients"], key=lambda c: c["mac"])
    assert (a["mac"], a["name"], a["ip"], a["signal_dbm"], a["tx_signal_dbm"], a["snr_db"], a["quality_pct"]) == \
        ("00:04:56:ab:cd:01", "sm-naidoo", "10.40.0.5", -67, -63, 29, 96)
    assert b["ip"] is None                                     # 0.0.0.0 = not known
    s = snmpdev.system(data, "epmp")
    assert (s["vendor"], s["firmware"], s["cpu_pct"], s["uptime_s"]) == ("Cambium", "4.8.1", 37.5, 1234)
    del data["1.3.6.1.2.1.1.3.0"]
    assert snmpdev.system(data, "epmp")["uptime_s"] == ((2 * 24 + 3) * 60 + 4) * 60 + 5


def test_apc_ups_high_precision_values():
    data = {f"{APC}.1.1.1.0": b"Smart-UPS 1000", f"{APC}.1.2.1.0": b"UPS 09.3", f"{APC}.4.1.1.0": 3,
            f"{APC}.2.3.1.0": 874, f"{APC}.2.2.1.0": 87, f"{APC}.2.2.3.0": 252_000, f"{APC}.2.3.4.0": 254,
            f"{APC}.2.2.8.0": 25, f"{APC}.3.3.1.0": 0, f"{APC}.4.3.3.0": 312, f"{APC}.2.3.2.0": 296,
            f"{APC}.2.2.4.0": 1}
    u = snmpdev.ups(data, "apc")
    assert u == {"source": "apc", "on_battery": True, "charge_pct": 87.4, "runtime_min": 42, "battery_v": 25.4,
                 "input_v": None, "load_pct": 31.2, "temperature_c": 29.6, "replace_battery": False}
    h = snmpdev.health(data, "apc", u)
    assert h["voltage_v"] == 25.4 and h["temperature_c"] == 29.6
    s = snmpdev.system(data, "apc")
    assert (s["vendor"], s["model"], s["firmware"]) == ("APC", "Smart-UPS 1000", "UPS 09.3")


def test_cyberpower_ups():
    data = {f"{CPS}.4.1.1.0": 2, f"{CPS}.2.2.1.0": 100, f"{CPS}.2.2.4.0": 180_000, f"{CPS}.2.2.2.0": 272,
            f"{CPS}.3.2.1.0": 2310, f"{CPS}.4.2.3.0": 18, f"{CPS}.2.2.5.0": 2}
    u = snmpdev.ups(data, "cyberpower")
    assert (u["on_battery"], u["runtime_min"], u["battery_v"], u["input_v"], u["replace_battery"]) == \
        (False, 30, 27.2, 231.0, True)
    assert snmpdev.ups({f"{CPS}.4.1.1.0": 7, f"{CPS}.2.2.1.0": 90}, "cyberpower")["on_battery"] is False  # rebooting


def test_standard_ups_mib():
    data = {f"{UPS}.1.1.0": b"Mecer", f"{UPS}.1.2.0": b"ME-2000-WTU", f"{UPS}.4.1.0": 5, f"{UPS}.2.2.0": 420,
            f"{UPS}.2.4.0": 64, f"{UPS}.2.3.0": 38, f"{UPS}.2.5.0": 252, f"{UPS}.3.3.1.3.1": 0, f"{UPS}.4.4.1.5.1": 35,
            f"{UPS}.2.1.0": 3}
    u = snmpdev.ups(data, "ups")
    assert (u["on_battery"], u["charge_pct"], u["runtime_min"], u["battery_v"], u["load_pct"]) == \
        (True, 64, 38, 25.2, 35)
    assert u["replace_battery"] is None                       # "battery low" is not "replace the battery"
    s = snmpdev.system(data, "ups")
    assert (s["vendor"], s["model"]) == ("Mecer", "ME-2000-WTU")
    assert snmpdev.ups(SYSTEM, "ups") is None
    assert snmpdev.ups(data, "airmax") is None


def test_generic_device_cpu_from_host_resources():
    data = {**SYSTEM, "1.3.6.1.2.1.25.3.3.1.2.196608": 10, "1.3.6.1.2.1.25.3.3.1.2.196609": 30}
    s = snmpdev.system(data, "generic")
    assert s["cpu_pct"] == 20 and s["board_name"] == "Linux 4.14" and s["vendor"] is None
    assert snmpdev.wireless(data, "generic", []) is None and snmpdev.health(data, "generic", None) is None


# ---------- poller ----------

class FakeClient:
    """Answers from a dict like a device would; counts calls; can be told to fail."""

    def __init__(self, data, fail=None):
        self.data, self.fail, self.calls = data, fail, {"get": 0, "walk": 0, "present": 0}

    def _maybe_fail(self):
        if self.fail:
            raise self.fail

    async def get(self, host, port, profile, oids):
        self.calls["get"] += 1
        self._maybe_fail()
        return {o: self.data[o] for o in oids if o in self.data}

    async def walk(self, host, port, profile, base, max_rows=4000):
        self.calls["walk"] += 1
        self._maybe_fail()
        return {k: v for k, v in self.data.items() if k.startswith(base + ".")}

    async def present(self, host, port, profile, bases):
        self.calls["present"] += 1
        self._maybe_fail()
        return {b for b in bases if any(k.startswith(b + ".") for k in self.data)}

    def close(self):
        pass


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


ENV = {"SNMP_COMMUNITY": "public"}
DEV = Device(id="d1", host="10.0.0.20", kind="snmp")


def test_poll_result_shape_rates_and_cached_detection():
    data = {**airmax_ap(), f"{IFX}.6.5": 1_000_000, f"{IFX}.10.5": 2_000_000}
    client, clock = FakeClient(data), Clock()
    poller = SnmpPoller(60, client=client, clock=clock, env=ENV)
    first = asyncio.run(poller.poll(DEV))
    assert first["status"] == "ok" and first["type"] == "poll" and first["device_id"] == "d1"
    assert first["snmp"] == {"family": "airmax", "version": "2c"}
    assert first["interfaces"][0]["rx_bps"] is None              # no rate from one reading
    data[f"{IFX}.6.5"] += 7_500_000                              # 60 MB in 60 s = 1 Mbit/s
    clock.t += 60
    second = asyncio.run(poller.poll(DEV))
    assert second["interfaces"][0]["rx_bps"] == 1_000_000.0
    assert client.calls["present"] == 1                          # the family is remembered
    assert second["wireless"]["clients"][0]["mac"] == STA_MAC
    for key in ("routing", "poe", "bgp", "ups"):
        assert second[key] is None


def test_poll_without_credentials_says_what_to_set():
    r = asyncio.run(SnmpPoller(60, client=FakeClient(SYSTEM), env={}).poll(
        Device(id="d2", host="10.0.0.21", kind="snmp", credential_profile="towers")))
    assert r["status"] == "error" and "SNMP_COMMUNITY_TOWERS" in r["error"]


@pytest.mark.parametrize("exc, status", [(SnmpTimeout("x"), "timeout"), (SnmpAuthError("wrongDigest"), "auth_failed"),
                                         (OSError("no route"), "error"), (ValueError("bad"), "error")])
def test_poll_failures_are_results_not_crashes(exc, status):
    client = FakeClient(SYSTEM)
    poller = SnmpPoller(60, client=client, env=ENV)
    assert asyncio.run(poller.poll(DEV))["status"] == "ok"
    client.fail = exc
    r = asyncio.run(poller.poll(DEV))
    assert r["status"] == status and r["error"]
    assert "d1" not in poller.family                              # detect again once it answers
    client.fail = None
    asyncio.run(poller.poll(DEV))
    assert client.calls["present"] == 2


def test_v2c_timeout_hints_at_the_community():
    r = asyncio.run(SnmpPoller(60, client=FakeClient({}, fail=SnmpTimeout("t")), env=ENV).poll(DEV))
    assert "community" in r["error"]


def test_a_device_without_a_system_group_is_an_error():
    r = asyncio.run(SnmpPoller(60, client=FakeClient({}), env=ENV).poll(DEV))
    assert r["status"] == "error" and "system group" in r["error"]


def test_classify_snmp_auth():
    assert classify(SnmpAuthError("unknownUserName")) == "auth_failed"
    assert classify(SnmpTimeout("x")) == "timeout"


def test_poll_cycle_sends_snmp_devices_to_the_snmp_poller():
    class RosPoller:
        pool = None

        def __init__(self):
            self.synced = None

        def sync(self, devices):
            self.synced = [d.id for d in devices]

        def poll(self, d):
            return {"device_id": d.id, "via": "routeros"}

    class Snmp:
        def forget(self, keep):
            self.kept = keep

        async def poll_all(self, devices):
            return [{"device_id": d.id, "via": "snmp"} for d in devices]

    ros, snmp = RosPoller(), Snmp()
    devices = [Device(id="r1", host="10.0.0.1"), Device(id="s1", host="10.0.0.2", kind="snmp")]
    out = asyncio.run(poll_cycle(ros, devices, snmp))
    assert sorted((r["device_id"], r["via"]) for r in out) == [("r1", "routeros"), ("s1", "snmp")]
    assert ros.synced == ["r1"] and snmp.kept == {"s1"}


def test_snmp_device_from_config_and_server():
    d = Device(id="x", host="10.0.0.9", kind="snmp", snmp_port=1161, credential_profile="ups")
    assert d.poll is True and d.snmp_port == 1161
    s = device_from_server({"id": "y", "host": "10.0.0.10", "kind": "snmp", "snmp_port": 16100,
                            "credential_profile": "default", "future_field": 1})
    assert s.kind == "snmp" and s.snmp_port == 16100


# ---------- --capture-snmp ----------

def test_capture_scrubs_identities_but_keeps_the_numbers():
    sta_oid = f"{AM}.7.1.3.1.{STA_IDX}"
    out = line(sta_oid, Integer32(-61))
    fake = ".".join(str(b) for b in fake_mac(bytes(STA)))
    assert out == f"{AM}.7.1.3.1.{fake}|2|-61"                      # MAC index replaced, consistently
    assert line(f"{AM}.7.1.2.1.{STA_IDX}", OctetString(b"Jan Smit")).endswith("|4|<removed>")
    assert line("1.3.6.1.2.1.1.5.0", OctetString(b"twr-north")) == "1.3.6.1.2.1.1.5.0|4|<removed>"
    assert line(f"{AM}.7.1.10.1.{STA_IDX}", IpAddress("41.1.2.3")).split("|")[2].startswith("192.0.2.")
    mac_line = line(f"{EP}.1.2.30.1.1.1", OctetString(b"00:04:56:AB:CD:01"))
    assert "00:04:56" not in mac_line and mac_line.split("|")[2].startswith("02:")
    assert line(f"{IF}.6.2", OctetString(b"\x24\x5a\x4c\x00\x00\x01")).split("|")[1] == "4x"
    assert line("1.3.6.1.2.1.1.1.0", OctetString(b"Linux 4.14")) == "1.3.6.1.2.1.1.1.0|4|Linux 4.14"
    assert line("1.3.6.1.2.1.1.3.0", TimeTicks(500)) == "1.3.6.1.2.1.1.3.0|67|500"
