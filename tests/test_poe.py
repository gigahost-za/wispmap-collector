"""PoE-out: port list + monitor -> the poll's "poe" list (fixtures written from the manual, not captured)."""
from librouteros.exceptions import TrapError

from tests.fakes import FakeApi
from wispmap_collector.poe import (
    milliamps,
    normalise_port,
    poll_poe,
    status,
    volts,
    watts,
)

KEYS = {"interface", "mode", "status", "voltage_v", "current_ma", "power_w"}


def test_ports_from_the_v7_manual():
    api = FakeApi("docs-v7")
    ports = poll_poe(api)
    assert all(set(p) == KEYS for p in ports)
    by = {p["interface"]: p for p in ports}
    assert by["ether2"] == {"interface": "ether2", "mode": "auto_on", "status": "powered_on", "voltage_v": 24.1,
                            "current_ma": 254.0, "power_w": 6.1}
    assert by["ether3"]["status"] == "short_circuit" and by["ether3"]["power_w"] is None
    assert by["ether4"]["mode"] == "forced_on"
    assert (by["ether5"]["status"], by["ether6"]["status"], by["ether6"]["mode"]) == ("waiting_for_load", "disabled", "off")
    [(name, cmd, kwargs)] = api.calls
    assert (name, cmd, kwargs) == ("interface_ethernet_poe", "monitor", {"numbers": "*2,*3,*4,*5,*6", "once": ""})


def test_values_with_or_without_units():
    assert volts("24.1V") == volts("24.1") == volts(24.1) == 24.1
    assert milliamps("254mA") == milliamps(254) == milliamps("0.254A") == 254.0
    assert watts("6.1 W") == watts("6.1") == 6.1
    assert volts("230V") is None and milliamps("9A") is None and watts(True) is None   # nonsense -> None
    assert volts("n/a") is None and status("") is None and status("Voltage-Too-Low") == "voltage_too_low"


def test_reading_missing_for_a_port():
    assert normalise_port({"name": "ether7", "poe-out": "auto-on"}, {}) == {
        "interface": "ether7", "mode": "auto_on", "status": None, "voltage_v": None, "current_ma": None,
        "power_w": None}


class NoPoe:
    def path(self, *parts):
        raise TrapError("no such command prefix")


class EmptyPoe:
    def path(self, *parts):
        return iter([])


def test_no_poe_menu_or_no_ports():
    assert poll_poe(EmptyPoe()) is None
    try:
        poll_poe(NoPoe())
    except TrapError:
        pass                                  # the runner turns this into None (test_runner)
    else:
        raise AssertionError("expected TrapError")
