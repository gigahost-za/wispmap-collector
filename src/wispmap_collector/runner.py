"""Collector runner: ping loop + API poll loop on one asyncio event loop.

Every result is printed as one JSON object per line (JSON Lines) on stdout,
shaped like the PingResult / PollResult payloads in spec doc 03 §5.
Human-readable messages go to stderr so stdout stays machine-readable.
"""
import asyncio
import errno
import json
import os
import signal
import sys
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from librouteros import connect as ros_connect
from librouteros.exceptions import ConnectionClosed, MultiTrapError, TrapError

from .config import Config, Device
from .ping import run_ping
from .rates import RateCalculator
from .routeros import poll_interfaces, poll_neighbors, poll_system
from .routing import poll_routing

# RouterOS keeps an API session alive after the user's password changes, so a reused session
# hides credential problems until the next reconnect. Log in afresh this often (seconds).
SESSION_MAX_AGE_S = 6 * 3600

# librouteros blocks, so every device poll runs in a thread. Python's default pool has only cpu+4 threads (8 on a
# 4-core box): a hung router holds one for the 5-s timeout, a slow one for ~10 s, and 35 bad routers out of 337
# took the poll cycle from 3 s to 32-47 s (tools/scale_test.py). Waiting threads cost almost nothing, so use many.
POLL_THREADS = 64


def poll_workers() -> int:
    """Poll threads: WISPMAP_POLL_THREADS (1-512), default POLL_THREADS."""
    try:
        n = int(os.environ.get("WISPMAP_POLL_THREADS", POLL_THREADS))
    except ValueError:
        n = POLL_THREADS
    return max(1, min(n, 512))


class BufferedTransport:
    """librouteros reads a word's length and then its body with separate recv() calls: ~570 per poll. Each call
    releases and re-takes Python's interpreter lock, and with 64 poll threads that hand-off cost more CPU than the
    polling itself. Read up to 64 KB at a time and cut words from memory instead (an offset, not deleting from the
    front, so a 2,000-route reply stays linear)."""

    CHUNK = 65536

    def __init__(self, sock):
        self.sock = sock
        self.buf = b""
        self.pos = 0

    def read(self, length: int) -> bytes:
        while len(self.buf) - self.pos < length:
            chunk = self.sock.recv(self.CHUNK)          # the socket timeout (5 s) still applies to every recv
            if not chunk:
                raise ConnectionClosed("Connection unexpectedly closed.")
            self.buf = self.buf[self.pos:] + chunk
            self.pos = 0
        out = self.buf[self.pos:self.pos + length]
        self.pos += length
        return out

    def write(self, data: bytes) -> None:
        self.sock.sendall(data)

    def close(self) -> None:
        self.sock.close()


def connect_buffered(**kwargs):
    """librouteros.connect, then buffered reads for the rest of the session (login itself is a few words)."""
    api = ros_connect(**kwargs)
    transport = getattr(getattr(api, "protocol", None), "transport", None)
    if transport is not None and hasattr(transport, "sock"):
        api.protocol.transport = BufferedTransport(transport.sock)
    return api


OPEN_FILES = 65536


def raise_open_files(want: int = OPEN_FILES) -> int:
    """Every router keeps an API session open: one socket each. The usual soft limit of 1,024 open files (also inside
    Docker) ran out at ~1,000 routers ("Too many open files"). Raise the soft limit towards the hard one (524,288 on
    Ubuntu and in Docker). Returns the soft limit now in force, -1 if it can't be read (not Linux)."""
    try:
        import resource
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        target = want if hard == resource.RLIM_INFINITY else min(want, hard)
        if soft != resource.RLIM_INFINITY and soft < target:
            resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
        return resource.getrlimit(resource.RLIMIT_NOFILE)[0]
    except (ImportError, ValueError, OSError):
        return -1


LOST = {"sent": 0, "recv": 0, "rtt_min_ms": None, "rtt_avg_ms": None, "rtt_max_ms": None}

# (raw counter, output field, multiplier): bytes become bits, the rest stay per-second
_RATES = (
    ("rx_byte", "rx_bps", 8), ("tx_byte", "tx_bps", 8),
    ("rx_packet", "rx_pps", 1), ("tx_packet", "tx_pps", 1),
    ("rx_error", "rx_err_ps", 1), ("tx_error", "tx_err_ps", 1),
    ("rx_drop", "rx_drop_ps", 1), ("tx_drop", "tx_drop_ps", 1),
)


