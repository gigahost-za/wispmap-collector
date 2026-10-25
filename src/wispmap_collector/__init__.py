"""WISPMap collector.

Two modes:
  server mode  WISPMAP_URL + WISPMAP_TOKEN set: devices from the server, results sent to the server
  local mode   otherwise (or --local): devices from devices.toml, results printed as JSON lines
"""
import asyncio
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from .config import Config, ConfigError, load_config, load_credentials
from .runner import run


def main() -> None:
    load_dotenv(".env")
    local = "--local" in sys.argv[1:] or not (os.environ.get("WISPMAP_URL") and os.environ.get("WISPMAP_TOKEN"))
    try:
        coro = run_local() if local else run_server()
        asyncio.run(coro)
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        sys.exit(2)
    except KeyboardInterrupt:
        print("wispmap-collector: stopped", file=sys.stderr)


async def run_local() -> None:
    try:
        cfg = load_config()
    except FileNotFoundError as e:
        raise ConfigError(str(e)) from None
    await run(cfg)


async def run_server() -> None:
    from .buffer import Buffer
    from .server import ServerClient
    from .sync import ConfigSync
    from .uplink import Uplink

    username, password = load_credentials()
    data_dir = Path(os.environ.get("WISPMAP_BUFFER_DIR", "data"))
    cfg = Config(ping_s=30, fast_s=60, username=username, password=password)
    client = ServerClient(os.environ["WISPMAP_URL"], os.environ["WISPMAP_TOKEN"])
    buffer = Buffer(data_dir / "buffer.sqlite3")
    sync = ConfigSync(client, cfg, data_dir / "config.json")
    uplink = Uplink(client, buffer)
    uplink.on_config_version = sync.notify
    try:
        await sync.startup()
        if buffer.depth():
            print(f"wispmap-collector: {buffer.depth()} batch(es) waiting in the buffer from last run",
                  file=sys.stderr)
        await run(cfg, sink=uplink.add, background=[uplink.run(lambda: cfg.push_s), sync.run()])
    finally:
        uplink.flush()                    # samples collected since the last push go to disk, sent next start
        buffer.close()
        await client.aclose()
