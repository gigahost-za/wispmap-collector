from wispmap_collector.rates import RateCalculator


def test_first_sample_has_no_rate():
    rc = RateCalculator(max_gap_s=180)
    assert rc.rate("k", 0.0, 1000) is None


def test_rate_per_second():
    rc = RateCalculator(max_gap_s=180)
    rc.rate("k", 0.0, 1000)
    assert rc.rate("k", 60.0, 7000) == 100.0          # 6000 / 60 s


def test_counter_reset_returns_none_then_recovers():
    rc = RateCalculator(max_gap_s=180)
    rc.rate("k", 0.0, 5000)
    assert rc.rate("k", 60.0, 100) is None            # reboot: counter went down
    assert rc.rate("k", 120.0, 6100) == 100.0         # rebaselined on 100


def test_gap_too_long_returns_none():
    rc = RateCalculator(max_gap_s=180)
    rc.rate("k", 0.0, 0)
    assert rc.rate("k", 600.0, 60000) is None         # 10 min gap > 180 s


def test_time_not_moving_returns_none():
    rc = RateCalculator(max_gap_s=180)
    rc.rate("k", 10.0, 0)
    assert rc.rate("k", 10.0, 500) is None            # dt == 0 -> no division by zero


def test_keys_are_independent():
    rc = RateCalculator(max_gap_s=180)
    rc.rate("a", 0.0, 0)
    rc.rate("b", 0.0, 0)
    assert rc.rate("a", 10.0, 100) == 10.0
    assert rc.rate("b", 10.0, 500) == 50.0


def test_missing_counter_returns_none():
    rc = RateCalculator(max_gap_s=180)
    assert rc.rate("k", 0.0, None) is None            # interface type without this counter
    rc.rate("k", 10.0, 100)
    assert rc.rate("k", 20.0, None) is None