def now_iso() -> str:
    """Wall-clock UTC timestamp for the payload (rates use the monotonic clock instead)."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def emit(obj: dict, out=None) -> None:
    print(json.dumps(obj), file=out or sys.stdout, flush=True)


def classify(exc: Exception) -> str:
    """Map an exception to a PollResult status (spec doc 03 §5)."""
    if isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, ConnectionRefusedError):
        return "refused"
    if isinstance(exc, (TrapError, MultiTrapError)) and "invalid user name or password" in str(exc):
        return "auth_failed"
    if isinstance(exc, OSError) and exc.errno in (errno.EHOSTUNREACH, errno.ENETUNREACH):
        return "unreachable"
    return "error"


# ---------- ping ----------

async def ping_cycle(cfg: Config) -> list[dict]:
    devices = [d for d in cfg.devices if d.ping]
    ts = now_iso()
    results = await run_ping([d.host for d in devices])
    return [{"type": "ping", "device_id": d.id, "ts": ts, **results.get(d.host, LOST)} for d in devices]


# ---------- API polling ----------

class Poller:
    """Polls RouterOS devices. One persistent API session per device, dropped on any error
    and renewed after session_max_age_s."""

    def __init__(self, cfg: Config, connect=connect_buffered, clock=time.monotonic,
                 session_max_age_s: float = SESSION_MAX_AGE_S):
        self.cfg = cfg
        self.connect = connect            # injectable so tests can pass a fake
        self.clock = clock                # injectable so tests control time
        self.session_max_age_s = session_max_age_s
        self.sessions: dict[str, object] = {}
        self.session_born: dict[str, float] = {}
        self.session_addr: dict[str, tuple] = {}   # (host, port) each session was opened to
        # one API session per device, used by one thread at a time (poll and neighbor cycles can overlap)
        self.locks: defaultdict[str, threading.Lock] = defaultdict(threading.Lock)
        self.rates = RateCalculator(max_gap_s=3 * cfg.fast_s)
        self.pool = ThreadPoolExecutor(max_workers=poll_workers(), thread_name_prefix="poll")

    def _session(self, d: Device):
        if d.credential_profile != "default":      # only one local profile so far (spec doc 03 §2)
            raise LookupError(f"unknown credential profile {d.credential_profile!r}")
        now = self.clock()
        if d.id in self.sessions and now - self.session_born[d.id] > self.session_max_age_s:
            self._drop(d)                  # periodic fresh login surfaces changed passwords
        if d.id not in self.sessions:
            self.sessions[d.id] = self.connect(host=d.host, port=d.api_port, username=self.cfg.username,
                                               password=self.cfg.password, timeout=5)
            self.session_born[d.id] = now
            self.session_addr[d.id] = (d.host, d.api_port)
        return self.sessions[d.id]

    def _drop(self, d: Device) -> None:
        self._drop_id(d.id)

    def _drop_id(self, dev_id: str) -> None:
        self.session_born.pop(dev_id, None)
        self.session_addr.pop(dev_id, None)
        api = self.sessions.pop(dev_id, None)
        if api is not None:
            try:
                api.close()
            except Exception:
                pass                       # the connection is already broken; nothing to save

    def close(self) -> None:
        for dev_id in list(self.sessions):
            self._drop_id(dev_id)
        self.pool.shutdown(wait=False, cancel_futures=True)

    def sync(self, devices: list[Device]) -> None:
        """After a config reload: close sessions of devices that are gone or whose address changed."""
        wanted = {d.id: (d.host, d.api_port) for d in devices}
        for dev_id in list(self.sessions):
            if self.session_addr.get(dev_id) != wanted.get(dev_id):
                self._drop_id(dev_id)

    def poll_neighbors(self, d: Device) -> dict | None:
        """Neighbor table of one device, or None if it can't be read (the next cycle tries again)."""
        with self.locks[d.id]:
            try:
                neighbors = poll_neighbors(self._session(d))
            except Exception:
                self._drop(d)
                return None
        return {"type": "neighbors", "device_id": d.id, "ts": now_iso(), "neighbors": neighbors}

    def poll(self, d: Device) -> dict:
        with self.locks[d.id]:
            return self._poll(d)

    def _poll(self, d: Device) -> dict:
        ts, t0 = now_iso(), self.clock()
        base = {"type": "poll", "device_id": d.id, "ts": ts, "cadence": "fast"}
        try:
            api = self._session(d)
            system = poll_system(api)
            raw_ifs = poll_interfaces(api)
        except Exception as exc:           # broad on purpose: one bad device must never stop the loop
            self._drop(d)
            return {**base, "status": classify(exc), "error": str(exc) or type(exc).__name__,
                    "duration_ms": int((self.clock() - t0) * 1000)}
        routing = self._routing(d, api)
        now = self.clock()
        return {**base, "status": "ok", "error": None,
                "duration_ms": int((now - t0) * 1000),
                "system": system,
                "interfaces": [self._with_rates(d, i, now) for i in raw_ifs],
                "routing": routing}

    def _routing(self, d: Device, api) -> dict | None:
        """OSPF state, or None if this router can't tell us (no routing menu, no permission, read failed).
        Never fails the poll: ping/system/interfaces are still good data."""
        try:
            return poll_routing(api)
        except (TrapError, MultiTrapError):
            return None                    # menu missing / not allowed: the session itself is fine
        except Exception:
            self._drop(d)                  # timeout or broken connection: reconnect next cycle
            return None

    def _with_rates(self, d: Device, i: dict, now: float) -> dict:
        out = {k: i[k] for k in ("name", "type", "mac", "running", "disabled", "link_downs")}
        for counter, field, mult in _RATES:
            r = self.rates.rate((d.id, i["name"], counter), now, i[counter])
            out[field] = None if r is None else round(r * mult, 2)
        return out


