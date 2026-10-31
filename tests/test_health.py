from librouteros.exceptions import TrapError

from tests.fakes import FakeApi
from tests.test_runner import Clock, make_cfg
from wispmap_collector.capture import capture
from wispmap_collector.config import Device
from wispmap_collector.health import normalise_health, poll_health
from wispmap_collector.runner import Poller


def test_chr_has_no_sensors():
    assert poll_health(FakeApi("chr")) is None          # real CHR answer: one settings row, no readings


def test_v7_one_row_per_sensor():
    h = poll_health(FakeApi("docs-v7"))
    assert (h["voltage_v"], h["temperature_c"], h["cpu_temp_c"], h["power_w"]) == (24.3, 38.0, 51.0, 18.4)
    assert h["states"] == {"psu1": "ok", "psu2": "fail"} and h["fans"] == {"fan1": 5340, "fan2": 0}


def test_v6_one_row_with_settings_mixed_in():
    h = poll_health(FakeApi("docs-v6"))
    assert (h["voltage_v"], h["temperature_c"], h["cpu_temp_c"], h["power_w"]) == (24.1, 35.0, 47.0, 4.3)
    assert h["states"] == {"psu1": "ok", "psu2": "ok"} and h["fans"] == {"fan1": 4560}


def test_units_and_nonsense():
    h = normalise_health({"temperature": ("100.4", "F"), "voltage": ("0", "V"), "board-temperature1": (40, "C")})
    assert h["temperature_c"] == 38.0 and h["voltage_v"] is None              # 100.4 °F; 0 V is no reading
    assert normalise_health({"voltage": (True, "")}) is None
    assert normalise_health({"voltage": ("24.0V", "")})["voltage_v"] == 24.0   # stray unit suffix


class ApiWithHealth(FakeApi):
    def __init__(self, health):
        super().__init__("chr")
        self.health = health

    def path(self, *parts):
        if parts == ("system", "health"):
            if isinstance(self.health, Exception):
                raise self.health
            return iter(self.health)
        return super().path(*parts)


def test_poll_carries_health_and_never_fails_on_it():
    d = Device(id="r1", name="R1", host="10.0.0.1")
    cases = [([{"name": "voltage", "value": "27.3", "type": "V"}], {"voltage_v": 27.3}),
             (TrapError(message="no such command prefix"), None)]
    for health, expect in cases:
        poller = Poller(make_cfg(d), clock=Clock(), connect=lambda h=health, **kw: ApiWithHealth(h))
        r = poller.poll(d)
        assert r["status"] == "ok" and r["interfaces"]
        assert (r["health"] or {}).get("voltage_v") == (expect or {}).get("voltage_v")
        assert (r["health"] is None) == (expect is None)


def test_capture_scrubs_identifiers():
    class Api(FakeApi):
        def path(self, *parts):
            if parts == ("system", "routerboard"):
                return iter([{"model": "hAP ax2", "serial-number": "HD1234567", "current-firmware": "7.16"}])
            return super().path(*parts)
    out = capture(Api("chr"))
    assert out["system_routerboard"] == [{"model": "hAP ax2", "serial-number": "<removed>", "current-firmware": "7.16"}]
    assert out["system_health"] == [{"state": "disabled", "state-after-reboot": "enabled"}]
    assert out["interface_ethernet_poe"]["error"] == "TrapError"      # missing menu (as on CHR): recorded, not fatal
