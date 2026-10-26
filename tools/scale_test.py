"""Collector scale test: the real Poller / poll cycle / fping against hundreds of fake routers (tools/fakeros.py).

    python tools/scale_test.py --routers 300 --slow 15 --hung 10 --dropped 5 --refused 5 --core 2

Starts fakeros on --port, then runs --cycles poll cycles (the first one together with a neighbour cycle, as at
collector start-up) and one fping round, and reports per cycle: wall time, results by status, the slowest
healthy router, threads in use, CPU and memory. A poll cycle must finish well inside the poll interval (60 s).
"""
import argparse
import asyncio
import json
import os
import resource
import subprocess
import sys
import threading
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from wispmap_collector.config import Config, Device          # noqa: E402
from wispmap_collector.ping import run_ping                  # noqa: E402
from wispmap_collector import runner                         # noqa: E402

GROUPS = {"routers": 21, "slow": 22, "hung": 23, "dropped": 24, "core": 25, "refused": 26}


def hosts(octet: int, n: int):
    return [f"127.{octet}.{i // 250}.{i % 250 + 1}" for i in range(n)]


class Peak(threading.Thread):
    """Samples the number of live threads every 20 ms."""
    def __init__(self):
        super().__init__(daemon=True)
        self.peak, self.running = 0, True

    def run(self):
        while self.running:
            self.peak = max(self.peak, threading.active_count())
            time.sleep(0.02)


async def main(a):
    if a.threads:
        os.environ["WISPMAP_POLL_THREADS"] = str(a.threads)
    runner.raise_open_files()
    fake = subprocess.Popen([sys.executable, "-u", str(Path(__file__).with_name("fakeros.py")), "--port", str(a.port),
                             "--latency-ms", str(a.latency_ms)], stdout=subprocess.PIPE, text=True)
    try:
        assert "listening" in fake.stdout.readline(), "fakeros did not start"
        devices, group_of = [], {}
        for group, octet in GROUPS.items():
            for h in hosts(octet, getattr(a, group)):
                port = a.port + 1 if group == "refused" else a.port      # nothing listens on port+1
                devices.append(Device(id=h, host=h, api_port=port, name=h))
                group_of[h] = group
        cfg = Config(ping_s=30, fast_s=60, username="wispmon", password="fake", devices=devices)
        poller = runner.Poller(cfg)
        workers = getattr(runner, "poll_workers", lambda: min(32, (os.cpu_count() or 1) + 4))()
        print(f"{len(devices)} routers: " + ", ".join(f"{getattr(a, g)} {g}" for g in GROUPS if getattr(a, g))
              + f" | latency {a.latency_ms:g} ms | CPUs {os.cpu_count()}, poll threads {workers}", flush=True)
        summary = []
        for cycle in range(1, a.cycles + 1):
            peak = Peak()
            peak.start()
            t0, cpu0 = time.monotonic(), time.process_time()
            if cycle == 1 and not a.no_neighbors:          # start-up: poll + neighbour cycle at the same time
                polls, neigh = await asyncio.gather(runner.poll_cycle(poller, devices),
                                                    runner.neighbor_cycle(poller, devices))
            else:
                polls, neigh = await runner.poll_cycle(poller, devices), None
            wall, cpu = time.monotonic() - t0, time.process_time() - cpu0
            peak.running = False
            by = Counter((group_of[p["device_id"]], p["status"]) for p in polls)
            healthy = [p["duration_ms"] for p in polls if group_of[p["device_id"]] in ("routers", "core")]
            row = {"cycle": cycle, "wall_s": round(wall, 2), "cpu_s": round(cpu, 2), "threads_peak": peak.peak,
                   "statuses": {f"{g}:{s}": n for (g, s), n in sorted(by.items())},
                   "healthy_max_ms": max(healthy, default=None),
                   "neighbors": None if neigh is None else len(neigh)}
            summary.append(row)
            print(json.dumps(row), flush=True)
            if cycle < a.cycles:
                await asyncio.sleep(a.gap)
        ping_hosts = [d.host for d in devices] + [f"192.0.2.{i + 1}" for i in range(a.ping_dead)]
        t0 = time.monotonic()
        pings = await run_ping(ping_hosts)
        lost = sum(1 for h in ping_hosts if pings.get(h, {}).get("recv", 0) == 0)
        print(json.dumps({"fping_hosts": len(ping_hosts), "fping_wall_s": round(time.monotonic() - t0, 2),
                          "no_reply": lost}), flush=True)
        print(json.dumps({"rss_mb": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1),
                          "sessions_open": len(poller.sessions)}), flush=True)
        poller.close()
    finally:
        fake.terminate()
        try:
            print(fake.communicate(timeout=10)[0].strip(), flush=True)
        except (BrokenPipeError, ValueError):
            pass


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    for g in GROUPS:
        p.add_argument(f"--{g}", type=int, default=0)
    p.add_argument("--cycles", type=int, default=3)
    p.add_argument("--gap", type=float, default=2.0, help="seconds between cycles (the real gap is 60 - cycle time)")
    p.add_argument("--ping-dead", type=int, default=0, help="extra unanswered hosts (192.0.2.x) for the fping round")
    p.add_argument("--no-neighbors", action="store_true")
    p.add_argument("--port", type=int, default=18728)
    p.add_argument("--latency-ms", type=float, default=0.0, help="reply delay of healthy fake routers (radio hops)")
    p.add_argument("--threads", type=int, default=0, help="WISPMAP_POLL_THREADS for this run")
    asyncio.run(main(p.parse_args()))
