"""Turn cumulative counters into per-second rates."""


class RateCalculator:
    """Remembers the previous reading per key and returns the per-second rate.

    Returns None (never a wrong number) when there is no honest answer:
    first reading, missing value, counter went down (reboot/wrap),
    time didn't move forward, or the gap is longer than max_gap_s.
    """

    def __init__(self, max_gap_s: float):
        self.max_gap_s = max_gap_s
        self._prev: dict = {}          # key -> (ts, value) of the last reading

    def rate(self, key, ts: float, value: int | None) -> float | None:
        if value is None:
            return None
        prev = self._prev.get(key)
        self._prev[key] = (ts, value)  # always store: a post-reboot value becomes the new baseline
        if prev is None:
            return None
        prev_ts, prev_value = prev
        dt = ts - prev_ts
        dv = value - prev_value
        if dt <= 0 or dt > self.max_gap_s or dv < 0:
            return None
        return dv / dt
