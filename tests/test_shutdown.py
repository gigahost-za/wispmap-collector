import asyncio
import os
import signal

from wispmap_collector import runner
from wispmap_collector.config import Config, Device


def test_sigterm_stops_run_cleanly(monkeypatch, capsys):
    """`docker stop` sends SIGTERM: run() must return (not die) and close sessions."""
    closed = []

    async def fake_ping_cycle(cfg):
        return []

    monkeypatch.setattr(runner, "ping_cycle", fake_ping_cycle)
    monkeypatch.setattr(runner.Poller, "close", lambda self: closed.append(True))
    cfg = Config(ping_s=1, fast_s=1, username="u", password="p",
                 devices=[Device("gw", "192.0.2.1", kind="ping_only")])

    async def main():
        asyncio.get_running_loop().call_later(0.3, os.kill, os.getpid(), signal.SIGTERM)
        await asyncio.wait_for(runner.run(cfg), timeout=5)

    asyncio.run(main())
    assert closed == [True]
    assert "stopping" in capsys.readouterr().err
