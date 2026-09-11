"""Argument parsing, flag precedence and the config command."""

from __future__ import annotations

import json

import pytest

from bosun import cli
from bosun.config import resolve


def parse(argv):
    return cli.build_parser().parse_args(argv)


def test_subcommand_is_required():
    with pytest.raises(SystemExit):
        parse([])


@pytest.mark.parametrize("command", ["up", "status", "down", "shrink", "config"])
def test_every_command_parses(command):
    assert parse([command]).command == command


def test_common_flags_are_available_on_each_subcommand():
    args = parse(["status", "--distro", "Debian", "--verbose", "--dry-run"])
    assert args.distro == "Debian"
    assert args.verbose
    assert args.dry_run


def test_unknown_engine_is_rejected_by_the_parser():
    with pytest.raises(SystemExit):
        parse(["up", "--engine", "lxc"])


def test_expose_choices_are_constrained():
    with pytest.raises(SystemExit):
        parse(["up", "--expose", "carrier-pigeon"])


def test_overrides_map_flags_onto_config_sections():
    args = parse(["up", "--distro", "Debian", "--user", "dev", "--context", "mine"])
    overrides = cli._overrides(args)
    assert overrides["distro"] == {"name": "Debian", "user": "dev"}
    assert overrides["client"] == {"context": "mine"}


def test_port_flag_targets_the_tls_port_in_tls_mode():
    """--port 9999 --expose tls must set tls_port, not the plain-TCP port."""
    args = parse(["up", "--expose", "tls", "--port", "9999"])
    overrides = cli._overrides(args)
    assert overrides["engine"]["tls_port"] == 9999
    assert resolve(overrides=overrides).port == 9999


def test_port_flag_targets_the_plain_port_otherwise():
    overrides = cli._overrides(parse(["up", "--port", "9999"]))
    assert overrides["engine"]["port"] == 9999


def test_empty_overrides_are_dropped():
    assert cli._overrides(parse(["status"])) == {}


def test_config_command_prints_resolved_settings(capsys):
    assert cli.main(["config"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["engine"]["name"] == "docker"
    assert printed["resolved"]["endpoint"] == "tcp://127.0.0.1:2375"


def test_config_command_reflects_flags(capsys):
    assert cli.main(["config", "--distro", "Debian"]) == 0
    assert json.loads(capsys.readouterr().out)["distro"]["name"] == "Debian"


def test_bad_config_exits_two(capsys, tmp_path):
    bad = tmp_path / "bosun.toml"
    bad.write_text('[engine]\nexpose = "smoke-signal"\n', encoding="utf-8")
    assert cli.main(["config", "-c", str(bad)]) == 2
    assert "expose" in capsys.readouterr().err


def test_unimplemented_engine_exits_nonzero(capsys):
    assert cli.main(["status", "--engine", "podman"]) == 1
    assert "not implemented" in capsys.readouterr().err


def test_version_flag(capsys):
    with pytest.raises(SystemExit) as exc:
        parse(["--version"])
    assert exc.value.code == 0
    assert "bosun" in capsys.readouterr().out
