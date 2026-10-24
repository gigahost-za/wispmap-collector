import json
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"


class FakeApi:
    """Mimics librouteros: api.path('system', 'resource') -> iterable of dicts from a fixture file."""

    def __init__(self, fixture_dir: str):
        self.dir = FIXTURES / fixture_dir

    def path(self, *parts):
        name = "_".join(parts)                 # ('system', 'resource') -> 'system_resource'
        return iter(json.loads((self.dir / f"{name}.json").read_text()))

    def close(self):
        pass
