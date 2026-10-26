"""A fake RouterOS API server for load-testing the collector: many routers in one process, no CHR needed.

Every router is a loopback address (Linux routes all of 127.0.0.0/8 to lo, so no setup is needed), all on one
port. The second octet decides how the router behaves:

    127.21.x.y  normal: a small tower router (7 interfaces, 2 OSPF neighbours, ~20 OSPF routes)
    127.22.x.y  slow: every reply takes 1-3 s (busy CPU, congested backhaul)
    127.23.x.y  hung: accepts the connection, never answers (the collector waits for its timeout)
    127.24.x.y  drops the connection right after accepting it
    127.25.x.y  core router: 2,000 active OSPF routes
    (refused:   point the device at a port nobody listens on)

--latency-ms N adds N ms (x0.5-1.5) to every reply of the healthy routers: real routers sit behind radio hops,
and without latency every collector thread is busy at once, which overstates the collector's CPU.

Counters grow with time, so the collector computes real rates. Answers only what the collector asks:
/login, /system/resource, /interface, /ip/neighbor, /routing/ospf/interface|neighbor, /ip/route.

    python tools/fakeros.py --port 18728          # Ctrl+C to stop
"""
import argparse
import asyncio
import random
import signal
import time

START = time.monotonic()


# ---------- wire format (RouterOS API: length-prefixed words, sentences end with an empty word) ----------

def encode_length(n: int) -> bytes:
    if n < 0x80:
        return bytes([n])
    if n < 0x4000:
        return (n | 0x8000).to_bytes(2, "big")
    if n < 0x200000:
        return (n | 0xC00000).to_bytes(3, "big")
    if n < 0x10000000:
        return (n | 0xE0000000).to_bytes(4, "big")
    return b"\xf0" + n.to_bytes(4, "big")


def sentence(*words: str) -> bytes:
    out = b""
    for w in words:
        b = w.encode()
        out += encode_length(len(b)) + b
    return out + b"\x00"


async def read_length(reader) -> int:
    first = (await reader.readexactly(1))[0]
    if first < 0x80:
        return first
    if first < 0xC0:
        return ((first & 0x3F) << 8) | (await reader.readexactly(1))[0]
    if first < 0xE0:
        return ((first & 0x1F) << 16) | int.from_bytes(await reader.readexactly(2), "big")
    if first < 0xF0:
        return ((first & 0x0F) << 24) | int.from_bytes(await reader.readexactly(3), "big")
    return int.from_bytes(await reader.readexactly(4), "big")


async def read_sentence(reader) -> list[str]:
    words = []
    while True:
        n = await read_length(reader)
        if n == 0:
            return words
        words.append((await reader.readexactly(n)).decode())


def row(d: dict) -> bytes:
    def val(v):
        return "true" if v is True else "false" if v is False else str(v)
    return sentence("!re", *(f"={k}={val(v)}" for k, v in d.items()))


# ---------- one simulated router ----------

