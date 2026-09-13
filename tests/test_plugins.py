"""docker CLI plugin wiring.

`docker compose` and `docker buildx` are not subcommands of the docker CLI —
they are separate executables it discovers in a plugins directory. pixi installs
them onto PATH as standalone commands, so `docker-compose` works while
`docker compose` fails with "unknown command". Since bosun is what installs
them, that half-wired state was bosun's to fix.

bosun adds the directory they already occupy to the client's plugin search path
rather than copying them into the client's own plugins directory. A copy goes
stale when the original is updated, and it assumes the copy will be executable
from its new location, which is not a safe assumption on every Windows install.
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
    bin_dir = tmp_path / "installer" / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    binaries = {}
    for name in ("docker-buildx", "docker-compose"):
        path = bin_dir / f"{name}.exe"
        path.write_bytes(b"binary contents")
        binaries[name] = str(path)
    monkeypatch.setattr("bosun.client.shutil.which", lambda n: binaries.get(n))
    return bin_dir


def responding(runner=None):
    return (runner or FakeRunner()).out("docker-cli-plugin-metadata", METADATA)


def config_of(home):
    return json.loads((home / ".docker" / "config.json").read_text(encoding="utf-8"))


def test_the_installer_directory_is_added_to_the_search_path(tmp_path, fake_binaries):
    home = tmp_path / "home"
    wired = client.wire_plugins(responding(), resolve(), DOCKER, lambda _: None, home)

    assert sorted(wired) == ["docker-buildx", "docker-compose"]
    assert config_of(home)["cliPluginsExtraDirs"] == [str(fake_binaries)]


def test_nothing_is_copied(tmp_path, fake_binaries):
    """A copy goes stale, and may not be executable from its new location."""
    home = tmp_path / "home"
    client.wire_plugins(responding(), resolve(), DOCKER, lambda _: None, home)
    assert not (home / ".docker" / "cli-plugins").exists()


def test_existing_config_keys_survive(tmp_path, fake_binaries):
    """config.json also holds registry credentials; clobbering it logs you out."""
    home = tmp_path / "home"
    path = home / ".docker" / "config.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"auths": {"registry.example": {"auth": "c2VjcmV0"}}, "currentContext": "bosun"})
    )

    client.wire_plugins(responding(), resolve(), DOCKER, lambda _: None, home)

    config = config_of(home)
    assert config["auths"] == {"registry.example": {"auth": "c2VjcmV0"}}
    assert config["currentContext"] == "bosun"
    assert config["cliPluginsExtraDirs"] == [str(fake_binaries)]


def test_an_existing_extra_dir_is_preserved(tmp_path, fake_binaries):
    home = tmp_path / "home"
    path = home / ".docker" / "config.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"cliPluginsExtraDirs": ["/somewhere/else"]}))

    client.wire_plugins(responding(), resolve(), DOCKER, lambda _: None, home)

    assert config_of(home)["cliPluginsExtraDirs"] == ["/somewhere/else", str(fake_binaries)]


def test_rewiring_is_a_noop(tmp_path, fake_binaries):
    home = tmp_path / "home"
    client.wire_plugins(responding(), resolve(), DOCKER, lambda _: None, home)
    before = (home / ".docker" / "config.json").read_text(encoding="utf-8")

    logs: list[str] = []
    client.wire_plugins(responding(), resolve(), DOCKER, logs.append, home)

    assert (home / ".docker" / "config.json").read_text(encoding="utf-8") == before
    assert not any("wired" in line for line in logs)


def test_a_binary_that_fails_the_handshake_is_not_wired(tmp_path, fake_binaries):
    runner = FakeRunner().on("docker-cli-plugin-metadata", Result(1, "", "unknown command"))
    logs: list[str] = []
    home = tmp_path / "home"

    assert client.wire_plugins(runner, resolve(), DOCKER, logs.append, home) == []
    assert not (home / ".docker").exists()
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


def test_a_corrupt_config_is_replaced_rather_than_crashing(tmp_path, fake_binaries):
    home = tmp_path / "home"
    path = home / ".docker" / "config.json"
    path.parent.mkdir(parents=True)
    path.write_text("{ this is not json")

    client.wire_plugins(responding(), resolve(), DOCKER, lambda _: None, home)
    assert config_of(home)["cliPluginsExtraDirs"] == [str(fake_binaries)]


def test_the_handshake_probe_is_read_only():
    """It runs under --dry-run, so its result must be real rather than synthetic."""
    runner = responding()
    client.is_plugin(runner, "docker-buildx")
    assert runner.marked_read_only("docker-cli-plugin-metadata")


def test_dry_run_writes_nothing(tmp_path, fake_binaries):
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
    """It was missing from the install list, so `docker buildx` had nothing to find."""
    assert "docker-buildx" in DOCKER.host_cli_packages
