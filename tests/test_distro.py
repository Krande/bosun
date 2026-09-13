"""Distro discovery, selection and user handling."""

from __future__ import annotations

import pytest

from bosun import distro
from bosun.config import resolve
from bosun.exec import BosunError, Result, Wsl
from fakes import FakeRunner


def test_lists_quiet_output():
    runner = FakeRunner().out("wsl.exe -l -q", "Ubuntu-24.04\nDebian\n")
    assert distro.list_distros(runner) == ["Ubuntu-24.04", "Debian"]


def test_falls_back_to_verbose_listing():
    """The quiet listing comes back empty on some WSL builds."""
    runner = FakeRunner()
    runner.out("wsl.exe -l -q", "")
    runner.out("wsl.exe -l -v", "  NAME      STATE    VERSION\n* Ubuntu-24.04  Running  2\n")
    assert distro.list_distros(runner) == ["Ubuntu-24.04"]


def test_verbose_fallback_skips_a_localised_header():
    """The header is translated; it is skipped by position, not by matching text."""
    runner = FakeRunner()
    runner.out("wsl.exe -l -q", "")
    runner.out("wsl.exe -l -v", "  NAVN      TILSTAND  VERSJON\n* Ubuntu-24.04  Kjorer  2\n")
    assert distro.list_distros(runner) == ["Ubuntu-24.04"]


def test_verbose_fallback_strips_the_default_marker():
    runner = FakeRunner()
    runner.out("wsl.exe -l -q", "")
    runner.out(
        "wsl.exe -l -v", "NAME STATE VERSION\n* Ubuntu-24.04 Running 2\n  Debian Stopped 2\n"
    )
    assert distro.list_distros(runner) == ["Ubuntu-24.04", "Debian"]


def test_find_prefers_the_configured_name():
    runner = FakeRunner().out("wsl.exe -l -q", "Ubuntu-22.04\nUbuntu-24.04\n")
    assert distro.find(runner, resolve()) == "Ubuntu-24.04"


def test_find_adopts_an_existing_distro_by_match():
    """An existing Ubuntu-22.04 is reused rather than installing a second distro."""
    runner = FakeRunner().out("wsl.exe -l -q", "Ubuntu-22.04\n")
    assert distro.find(runner, resolve()) == "Ubuntu-22.04"


def test_find_returns_none_when_nothing_matches():
    runner = FakeRunner().out("wsl.exe -l -q", "Debian\nkali-linux\n")
    assert distro.find(runner, resolve()) is None


def test_find_respects_a_configured_match():
    runner = FakeRunner().out("wsl.exe -l -q", "Debian\n")
    cfg = resolve(overrides={"distro": {"name": "Debian-13", "match": "debian"}})
    assert distro.find(runner, cfg) == "Debian"


def test_launchable_probes_a_root_shell():
    runner = FakeRunner()
    assert distro.is_launchable(runner, "Ubuntu-24.04")
    assert runner.ran("-u root -- true")


def test_ensure_ready_refuses_to_install_when_disabled():
    runner = FakeRunner().out("wsl.exe -l -q", "")
    cfg = resolve(overrides={"distro": {"install": False}})
    with pytest.raises(BosunError, match="install"):
        distro.ensure_ready(runner, cfg, lambda _: None)


def test_ensure_ready_explains_an_unstartable_distro():
    runner = FakeRunner().out("wsl.exe -l -q", "Ubuntu-24.04\n")
    runner.fail("-u root -- true")
    with pytest.raises(BosunError, match="will not start"):
        distro.ensure_ready(runner, resolve(), lambda _: None)


@pytest.mark.parametrize(
    "name,expected",
    [
        ("Ubuntu-24.04", "Canonical.Ubuntu.2404"),
        ("Ubuntu-22.04", "Canonical.Ubuntu.2204"),
        ("Ubuntu", "Canonical.Ubuntu"),
    ],
)
def test_winget_id_mapping(name, expected):
    assert distro._winget_id(name) == expected


# ── user resolution ────────────────────────────────────────────────────────


def test_configured_user_wins():
    wsl = Wsl(FakeRunner(), "Ubuntu-24.04")
    cfg = resolve(overrides={"distro": {"user": "configured"}})
    assert distro.resolve_user(wsl, cfg) == "configured"


def test_falls_back_to_the_existing_default_user():
    wsl = Wsl(FakeRunner().out("whoami", "existing\n"), "Ubuntu-24.04")
    assert distro.resolve_user(wsl, resolve()) == "existing"


def test_root_is_not_treated_as_the_default_user():
    """whoami returns root on a distro with no default user configured yet."""
    wsl = Wsl(FakeRunner().out("whoami", "root\n"), "Ubuntu-24.04")
    assert distro.current_user(wsl) is None


def test_no_prompt_mode_fails_instead_of_asking():
    wsl = Wsl(FakeRunner().out("whoami", "root\n"), "Ubuntu-24.04")
    with pytest.raises(BosunError, match="username"):
        distro.resolve_user(wsl, resolve(), prompt=False)


