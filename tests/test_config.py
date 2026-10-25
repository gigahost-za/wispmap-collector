import pytest

from wispmap_collector.config import ConfigError, Device, load_config

TOML = """
[intervals]
ping_s = 15
fast_s = 45

[[devices]]
id = "core"
host = "10.255.255.1"

[[devices]]
id = "gw"
host = "192.168.1.254"
kind = "ping_only"
"""


@pytest.fixture
def creds(monkeypatch):
    monkeypatch.setenv("WISPMON_USER", "wispmon")
    monkeypatch.setenv("WISPMON_PASSWORD", "s3cret")


def write(tmp_path, text):
    p = tmp_path / "devices.toml"
    p.write_text(text)
    return p


def load(tmp_path, text):
    # env_file points at a file that doesn't exist, so the real .env is never read in tests
    return load_config(write(tmp_path, text), env_file=tmp_path / "missing.env")


def test_loads_intervals_and_devices(tmp_path, creds):
    cfg = load(tmp_path, TOML)
    assert (cfg.ping_s, cfg.fast_s) == (15, 45)
    assert [d.id for d in cfg.devices] == ["core", "gw"]
    assert cfg.username == "wispmon" and cfg.password == "s3cret"


def test_device_defaults(tmp_path, creds):
    core = load(tmp_path, TOML).devices[0]
    assert (core.kind, core.api_port, core.ping, core.poll) == ("routeros", 8728, True, True)


def test_ping_only_is_never_polled(tmp_path, creds):
    gw = load(tmp_path, TOML).devices[1]
    assert gw.kind == "ping_only" and gw.poll is False


def test_interval_defaults(tmp_path, creds):
    cfg = load(tmp_path, '[[devices]]\nid = "a"\nhost = "1.2.3.4"\n')
    assert (cfg.ping_s, cfg.fast_s) == (30, 60)


def test_password_not_in_repr(tmp_path, creds):
    assert "s3cret" not in repr(load(tmp_path, TOML))


def test_missing_credentials(tmp_path, monkeypatch):
    monkeypatch.delenv("WISPMON_USER", raising=False)
    monkeypatch.delenv("WISPMON_PASSWORD", raising=False)
    with pytest.raises(ConfigError, match="WISPMON_USER"):
        load(tmp_path, TOML)


def test_unknown_kind(tmp_path, creds):
    with pytest.raises(ConfigError, match="unknown kind"):
        load(tmp_path, '[[devices]]\nid = "x"\nhost = "1.2.3.4"\nkind = "ubiquiti"\n')


def test_typo_in_device_key(tmp_path, creds):
    with pytest.raises(ConfigError, match="bad device entry"):
        load(tmp_path, '[[devices]]\nid = "x"\nhots = "1.2.3.4"\n')


def test_duplicate_ids(tmp_path, creds):
    dup = '[[devices]]\nid = "x"\nhost = "1.1.1.1"\n[[devices]]\nid = "x"\nhost = "2.2.2.2"\n'
    with pytest.raises(ConfigError, match="duplicate"):
        load(tmp_path, dup)


def test_repo_devices_toml_is_valid(creds):
    """The committed devices.toml must always load."""
    cfg = load_config("devices.toml", env_file="missing.env")
    assert {d.id for d in cfg.devices} >= {"chr-core", "chr-twr-a", "chr-twr-b"}


def test_device_direct():
    assert Device(id="a", host="h").kind == "routeros"
