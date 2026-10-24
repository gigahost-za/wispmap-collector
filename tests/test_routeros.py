from tests.fakes import FakeApi
from wispmap_collector.routeros import poll_system, poll_interfaces


def test_poll_system_normalises():
    s = poll_system(FakeApi("chr"))
    assert set(s) == {"uptime_s", "cpu_pct", "mem_used_pct", "ros_version", "board_name"}
    assert s["board_name"].startswith("CHR")          # VirtualBox: "CHR innotek GmbH VirtualBox"
    assert isinstance(s["uptime_s"], int) and s["uptime_s"] > 0
    assert 0 <= s["mem_used_pct"] <= 100
    assert s["ros_version"] == "7.23.8"


def test_poll_interfaces_counters():
    ifs = {i["name"]: i for i in poll_interfaces(FakeApi("chr"))}
    assert set(ifs) == {"ether1", "ether2", "ether3", "lo"}
    e1 = ifs["ether1"]
    assert e1["type"] == "ether"
    assert e1["mac"] == "08:00:27:fc:d4:96"           # real CHR-CORE ether1, lowercased
    assert isinstance(e1["rx_byte"], int)
    assert e1["running"] is True


def test_poll_interfaces_real_quirks():
    ifs = {i["name"]: i for i in poll_interfaces(FakeApi("chr"))}
    assert ifs["ether3"]["running"] is False          # L3 backup link is disabled
    assert ifs["lo"]["type"] == "loopback"
    assert ifs["lo"]["mac"] is None                   # 00:00:00:00:00:00 -> None (never link-match)