def test_group_membership_is_a_noop_when_already_present():
    runner = FakeRunner().on("grep -qx docker", Result(0))
    wsl = Wsl(runner, "Ubuntu-24.04")
    assert distro.ensure_in_group(wsl, "dev", "docker", lambda _: None) is False
    assert not runner.ran("usermod -aG docker")


def test_group_membership_is_added_when_missing():
    runner = FakeRunner().on("grep -qx docker", Result(1))
    wsl = Wsl(runner, "Ubuntu-24.04")
    assert distro.ensure_in_group(wsl, "dev", "docker", lambda _: None) is True
    assert runner.ran("usermod -aG docker dev")


def test_provisioning_never_writes_a_sudoers_file():
    """bosun uses `wsl -u root` instead; a stale NOPASSWD file is a real risk."""
    runner = FakeRunner().on("grep -qx docker", Result(1))
    wsl = Wsl(runner, "Ubuntu-24.04")
    distro.ensure_in_group(wsl, "dev", "docker", lambda _: None)
    assert not any("sudoers" in " ".join(c) for c in runner.calls)


def test_wsl_conf_is_left_alone_when_already_correct():
    """Rewriting it would force a restart on every run."""
    current = "[boot]\nsystemd=true\n\n[user]\ndefault=dev\n"
    runner = FakeRunner().out("cat /etc/wsl.conf", current)
    wsl = Wsl(runner, "Ubuntu-24.04")
    assert distro.configure_wsl_conf(wsl, resolve(), "dev", lambda _: None) is False


def test_wsl_conf_is_written_when_the_user_differs():
    runner = FakeRunner().out("cat /etc/wsl.conf", "[user]\ndefault=other\n")
    wsl = Wsl(runner, "Ubuntu-24.04")
    assert distro.configure_wsl_conf(wsl, resolve(), "dev", lambda _: None) is True
    assert "default=dev" in runner.written_content(-1)


# ── usernames and passwords ────────────────────────────────────────────────
#
# useradd's own refusal is a bare exit code and a message about an "invalid
# user name" that does not say which rule was broken, so the check happens
# before the account is created.


@pytest.mark.parametrize("name", ["dev", "_svc", "a", "user-1", "x" * 32])
def test_valid_usernames_are_accepted(name):
    assert distro.validate_username(name) == name


@pytest.mark.parametrize(
    "name", ["1dev", "Dev", "dev user", "dev!", "", "   ", "-dev", "x" * 33, "dev.name"]
)
def test_invalid_usernames_are_rejected(name):
    with pytest.raises(BosunError, match="not a valid Linux username"):
        distro.validate_username(name)


def test_a_configured_username_is_validated():
    """A typo in bosun.toml should fail here, not inside useradd."""
    wsl = Wsl(FakeRunner(), "Ubuntu-24.04")
    cfg = resolve(overrides={"distro": {"user": "Not Valid"}})
    with pytest.raises(BosunError, match="not a valid Linux username"):
        distro.resolve_user(wsl, cfg)


def test_an_existing_user_is_not_re_validated():
    """Linux already accepted it, and bosun's rules may be stricter."""
    wsl = Wsl(FakeRunner().out("whoami", "Odd.Name\n"), "Ubuntu-24.04")
    assert distro.resolve_user(wsl, resolve()) == "Odd.Name"


def test_generated_passwords_are_random_and_long_enough():
    a, b = distro.generate_password(), distro.generate_password()
    assert a != b
    assert len(a) == 20


def test_no_console_for_the_username_explains_the_alternatives(monkeypatch):
    def no_console(_prompt):
        raise EOFError

    monkeypatch.setattr("builtins.input", no_console)
    wsl = Wsl(FakeRunner().out("whoami", "root\n"), "Ubuntu-24.04")
    with pytest.raises(BosunError, match="BOSUN_USER"):
        distro.resolve_user(wsl, resolve())


def test_a_new_account_gets_a_generated_password_without_a_console(monkeypatch):
    """A sudo-capable account with no password cannot be used interactively
    later, and that surfaces long after this run."""

    def no_console(_prompt):
        raise EOFError

    monkeypatch.setattr("bosun.distro.getpass.getpass", no_console)
    runner = FakeRunner().on("id -u dev", Result(1))
    logs: list[str] = []

    distro.ensure_user(Wsl(runner, "Ubuntu-24.04"), "dev", logs.append)

    assert runner.ran("chpasswd")
    assert any("generated one" in line for line in logs)
    assert any("passwd dev" in line for line in logs), "must say how to change it"


def test_the_password_never_reaches_the_command_line(monkeypatch):
    """It goes on stdin so it cannot appear in a process listing."""
    monkeypatch.setattr("bosun.distro.getpass.getpass", lambda _p: "hunter2")
    runner = FakeRunner().on("id -u dev", Result(1))

    distro.ensure_user(Wsl(runner, "Ubuntu-24.04"), "dev", lambda _: None)

    assert any("hunter2" in (s or "") for s in runner.stdins)
    assert not any("hunter2" in " ".join(c) for c in runner.calls)
