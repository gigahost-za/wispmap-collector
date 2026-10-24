import asyncio

import pytest

from wispmap_collector.ping import build_fping_cmd, parse_fping, run_ping

# Real `fping -C 3 -p 200 -t 1000 -q ...` output from the lab (fping 5.1, stderr).
# Note the padding before " : " when host names have different lengths.
SAMPLE = """10.255.255.1  : 0.251 0.223 0.274
10.255.255.2  : 0.410 0.458 0.440
10.255.255.3  : 1.13 0.715 0.685
10.255.255.99 : - - -
192.168.1.254 : 1.01 0.981 1.01
"""


def test_build_cmd():
    assert build_fping_cmd(["a", "b"], count=3, interval_ms=200, timeout_ms=1000) == \
        ["fping", "-C", "3", "-p", "200", "-t", "1000", "-q", "a", "b"]


def test_parse_all_hosts_found():
    assert set(parse_fping(SAMPLE)) == {"10.255.255.1", "10.255.255.2", "10.255.255.3",
                                        "10.255.255.99", "192.168.1.254"}


def test_parse_all_ok():
    assert parse_fping(SAMPLE)["10.255.255.1"] == {
        "sent": 3, "recv": 3, "rtt_min_ms": 0.22, "rtt_avg_ms": 0.25, "rtt_max_ms": 0.27}


def test_parse_total_loss():
    assert parse_fping(SAMPLE)["10.255.255.99"] == {
        "sent": 3, "recv": 0, "rtt_min_ms": None, "rtt_avg_ms": None, "rtt_max_ms": None}


def test_parse_partial_loss():
    r = parse_fping("10.255.255.2 : 1.10 - 0.90\n")["10.255.255.2"]
    assert (r["sent"], r["recv"], r["rtt_avg_ms"]) == (3, 2, 1.0)


def test_ignores_icmp_and_dns_noise():
    noise = ("ICMP Host Unreachable from 192.168.1.201 for ICMP Echo sent to 10.0.9.9\n"
             "nosuchhost.invalid: Name or service not known\n")
    assert parse_fping(noise) == {}


def test_run_ping_no_hosts_skips_fping():
    assert asyncio.run(run_ping([])) == {}


@pytest.mark.lab
def test_run_ping_live_lab():
    """Needs the CHR lab. Skip with: pytest -m 'not lab'"""
    r = asyncio.run(run_ping(["10.255.255.1", "10.255.255.99"], count=2))
    assert r["10.255.255.1"]["recv"] == 2
    assert r["10.255.255.99"]["recv"] == 0
