"""The three-layer config stack, and the promise that no identity is baked in."""

from __future__ import annotations

import pytest

from bosun.config import DEFAULTS, Config, ConfigError, apply_env, deep_merge, resolve

TOML = """\
[distro]
name = "Ubuntu-22.04"
user = "someone"

[engine]
expose = "tls"
tls_port = 2999

[tls]
org = "Example Org"
country = "SE"
"""


@pytest.fixture
def config_file(tmp_path):
    path = tmp_path / "bosun.toml"
    path.write_text(TOML, encoding="utf-8")
    return str(path)


def test_defaults_alone_are_a_working_config():
    cfg = resolve()
    assert cfg.distro_name == "Ubuntu-24.04"
    assert cfg.engine_name == "docker"
    assert cfg.endpoint == "tcp://127.0.0.1:2375"


def test_toml_overrides_defaults(config_file):
    cfg = resolve(config_file)
    assert cfg.distro_name == "Ubuntu-22.04"
    assert cfg.user == "someone"
    assert cfg.tls_enabled
    assert cfg.port == 2999


def test_toml_keys_left_unset_keep_their_defaults(config_file):
    cfg = resolve(config_file)
    assert cfg.context == DEFAULTS["client"]["context"]
    assert cfg.apt["retries"] == 3


def test_env_beats_toml(config_file):
    cfg = resolve(config_file, env={"BOSUN_DISTRO": "Debian", "BOSUN_USER": "other"})
    assert cfg.distro_name == "Debian"
    assert cfg.user == "other"


def test_flags_beat_env(config_file):
    cfg = resolve(
        config_file,
        overrides={"distro": {"name": "FromFlag"}},
        env={"BOSUN_DISTRO": "FromEnv"},
    )
    assert cfg.distro_name == "FromFlag"


def test_empty_env_var_is_ignored():
    cfg = resolve(env={"BOSUN_DISTRO": ""})
    assert cfg.distro_name == "Ubuntu-24.04"


def test_env_ints_are_cast():
    cfg = resolve(env={"BOSUN_PORT": "3000"})
    assert cfg.port == 3000


def test_non_numeric_port_env_is_rejected():
    with pytest.raises(ConfigError):
        resolve(env={"BOSUN_PORT": "not-a-port"})


def test_missing_explicit_config_file_is_an_error(tmp_path):
    with pytest.raises(ConfigError):
        resolve(str(tmp_path / "nope.toml"))


def test_missing_default_config_file_is_fine(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert resolve().distro_name == "Ubuntu-24.04"


@pytest.mark.parametrize("expose", ["ftp", "TCP ", ""])
def test_invalid_expose_mode_rejected(expose):
    with pytest.raises(ConfigError, match="expose"):
        resolve(overrides={"engine": {"expose": expose}})


@pytest.mark.parametrize("port", [0, 70000, "2375"])
def test_invalid_port_rejected(port):
    with pytest.raises(ConfigError, match="port"):
        resolve(overrides={"engine": {"port": port}})


def test_blank_distro_name_rejected():
    with pytest.raises(ConfigError, match="distro"):
        resolve(overrides={"distro": {"name": "   "}})


# ── the "nothing personal ships in the defaults" guarantees ────────────────


def test_default_tls_subject_carries_no_identity():
    """The shipped default must not name a person, employer or country."""
    cfg = resolve()
    assert cfg.subject("localhost") == "/O=bosun/CN=localhost"
    assert "/C=" not in cfg.subject("localhost")


def test_blank_org_and_country_are_omitted_entirely():
    cfg = resolve(overrides={"tls": {"org": "", "country": ""}})
    assert cfg.subject("x") == "/CN=x"


def test_subject_uses_configured_identity(config_file):
    assert resolve(config_file).subject("host") == "/C=SE/O=Example Org/CN=host"


def test_no_default_username():
    """A username baked into the defaults is exactly the leak to avoid."""
    assert resolve().user == ""
    assert DEFAULTS["distro"]["user"] == ""


def test_no_default_vhdx_path():
    assert resolve().vhdx["path"] == ""


# ── derived values ─────────────────────────────────────────────────────────


def test_endpoint_is_empty_in_unix_mode():
    assert resolve(overrides={"engine": {"expose": "unix"}}).endpoint == ""


def test_tls_mode_uses_the_tls_port():
    cfg = resolve(overrides={"engine": {"expose": "tls"}})
    assert cfg.port == cfg.engine["tls_port"]


def test_apt_flags_reflect_settings():
    flags = " ".join(resolve().apt_flags())
    assert "Acquire::ForceIPv4=true" in flags
    assert "Acquire::Retries=3" in flags

    off = " ".join(resolve(overrides={"apt": {"force_ipv4": False}}).apt_flags())
    assert "ForceIPv4" not in off


def test_deep_merge_does_not_mutate_its_inputs():
    base = {"a": {"b": 1}}
    deep_merge(base, {"a": {"c": 2}})
    assert base == {"a": {"b": 1}}


def test_apply_env_leaves_unrelated_vars_alone():
    data = apply_env(DEFAULTS, {"PATH": "/usr/bin", "BOSUN_CONTEXT": "mine"})
    assert data["client"]["context"] == "mine"


def test_config_is_frozen():
    with pytest.raises((AttributeError, TypeError)):
        Config().distro = {}