def _in_threads(poller, fn, devices):
    """Run fn(device) for every device in the poller's own thread pool (librouteros blocks)."""
    loop = asyncio.get_running_loop()
    pool = getattr(poller, "pool", None)               # None: the default pool (test doubles without one)
    return asyncio.gather(*(loop.run_in_executor(pool, fn, d) for d in devices))


async def poll_cycle(poller: Poller, devices: list[Device]) -> list[dict]:
    poller.sync(devices)
    return list(await _in_threads(poller, poller.poll, devices))


async def neighbor_cycle(poller: Poller, devices: list[Device]) -> list[dict]:
    results = await _in_threads(poller, poller.poll_neighbors, devices)
    return [r for r in results if r is not None]


# ---------- loops ----------

async def every(interval_s, cycle, sink=None) -> None:
    """Run `cycle()` every interval_s seconds without drifting; never let one failure stop the loop.
    interval_s may be a function, so a config reload can change it. Results go to sink (default: stdout)."""
    sink = sink or emit
    while True:
        started = time.monotonic()
        try:
            for result in await cycle():
                sink(result)
        except Exception as exc:
            sink({"type": "collector_error", "ts": now_iso(), "error": f"{type(exc).__name__}: {exc}"})
        interval = interval_s() if callable(interval_s) else interval_s
        await asyncio.sleep(max(0.0, interval - (time.monotonic() - started)))


async def run(cfg: Config, sink=None, background=()) -> None:
    """Ping + poll loops (and any background coroutines, e.g. the uplink) until SIGINT/SIGTERM.
    Device lists and intervals are read from cfg on every cycle, so config reloads apply live."""
    open_files = raise_open_files()
    poller = Poller(cfg)
    polled = [d for d in cfg.devices if d.poll]
    print(f"wispmap-collector: {len(cfg.devices)} devices ({len(polled)} polled), "
          f"ping every {cfg.ping_s}s, poll every {cfg.fast_s}s, {poll_workers()} poll threads, "
          f"open files {open_files}", file=sys.stderr)

    # Stop cleanly on Ctrl+C (SIGINT) and `docker stop` / systemd (SIGTERM).
    # Registering explicitly also works when the shell started us with SIGINT ignored.
    loop = asyncio.get_running_loop()
    main_task = asyncio.current_task()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, main_task.cancel)

    try:
        await asyncio.gather(
            every(lambda: cfg.ping_s, lambda: ping_cycle(cfg), sink),
            every(lambda: cfg.fast_s, lambda: poll_cycle(poller, [d for d in cfg.devices if d.poll]), sink),
            every(lambda: cfg.neighbors_s, lambda: neighbor_cycle(poller, [d for d in cfg.devices if d.poll]), sink),
            *background,
        )
    except asyncio.CancelledError:
        print("wispmap-collector: stopping", file=sys.stderr)
    finally:
        poller.close()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(sig)
