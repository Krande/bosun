"""The engine registry — the seam that keeps bosun from being Docker-only."""

from __future__ import annotations

import pytest

from bosun import engines


def test_docker_is_implemented():
    spec = engines.get("docker")
    assert spec.name == "docker"
    assert spec.implemented


def test_lookup_is_case_and_whitespace_insensitive():
    assert engines.get("  Docker ").name == "docker"


def test_unknown_engine_lists_what_is_known():
    with pytest.raises(engines.UnsupportedEngine, match="bosun knows"):
        engines.get("containerd")


def test_registered_but_unimplemented_engine_fails_loudly():
    """Better to refuse than to half-provision a machine."""
    with pytest.raises(engines.UnsupportedEngine, match="not implemented"):
        engines.get("podman")


def test_daemon_dir_is_derived_from_the_config_path():
    assert engines.DOCKER.daemon_dir() == "/etc/docker"


def test_exec_start_does_not_carry_the_conflicting_flag():
    """-H fd:// plus a hosts array in daemon.json stops the daemon booting."""
    assert "fd://" not in engines.DOCKER.exec_start


def test_every_spec_declares_the_fields_the_flows_read():
    for spec in engines.ENGINES.values():
        assert spec.service and spec.socket and spec.group
        assert spec.host_cli and spec.host_cli_packages
        assert spec.daemon_json.startswith("/")
