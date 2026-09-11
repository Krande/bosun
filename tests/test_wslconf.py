"""Editing /etc/wsl.conf without destroying what is already in it.

The preservation tests carry most of the weight here. A distro's wsl.conf often
carries [network] or [automount] settings that bosun knows nothing about, and
losing them breaks DNS or drive mounts long after the run that caused it.
"""

from __future__ import annotations

from bosun import wslconf


def test_adds_section_to_empty_file():
    out = wslconf.with_systemd("")
    assert "[boot]" in out
    assert "systemd=true" in out
    assert wslconf.systemd_enabled(out)


def test_updates_existing_key_in_place():
    out = wslconf.with_systemd("[boot]\nsystemd=false\n")
    assert out.count("systemd") == 1
    assert wslconf.systemd_enabled(out)


def test_appends_key_to_existing_section():
    out = wslconf.with_systemd("[boot]\ncommand=echo hi\n")
    assert "command=echo hi" in out
    assert wslconf.systemd_enabled(out)


def test_preserves_unrelated_sections():
    original = (
        "[automount]\nenabled=true\noptions=metadata\n\n[network]\ngenerateResolvConf=false\n"
    )
    out = wslconf.with_systemd(original)
    for line in ("[automount]", "options=metadata", "[network]", "generateResolvConf=false"):
        assert line in out
    assert wslconf.systemd_enabled(out)


def test_preserves_comments():
    original = "# hand-tuned, do not clobber\n[boot]\n# turn this on one day\nsystemd=false\n"
    out = wslconf.with_systemd(original)
    assert "# hand-tuned, do not clobber" in out
    assert "# turn this on one day" in out


def test_key_lands_in_the_right_section():
    """A key added to [boot] must not leak into whatever section follows it."""
    original = "[boot]\nsystemd=false\n\n[automount]\nenabled=true\n"
    out = wslconf.set_key(original, "boot", "command", "echo hi")
    boot, _, automount = out.partition("[automount]")
    assert "command=echo hi" in boot
    assert "command" not in automount
    assert "enabled=true" in automount


def test_inserts_into_section_that_runs_to_eof():
    out = wslconf.with_default_user("[boot]\nsystemd=true\n\n[user]\n", "dev")
    assert wslconf.default_user(out) == "dev"
    assert wslconf.systemd_enabled(out)


def test_setting_both_keys_is_stable():
    once = wslconf.with_default_user(wslconf.with_systemd(""), "dev")
    twice = wslconf.with_default_user(wslconf.with_systemd(once), "dev")
    assert once == twice


def test_get_returns_none_for_missing():
    assert wslconf.get("[boot]\nsystemd=true\n", "user", "default") is None
    assert wslconf.default_user("") is None


def test_systemd_enabled_accepts_truthy_spellings():
    for value in ("true", "True", "1", "yes", "on"):
        assert wslconf.systemd_enabled(f"[boot]\nsystemd={value}\n")
    assert not wslconf.systemd_enabled("[boot]\nsystemd=false\n")


def test_section_matching_is_case_insensitive():
    out = wslconf.with_systemd("[Boot]\nsystemd=false\n")
    assert out.count("systemd") == 1
