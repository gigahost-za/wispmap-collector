import pytest
from wispmap_collector.parsing import parse_duration, parse_version, parse_mac


# ---------- parse_mac ----------
@pytest.mark.parametrize("raw,expected", [
    ("08:00:27:A1:33:24", "08:00:27:a1:33:24"),   # real TWR-A ether1
    ("08-00-27-a1-33-24", "08:00:27:a1:33:24"),   # dash style
    ("00:00:00:00:00:00", None),                  # loopback: never use for link matching
    ("", None),
    (None, None),
])
def test_parse_mac(raw, expected):
    assert parse_mac(raw) == expected


# ---------- parse_version ----------
@pytest.mark.parametrize("raw,expected", [
    ("7.23.8 (long-term)", "7.23.8"),                       # real /system/resource
    ("7.23.8 (long-term) 2026-10-08 09:46:16", "7.23.8"),   # real /ip/neighbor
    ("6.49.10 (long-term)", "6.49.10"),
    ("7.17beta2 (testing)", "7.17beta2"),
    (None, None),
])
def test_parse_version(raw, expected):
    assert parse_version(raw) == expected


# ---------- parse_duration ----------
@pytest.mark.parametrize("raw,expected", [
    ("1h5m15s", 3915),                                # real CHR 7.23
    ("45s", 45),
    ("1m5s", 65),
    ("3d4h5m6s", 3*86400 + 4*3600 + 5*60 + 6),
    ("1w2d3h4m5s", 9*86400 + 3*3600 + 4*60 + 5),
    ("5s300ms", 5),                                   # milliseconds dropped
    ("2w3d04:05:06", 17*86400 + 4*3600 + 5*60 + 6),   # older v6 style
    ("00:01:30", 90),
    ("", None),
    (None, None),
])
def test_parse_duration(raw, expected):
    assert parse_duration(raw) == expected


def test_parse_duration_rejects_garbage():
    with pytest.raises(ValueError):
        parse_duration("abc")
