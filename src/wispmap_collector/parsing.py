"""Turn raw RouterOS strings into clean values."""
import re

_UNITS = {"w": 604800, "d": 86400, "h": 3600, "m": 60, "s": 1, "ms": 0}
_TOKEN = re.compile(r"(\d+)(ms|w|d|h|m|s)")
_HMS = re.compile(r"(\d+):(\d{2}):(\d{2})(?:\.\d+)?$")


def parse_mac(raw: str | None) -> str | None:
    if not raw:                                       # catches None and ""
        return None
    mac = raw.strip().lower().replace("-", ":")
    return None if mac == "00:00:00:00:00:00" else mac



def parse_version(raw: str | None) -> str | None:
    if not raw:
        return None
    return raw.split()[0]


def parse_duration(raw: str | None) -> int | None:
    """RouterOS duration ('1w2d3h4m5s', '2d04:05:06', '5s300ms') -> whole seconds."""
    if not raw:
        return None
    total, rest = 0, raw.strip()

    if m := _HMS.search(rest):                 # clock tail present?
        h, mi, s = map(int, m.groups())        # ('04','05','06') -> 4, 5, 6
        total += h * 3600 + mi * 60 + s
        rest = rest[: m.start()]               # keep only the part before it

    pos = 0
    for tok in _TOKEN.finditer(rest):
        if tok.start() != pos:                       # gap before this pair
            raise ValueError(f"bad duration: {raw!r}")
        total += int(tok.group(1)) * _UNITS[tok.group(2)]
        pos = tok.end()
    if pos != len(rest):                             # leftovers at the end (or nothing matched)
        raise ValueError(f"bad duration: {raw!r}")
    return total