class Router:
    def __init__(self, addr: str):
        self.addr = addr
        self.kind = int(addr.split(".")[1])
        r = random.Random(addr)
        self.rng = r
        self.delay = r.uniform(1.0, 3.0) if self.kind == 22 else 0.0
        self.boot = time.time() - r.randint(3600, 90 * 86400)
        self.ifaces = [("ether1", "ether"), ("ether2", "ether"), ("ether3", "ether"), ("ether4", "ether"),
                       ("wlan1", "wlan"), ("bridge1", "bridge"), ("lo", "loopback")]
        self.bps = {n: r.uniform(0.1e6, 80e6) for n, _ in self.ifaces}
        self.mac = [r.randrange(256) for _ in range(4)]
        n_routes = 2000 if self.kind == 25 else 20
        self.routes = [{"dst-address": f"10.{i // 250}.{i % 250}.0/24", "gateway": "10.0.1.2",
                        "immediate-gw": f"10.0.1.2%ether{1 + i % 2}", "active": True, "ospf": True}
                       for i in range(n_routes)]

    def uptime(self) -> str:
        s = int(time.time() - self.boot)
        return f"{s // 86400}d{s % 86400 // 3600}h{s % 3600 // 60}m{s % 60}s"

    def answer(self, cmd: str) -> list[dict] | None:
        t = time.monotonic() - START + 1e6
        if cmd == "/system/resource/print":
            return [{"uptime": self.uptime(), "cpu-load": self.rng.randint(1, 40), "free-memory": 700_000_000,
                     "total-memory": 1_073_741_824, "version": "7.16.2 (stable)", "board-name": "RB4011iGS+"}]
        if cmd == "/interface/print":
            out = []
            for k, (name, typ) in enumerate(self.ifaces):
                b = int(self.bps[name] / 8 * t)
                out.append({".id": f"*{k + 1:X}", "name": name, "type": typ,
                            "mac-address": "00:00:00:00:00:00" if typ == "loopback" else
                            "4C:5E:0C:%02X:%02X:%02X" % (self.mac[0], self.mac[1], k),
                            "running": name != "ether4", "disabled": False,
                            "rx-byte": b, "tx-byte": b // 4, "rx-packet": b // 1000, "tx-packet": b // 4000,
                            "rx-error": 0, "tx-error": 0, "rx-drop": 0, "tx-drop": 0, "link-downs": 1})
            return out
        if cmd == "/ip/neighbor/print":
            return [{"interface": "ether1", "mac-address": "4C:5E:0C:AA:%02X:01" % self.mac[2],
                     "address": "10.0.1.1", "identity": "upstream", "platform": "MikroTik", "version": "7.16.2",
                     "board": "RB4011iGS+", "interface-name": "ether2", "discovered-by": "mndp"}]
        if cmd == "/routing/ospf/interface/print":
            return [{"interface": n, "cost": 10 if n == "ether1" else 100, "state": "ptp"} for n in ("ether1", "ether2")]
        if cmd == "/routing/ospf/neighbor/print":
            return [{"interface": n, "address": "10.0.1.2", "router-id": "10.255.255.1", "state": "Full",
                     "state-changes": 3, "adjacency": "2d3h"} for n in ("ether1", "ether2")]
        if cmd == "/ip/route/print":
            return self.routes
        return None


ROUTERS: dict[str, Router] = {}
LATENCY_S = [0.0]
STATS = {"connections": 0, "commands": 0}


async def handle(reader, writer):
    addr = writer.get_extra_info("sockname")[0]
    peer = writer.get_extra_info("peername")[0]
    if not peer.startswith("127.") or not addr.startswith("127."):
        writer.close()                                       # loopback only, even when bound to 0.0.0.0
        return
    router = ROUTERS.get(addr) or ROUTERS.setdefault(addr, Router(addr))
    STATS["connections"] += 1
    try:
        if router.kind == 24:
            return                                           # drop right away (finally closes)
        while True:
            words = await read_sentence(reader)
            if not words:
                continue
            STATS["commands"] += 1
            if router.kind == 23:
                continue                                     # hung: never answer anything
            delay = router.delay or LATENCY_S[0] * router.rng.uniform(0.5, 1.5)
            if delay:
                await asyncio.sleep(delay)
            cmd = words[0]
            if cmd == "/login":
                writer.write(sentence("!done"))
            else:
                rows = router.answer(cmd)
                if rows is None:
                    writer.write(sentence("!trap", "=message=no such command or directory (fake)") + sentence("!done"))
                else:
                    writer.write(b"".join(row(r) for r in rows) + sentence("!done"))
            await writer.drain()
    except (asyncio.IncompleteReadError, ConnectionError):
        pass
    finally:
        writer.close()


async def serve(port: int, ready=None, latency_ms: float = 0.0):
    LATENCY_S[0] = latency_ms / 1000
    try:
        import resource                                  # one socket per router: raise the open-files limit
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        resource.setrlimit(resource.RLIMIT_NOFILE, (min(65536, hard), hard))
    except (ImportError, ValueError, OSError):
        pass
    server = await asyncio.start_server(handle, "0.0.0.0", port, backlog=4096)
    if ready:
        ready()
    async with server:
        await server.serve_forever()


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--port", type=int, default=18728)
    p.add_argument("--latency-ms", type=float, default=0.0)
    args = p.parse_args()

    async def run():
        loop = asyncio.get_running_loop()
        task = asyncio.current_task()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, task.cancel)
        try:
            await serve(args.port, lambda: print(f"fakeros: listening on 127.x.y.z:{args.port}", flush=True),
                        args.latency_ms)
        except asyncio.CancelledError:
            print(f"fakeros: {len(ROUTERS)} routers, {STATS['connections']} connections, "
                  f"{STATS['commands']} commands", flush=True)

    asyncio.run(run())


if __name__ == "__main__":
    main()
