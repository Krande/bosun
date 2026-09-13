"""docker CLI plugin wiring.

`docker compose` and `docker buildx` are not subcommands of the docker CLI —
they are separate executables it discovers in a cli-plugins directory. pixi
installs them onto PATH as standalone commands, so `docker-compose` works while
`docker compose` fails with "unknown command". Since bosun is what installs
them, that half-wired state was bosun's to fix.
"""

from __future__ import annotations

import json

import pytest

from bosun import client
from bosun.config import resolve
from bosun.engines import DOCKER
from bosun.exec import DryRunRunner, Result
from fakes import FakeRunner

METADATA = json.dumps({"SchemaVersion": "0.1.0", "Vendor": "Docker Inc.", "Version": "v0.37.1"})


@pytest.fixture
def fake_binaries(tmp_path, monkeypatch):
    """Put plugin-looking executables on a fake PATH."""
    binaries = {}
    for name in ("docker-buildx", "docker-compose"):
        path = tmp_path / "bin" / f"{name}.exe"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"binary contents")
        binaries[name] = str(path)
    monkeypatch.setattr("bosun.client.shutil.which", lambda n: binaries.get(n))
    return binaries


def responding(runner=None):
    return (runner or FakeRunner()).out("docker-cli-plugin-metadata", METADATA)


def test_a_plugin_is_copied_into_the_cli_plugins_directory(tmp_path, fake_binaries):
    home = tmp_path / "home"
    wired = client.wire_plugins(responding(), resolve(), DOCKER, lambda _: None, home)

    assert sorted(wired) == ["docker-buildx", "docker-compose"]
    assert (home / ".docker" / "cli-plugins" / "docker-buildx.exe").is_file()
    assert (home / ".docker" / "cli-plugins" / "docker-compose.exe").is_file()


def test_the_copy_is_the_real_binary(tmp_path, fake_binaries):
    home = tmp_path / "home"
    client.wire_plugins(responding(), resolve(), DOCKER, lambda _: None, home)
    assert (
        home / ".docker" / "cli-plugins" / "docker-buildx.exe"
    ).read_bytes() == b"binary contents"


def test_rewiring_an_already_wired_plugin_is_a_noop(tmp_path, fake_binaries):
    home = tmp_path / "home"
    client.wire_plugins(responding(), resolve(), DOCKER, lambda _: None, home)
    assert client.wire_plugins(responding(), resolve(), DOCKER, lambda _: None, home) == []


def test_a_binary_that_fails_the_handshake_is_not_wired(tmp_path, fake_binaries):
    """Not every docker-* executable on PATH is a plugin; installing a
    non-conforming one makes every later docker command print a warning."""
    runner = FakeRunner().on("docker-cli-plugin-metadata", Result(1, "", "unknown command"))
    logs: list[str] = []
    home = tmp_path / "home"

    assert client.wire_plugins(runner, resolve(), DOCKER, logs.append, home) == []
    assert not (home / ".docker" / "cli-plugins").exists()
    assert any("handshake" in line for line in logs)


def test_non_json_metadata_is_rejected(tmp_path, fake_binaries):
    runner = FakeRunner().out("docker-cli-plugin-metadata", "not json at all")
    assert client.wire_plugins(runner, resolve(), DOCKER, lambda _: None, tmp_path) == []


def test_json_without_a_schema_version_is_rejected(tmp_path, fake_binaries):
    runner = FakeRunner().out("docker-cli-plugin-metadata", json.dumps({"Vendor": "someone"}))
    assert client.wire_plugins(runner, resolve(), DOCKER, lambda _: None, tmp_path) == []


def test_a_binary_absent_from_path_is_skipped(tmp_path, monkeypatch):
    monkeypatch.setattr("bosun.client.shutil.which", lambda _n: None)
    assert client.wire_plugins(responding(), resolve(), DOCKER, lambda _: None, tmp_path) == []


def test_wiring_can_be_switched_off(tmp_path, fake_binaries):
    cfg = resolve(overrides={"client": {"wire_plugins": False}})
    assert client.wire_plugins(responding(), cfg, DOCKER, lambda _: None, tmp_path) == []
    assert not (tmp_path / ".docker").exists()


def test_the_handshake_probe_is_read_only():
    """It runs under --dry-run, so its result must be real rather than synthetic."""
    runner = responding()
    client.is_plugin(runner, "docker-buildx")
    assert runner.marked_read_only("docker-cli-plugin-metadata")


def test_dry_run_copies_nothing(tmp_path, fake_binaries):
    home = tmp_path / "home"
    runner = DryRunRunner(responding())
    logs: list[str] = []

    wired = client.wire_plugins(runner, resolve(), DOCKER, logs.append, home)

    assert sorted(wired) == ["docker-buildx", "docker-compose"]
    assert not (home / ".docker").exists(), "dry run must not touch the filesystem"
    assert any("[dry-run]" in line for line in logs)


def test_the_engine_declares_its_plugins():
    assert "docker-buildx" in DOCKER.cli_plugins
    assert "docker-compose" in DOCKER.cli_plugins


def test_buildx_is_installed_alongside_the_cli():
    """It was missing from the install list, so `docker buildx` had nothing to wire."""
    assert "docker-buildx" in DOCKER.host_cli_packages
