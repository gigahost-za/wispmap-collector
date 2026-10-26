"""ICMP reachability via fping: one process pings every host in parallel."""
import asyncio


class FpingError(RuntimeError):
    """fping itself failed (bad arguments, missing binary, no permission)."""


def build_fping_cmd(hosts, count: int, interval_ms: int, timeout_ms: int, spacing_ms: int = 1) -> list[str]:
    # -C n : send n pings per host and print every RTT (lost ones as "-")
    # -p ms: gap between pings to the same host
    # -t ms: how long to wait for each reply
    # -i ms: gap between any two pings. fping's default 10 ms made a round grow with the host count (500 hosts:
    #        28 s, close to the 30-s ping interval); 1 ms: 5 s. Allowed without root (checked in the container).
    # -q   : quiet, only the per-host summary lines
    return ["fping", "-C", str(count), "-p", str(interval_ms), "-t", str(timeout_ms), "-i", str(spacing_ms),
            "-q", *hosts]


def parse_fping(output: str) -> dict[str, dict]:
    """Parse `fping -C` summary lines ("host : 0.25 - 0.27") into per-host stats."""
    results = {}
    for line in output.splitlines():
        host, sep, samples = line.partition(" : ")
        if not sep:                      # not a summary line (ICMP noise, DNS errors)
            continue
        values = samples.split()
        rtts = [float(v) for v in values if v != "-"]
        results[host.strip()] = {
            "sent": len(values),
            "recv": len(rtts),
            "rtt_min_ms": round(min(rtts), 2) if rtts else None,
            "rtt_avg_ms": round(sum(rtts) / len(rtts), 2) if rtts else None,
            "rtt_max_ms": round(max(rtts), 2) if rtts else None,
        }
    return results


async def run_ping(hosts, count: int = 3, interval_ms: int = 200, timeout_ms: int = 1000) -> dict[str, dict]:
    """Ping all hosts in one fping call. Hosts missing from the result count as total loss for the caller."""
    if not hosts:
        return {}
    proc = await asyncio.create_subprocess_exec(
        *build_fping_cmd(hosts, count, interval_ms, timeout_ms),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, err = await proc.communicate()
    # exit 0 = all alive, 1 = some unreachable, 2 = some names didn't resolve -> all normal for monitoring
    if proc.returncode not in (0, 1, 2):
        raise FpingError(f"fping exited {proc.returncode}: {err.decode(errors='replace').strip()}")
    return parse_fping(err.decode(errors="replace"))
