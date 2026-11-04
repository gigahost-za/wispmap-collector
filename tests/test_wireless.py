"""Wireless (legacy, wifi, 60 GHz), sessions and security reads (collector 0.6.0; fixtures from the manuals)."""
import pytest

from tests.fakes import FakeApi
from wispmap_collector.security import poll_security
from wispmap_collector.sessions import poll_sessions
from wispmap_collector.wireless import (
    dbm,
    frequency,
    mbps,
    mode_of,
    percent,
    poll_wireless,
    width_mhz,
)

CLIENT = {"interface", "kind", "mac", "name", "ip", "signal_dbm", "tx_signal_dbm", "snr_db", "tx_rate_mbps",
          "rx_rate_mbps", "ccq_pct", "quality_pct", "uptime_s"}
IFACE = {"name", "kind", "mode", "ssid", "band", "frequency_mhz", "width_mhz", "running", "disabled", "clients"}


def test_legacy_sector():
    w = poll_wireless(FakeApi("docs-v6"))
    assert all(set(i) == IFACE for i in w["interfaces"]) and all(set(c) == CLIENT for c in w["clients"])
    wlan1, wlan2 = w["interfaces"]
    assert wlan1 == {"name": "wlan1", "kind": "legacy", "mode": "ap", "ssid": "T004-SEC-N", "band": "5ghz-a/n/ac",
                     "frequency_mhz": 5180, "width_mhz": 80, "running": True, "disabled": False, "clients": 3}
    assert (wlan2["mode"], wlan2["width_mhz"], wlan2["disabled"], wlan2["clients"]) == ("station", 20, True, 0)
    smit, plot, kerk = w["clients"]
    assert smit == {"interface": "wlan1", "kind": "legacy", "mac": "b8:69:f4:11:22:33", "name": "Smit Farm",
                    "ip": "10.20.1.15", "signal_dbm": -62.0, "tx_signal_dbm": -60.0, "snr_db": 38.0,
                    "tx_rate_mbps": 144.4, "rx_rate_mbps": 130.0, "ccq_pct": 91.0, "quality_pct": None,
                    "uptime_s": 3 * 86400 + 4 * 3600 + 12 * 60 + 30}
    assert (plot["name"], plot["ccq_pct"], plot["uptime_s"], plot["signal_dbm"]) == ("Plot 12 Mokoena", 54.0, 18760, -81.0)
    assert (kerk["rx_rate_mbps"], kerk["snr_db"], kerk["uptime_s"], kerk["ip"]) == (1300.0, 45.0, 9 * 86400, None)


def test_wifi_package():
    w = poll_wireless(FakeApi("docs-v7-wifi"))
    wifi1, wifi2 = w["interfaces"]
    assert (wifi1["kind"], wifi1["mode"], wifi1["frequency_mhz"], wifi1["width_mhz"], wifi1["clients"]) == ("wifi", "ap", 5500, 80, 2)
    assert (wifi2["frequency_mhz"], wifi2["running"], wifi2["clients"]) == (None, False, 0)
    a, b = w["clients"]
    assert (a["signal_dbm"], a["snr_db"], a["ccq_pct"], a["tx_rate_mbps"], a["uptime_s"]) == (-58.0, None, None, 390.0, 7384)
    assert (b["signal_dbm"], b["rx_rate_mbps"]) == (-74.0, 72.2)


def test_w60g_ptp_reads_monitor_per_interface():
    api = FakeApi("docs-w60g-ptp")
    w = poll_wireless(api)
    [i] = w["interfaces"]
    [c] = w["clients"]
    assert (i["kind"], i["mode"], i["band"], i["frequency_mhz"], i["clients"]) == ("w60g", "station", "60ghz", None, 1)
    assert c == {"interface": "wlan60-1", "kind": "w60g", "mac": "48:8f:5a:01:02:03", "name": None, "ip": None,
                 "signal_dbm": -58.0, "tx_signal_dbm": None, "snr_db": None, "tx_rate_mbps": 2300.0, "rx_rate_mbps": None,
                 "ccq_pct": None, "quality_pct": 80.0, "uptime_s": None}
    assert api.calls == [("interface_w60g", "monitor", {"numbers": "*1", "once": ""})]


def test_w60g_multipoint_uses_the_station_table():
    api = FakeApi("docs-w60g-ap")
    w = poll_wireless(api)
    assert [c["signal_dbm"] for c in w["clients"]] == [-61.0, -70.0] and api.calls == []
    assert w["interfaces"][0]["mode"] == "ap" and w["interfaces"][0]["clients"] == 2


def test_no_wireless_at_all():
    assert poll_wireless(FakeApi("chr")) is None and poll_wireless(FakeApi("docs-v7")) is None


@pytest.mark.parametrize("raw,expected", [("-62dBm@6Mbps", -62.0), ("-62", -62.0), (-62, -62.0), ("5", None),
                                          ("-130", None), (None, None), (True, None), ("n/a", None)])
def test_dbm(raw, expected):
    assert dbm(raw) == expected


def test_units():
    assert mbps("866.6Mbps-80MHz/2S/SGI") == 866.6 and mbps("2.3Gbps") == 2300.0 and mbps("54") == 54.0
    assert mbps("500kbps") == 0.5 and mbps("") is None and mbps(130) == 130.0
    assert percent("91%") == 91.0 and percent("140") is None
    assert frequency("5180/20-Ce/an") == 5180 and frequency("auto") is None and frequency("58320") == 58320
    assert width_mhz("20/40mhz-XX") == 40 and width_mhz("10mhz") == 10 and width_mhz(None) is None
    assert (mode_of("ap-bridge"), mode_of("station-pseudobridge"), mode_of("bridge"), mode_of(None)) == ("ap", "station", "ap", None)


def test_sessions():
    assert poll_sessions(FakeApi("docs-v6")) == {"pppoe": 3, "ppp_other": 1, "hotspot": None, "dhcp_bound": 2}
    assert poll_sessions(FakeApi("docs-v7-wifi")) == {"pppoe": None, "ppp_other": None, "hotspot": 2, "dhcp_bound": None}
    assert poll_sessions(FakeApi("chr")) is None        # captured 7.23.8 CHR: menus exist, no servers run


def test_security_facts_without_names_or_passwords():
    s = poll_security(FakeApi("docs-v6"), "wispmon")
    by = {x["name"]: x for x in s["services"]}
    assert by["telnet"] == {"name": "telnet", "port": 23, "disabled": False, "restricted": False}
    assert by["api"]["restricted"] and by["ftp"]["disabled"] and not by["winbox"]["restricted"]
    assert len(s["services"]) == 7 and "btest" not in by          # dynamic rows (7.23+: live connections) skipped
    assert (s["default_admin"], s["full_users"], s["own_policy"]) == (True, 2, ["api", "read", "test"])
    assert s["update"] == {"channel": "long-term", "installed": "6.49.10", "latest": "6.49.18"}
    assert (s["dns_remote"], s["socks"], s["proxy"], s["mac_winbox"]) == (True, False, True, "all")
    assert "noc" not in str(s) and "wispmon" not in str(s)                   # no user names leave the network
    assert poll_security(FakeApi("chr"), "wispmon") is None
    assert poll_security(FakeApi("docs-v6"), "someone-else")["own_policy"] is None
