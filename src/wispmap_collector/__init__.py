"""WISPMap collector."""
import asyncio
import sys

from .config import ConfigError, load_config
from .runner import run


def main() -> None:
    try:
        cfg = load_config()
    except (ConfigError, FileNotFoundError) as e:
        print(f"config error: {e}", file=sys.stderr)
        sys.exit(2)
    try:
        asyncio.run(run(cfg))
    except KeyboardInterrupt:
        print("wispmap-collector: stopped", file=sys.stderr)
