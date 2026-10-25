"""Collector runner: ping loop + API poll loop on one asyncio event loop.

Every result is printed as one JSON object per line (JSON Lines) on stdout,
shaped like the PingResult / PollResult payloads in spec doc 03 §5.
Human-readable messages go to stderr so stdout stays machine-readable.
"""
import asyncio
import errno
import json
import signal
import sys
import time
from datetime import datetime, timezone

from librouteros import connect as ros_connect
from librouteros.exceptions import MultiTrapError, TrapError

from .config import Config, Device
from .ping import run_ping
from .rates import RateCalculator
from .routeros import poll_interfaces, poll_system

# RouterOS keeps an API session alive after the user's password changes, so a reused session
# hides credential problems until the next reconnect. Log in afresh this often (seconds).
SESSION_MAX_AGE_S = 6 * 3600

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

    def __init__(self, cfg: Config, connect=ros_connect, clock=time.monotonic,
                 session_max_age_s: float = SESSION_MAX_AGE_S):
        self.cfg = cfg
        self.connect = connect            # injectable so tests can pass a fake
        self.clock = clock                # injectable so tests control time
        self.session_max_age_s = session_max_age_s
        self.sessions: dict[str, object] = {}
        self.session_born: dict[str, float] = {}
        self.session_addr: dict[str, tuple] = {}   # (host, port) each session was opened to
        self.rates = RateCalculator(max_gap_s=3 * cfg.fast_s)

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

    def sync(self, devices: list[Device]) -> None:
        """After a config reload: close sessions of devices that are gone or whose address changed."""
        wanted = {d.id: (d.host, d.api_port) for d in devices}
        for dev_id in list(self.sessions):
            if self.session_addr.get(dev_id) != wanted.get(dev_id):
                self._drop_id(dev_id)

    def poll(self, d: Device) -> dict:
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
        now = self.clock()
        return {**base, "status": "ok", "error": None,
                "duration_ms": int((now - t0) * 1000),
                "system": system,
                "interfaces": [self._with_rates(d, i, now) for i in raw_ifs]}

    def _with_rates(self, d: Device, i: dict, now: float) -> dict:
        out = {k: i[k] for k in ("name", "type", "mac", "running", "disabled", "link_downs")}
        for counter, field, mult in _RATES:
            r = self.rates.rate((d.id, i["name"], counter), now, i[counter])
            out[field] = None if r is None else round(r * mult, 2)
        return out


async def poll_cycle(poller: Poller, devices: list[Device]) -> list[dict]:
    poller.sync(devices)
    # librouteros blocks, so each device is polled in a worker thread; gather runs them concurrently
    return list(await asyncio.gather(*(asyncio.to_thread(poller.poll, d) for d in devices)))


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
    poller = Poller(cfg)
    polled = [d for d in cfg.devices if d.poll]
    print(f"wispmap-collector: {len(cfg.devices)} devices ({len(polled)} polled), "
          f"ping every {cfg.ping_s}s, poll every {cfg.fast_s}s", file=sys.stderr)

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
            *background,
        )
    except asyncio.CancelledError:
        print("wispmap-collector: stopping", file=sys.stderr)
    finally:
        poller.close()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(sig)
