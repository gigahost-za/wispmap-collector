"""Small shared helpers: version string and stderr logging."""
import sys
from importlib.metadata import PackageNotFoundError, version

try:
    VERSION = version("wispmap-collector")
except PackageNotFoundError:            # running from a source tree that isn't installed
    VERSION = "0.0.0"


def log(msg: str) -> None:
    """Human-readable messages go to stderr (stdout is reserved for JSON results in local mode)."""
    print(f"wispmap-collector: {msg}", file=sys.stderr, flush=True)
