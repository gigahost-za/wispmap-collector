import json
from pathlib import Path

from librouteros.exceptions import TrapError

FIXTURES = Path(__file__).parent / "fixtures"


class FakePath:
    """Iterating = print (<name>.json); calling with a command = that command (<name>_<command>.json)."""

    def __init__(self, api, name):
        self.api, self.name = api, name

    def __iter__(self):
        f = self.api.dir / f"{self.name}.json"
        if not f.exists():                      # like a real router without that menu (CHR has no PoE)
            raise TrapError("no such command prefix")
        return iter(json.loads(f.read_text()))

    def __call__(self, cmd, **kwargs):
        self.api.calls.append((self.name, cmd, kwargs))
        return iter(json.loads((self.api.dir / f"{self.name}_{cmd}.json").read_text()))


class FakeApi:
    """Mimics librouteros: api.path('system', 'resource') -> iterable of dicts from a fixture file."""

    def __init__(self, fixture_dir: str):
        self.dir = FIXTURES / fixture_dir
        self.calls = []

    def path(self, *parts):
        return FakePath(self, "_".join(parts))   # ('system', 'resource') -> system_resource

    def close(self):
        pass
