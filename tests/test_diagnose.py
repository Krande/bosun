"""The status checks and their rendering."""

from __future__ import annotations

import pytest

from bosun import diagnose
from bosun.config import resolve
from bosun.diagnose import Check
from bosun.engines import DOCKER
from fakes import FakeRunner, healthy_machine


@pytest.fixture(autouse=True)
def host_cli_present(monkeypatch):
    """Pin the host-PATH probe so these tests do not depend on the dev box."""
    monkeypatch.setattr("bosun.diagnose.have", lambda _name: True)


def names(checks):
    return [c.name for c in checks]


def test_healthy_machine_passes_every_required_check():
    checks = diagnose.run_checks(healthy_machine(), resolve(), DOCKER)
    assert diagnose.healthy(checks)


def test_missing_distro_short_circuits_with_a_useful_detail():
    runner = FakeRunner().out("wsl.exe -l -q", "Debian\n")
    checks = diagnose.run_checks(runner, resolve(), DOCKER)
    assert len(checks) == 1
    assert not checks[0].ok
    assert "Debian" in checks[0].detail


def test_unstartable_distro_stops_before_probing_inside_it():
    runner = FakeRunner().out("wsl.exe -l -q", "Ubuntu-24.04\n").fail("-u root -- true")
    checks = diagnose.run_checks(runner, resolve(), DOCKER)
    assert names(checks) == ["distro registered", "distro starts"]


def test_inactive_service_is_reported():
    runner = healthy_machine()
    runner.out("systemctl is-active docker", "inactive\n")
    checks = diagnose.run_checks(runner, resolve(), DOCKER)
    assert not diagnose.healthy(checks)
    failed = [c.name for c in checks if c.required and not c.ok]
    assert "docker service active" in failed


def test_missing_group_membership_explains_the_restart():
    runner = healthy_machine(in_group=False)
    checks = diagnose.run_checks(runner, resolve(), DOCKER)
    group = next(c for c in checks if "group" in c.name)
    assert not group.ok
    assert "restart" in group.detail


def test_unix_mode_does_not_require_a_listening_port():
    runner = healthy_machine()
    cfg = resolve(overrides={"engine": {"expose": "unix"}})
    checks = diagnose.run_checks(runner, cfg, DOCKER)
    assert not any("listening" in c.name for c in checks)
    assert diagnose.healthy(checks)


def test_host_side_checks_are_optional():
    """A missing Windows CLI is worth reporting but is not a broken machine."""
    checks = diagnose.run_checks(healthy_machine(), resolve(), DOCKER)
    host = next(c for c in checks if "Windows PATH" in c.name)
    assert not host.required


def test_render_marks_pass_fail_and_not_applicable():
    rendered = diagnose.render(
        [
            Check("passing", True),
            Check("failing", False),
            Check("optional", False, required=False),
        ],
        marks=diagnose.GLYPH_MARKS,
    )
    assert "✓ passing" in rendered
    assert "✗ failing" in rendered
    assert "– optional" in rendered


def test_render_includes_details():
    assert "(Ubuntu-24.04)" in diagnose.render([Check("distro", True, "Ubuntu-24.04")])


# ── encoding fallback ──────────────────────────────────────────────────────
#
# Windows hands stdout a cp1252 encoding whenever output is piped or the console
# is not on a UTF-8 code page. The glyphs are unencodable there, and printing
# them raises rather than degrading — which turned `bosun status | tee log.txt`
# into a traceback before this fallback existed.


class _Stream:
    def __init__(self, encoding):
        self.encoding = encoding


def test_glyphs_used_when_the_stream_can_encode_them():
    assert diagnose.pick_marks(_Stream("utf-8")) == diagnose.GLYPH_MARKS


def test_ascii_used_on_a_legacy_windows_code_page():
    assert diagnose.pick_marks(_Stream("cp1252")) == diagnose.ASCII_MARKS


def test_ascii_used_when_the_encoding_is_unknown_or_absent():
    assert diagnose.pick_marks(_Stream(None)) == diagnose.ASCII_MARKS
    assert diagnose.pick_marks(_Stream("not-a-real-codec")) == diagnose.ASCII_MARKS


def test_every_ascii_marker_survives_cp1252():
    """The fallback is worthless if it cannot itself be printed."""
    for marker in diagnose.ASCII_MARKS.values():
        marker.encode("cp1252")


def test_rendered_table_is_encodable_on_cp1252():
    rendered = diagnose.render(
        [Check("a", True), Check("b", False), Check("c", False, required=False)],
        marks=diagnose.ASCII_MARKS,
    )
    rendered.encode("cp1252")


def test_markers_are_distinct():
    for marks in (diagnose.GLYPH_MARKS, diagnose.ASCII_MARKS):
        assert len(set(marks.values())) == 3


def test_healthy_ignores_optional_failures():
    assert diagnose.healthy([Check("a", True), Check("b", False, required=False)])
    assert not diagnose.healthy([Check("a", True), Check("b", False)])


# ── output that survives a legacy console ──────────────────────────────────


def test_no_runtime_string_carries_a_character_cp1252_cannot_encode():
    """Windows gives stdout cp1252 whenever output is piped.

    The status markers already fall back to ASCII, but an em-dash in any other
    log line degrades to a replacement character in the same situation, which
    looked like corruption in real output. Docstrings and comments are exempt —
    they are never printed.
    """
    import ast
    import pathlib

    emitters = {
        "log",
        "print",
        "warn",
        "note",
        "info",
        "BosunError",
        "UnsupportedProvider",
        "UnsupportedEngine",
        "ConfigError",
    }
    offenders = []

    for path in sorted(pathlib.Path(diagnose.__file__).parent.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name not in emitters:
                continue
            for arg in ast.walk(node):
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    try:
                        arg.value.encode("cp1252")
                    except UnicodeEncodeError:
                        offenders.append(f"{path.name}:{arg.lineno}")

    assert not offenders, f"non-cp1252 characters in printed strings: {offenders}"
