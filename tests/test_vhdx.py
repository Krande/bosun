"""Locating and compacting the distro's virtual disk."""

from __future__ import annotations

import pytest

from bosun import vhdx
from bosun.config import resolve
from bosun.exec import BosunError


def make_disk(tmp_path, *parts):
    path = tmp_path.joinpath(*parts)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * 1024)
    return path


def test_configured_path_is_used_verbatim(tmp_path):
    disk = make_disk(tmp_path, "custom", "ext4.vhdx")
    cfg = resolve(overrides={"vhdx": {"path": str(disk)}})
    assert vhdx.resolve_path(cfg) == disk


def test_configured_path_that_does_not_exist_is_an_error(tmp_path):
    cfg = resolve(overrides={"vhdx": {"path": str(tmp_path / "missing.vhdx")}})
    with pytest.raises(BosunError, match="does not exist"):
        vhdx.resolve_path(cfg)


def test_discovers_a_store_installed_disk(tmp_path):
    make_disk(
        tmp_path, "Packages", "CanonicalGroupLimited.Ubuntu24.04LTS_x", "LocalState", "ext4.vhdx"
    )
    found = vhdx.discover(resolve(), {"LOCALAPPDATA": str(tmp_path)})
    assert len(found) == 1
    assert found[0].name == "ext4.vhdx"


def test_discovers_a_wsl_install_disk(tmp_path):
    make_disk(tmp_path, "wsl", "Ubuntu-24.04", "ext4.vhdx")
    assert len(vhdx.discover(resolve(), {"LOCALAPPDATA": str(tmp_path)})) == 1


def test_no_disk_found_explains_the_fix(tmp_path):
    cfg = resolve()
    with pytest.raises(BosunError, match=r"\[vhdx\].path"):
        vhdx.resolve_path(cfg, {"LOCALAPPDATA": str(tmp_path)})


def test_ambiguous_disks_are_listed_rather_than_guessed(tmp_path):
    """Compacting the wrong disk is worse than asking which one."""
    make_disk(tmp_path, "Packages", "Ubuntu22", "LocalState", "ext4.vhdx")
    make_disk(tmp_path, "Packages", "Ubuntu24", "LocalState", "ext4.vhdx")
    with pytest.raises(BosunError, match="2 candidate"):
        vhdx.resolve_path(resolve(), {"LOCALAPPDATA": str(tmp_path)})


def test_discovery_ignores_unrelated_distros(tmp_path):
    make_disk(tmp_path, "Packages", "TheDebianProject.Debian_y", "LocalState", "ext4.vhdx")
    assert vhdx.discover(resolve(), {"LOCALAPPDATA": str(tmp_path)}) == []


def test_missing_env_var_is_not_a_crash():
    assert vhdx.discover(resolve(), {}) == []


def test_size_of_a_missing_file_is_zero(tmp_path):
    assert vhdx.size_gb(tmp_path / "nope.vhdx") == 0.0
