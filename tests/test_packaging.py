"""Packaging guards: secrets never reach the image, the image runs as non-root, versions agree."""
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def lines(name):
    return [l.strip() for l in (ROOT / name).read_text().splitlines() if l.strip() and not l.startswith("#")]


def test_dockerignore_keeps_secrets_and_state_out():
    ignored = lines(".dockerignore")
    for must in (".env", "data/", "devices.toml", ".git/"):
        assert must in ignored, must
    assert "!.env.example" in ignored


def test_image_is_non_root_with_exec_entrypoint():
    df = (ROOT / "Dockerfile").read_text()
    runtime = df.rsplit("\nFROM ", 1)[1]
    assert "\nUSER wispmap" in runtime
    assert 'ENTRYPOINT ["wispmap-collector"]' in runtime     # exec form: PID 1 receives SIGTERM
    assert "WISPMAP_BUFFER_DIR=/data" in runtime
    assert "--frozen" in df                                  # builds exactly what uv.lock says


def test_uv_versions_agree():
    df = (ROOT / "Dockerfile").read_text()
    wf = (ROOT / ".github/workflows/image.yml").read_text()
    uv = df.split("ghcr.io/astral-sh/uv:", 1)[1].split()[0]
    assert f'version: "{uv}"' in wf


def test_env_example_lists_everything_the_collector_reads():
    keys = {l.split("=", 1)[0].lstrip("# ").strip() for l in (ROOT / ".env.example").read_text().splitlines()
            if "=" in l}
    assert {"WISPMAP_URL", "WISPMAP_TOKEN", "WISPMON_USER", "WISPMON_PASSWORD"} <= keys


def test_version_is_release_shaped():
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    assert all(p.isdigit() for p in version.split(".")) and len(version.split(".")) == 3
