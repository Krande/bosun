"""The process adapter: command construction, dry-run behaviour, TLS rendering."""

from __future__ import annotations

from bosun import tls
from bosun.config import resolve
from bosun.engines import DOCKER
from bosun.exec import DryRunRunner, Result, Wsl, command_line, decode_output
from fakes import FakeRunner


def test_wsl_targets_the_named_distro():
    runner = FakeRunner()
    Wsl(runner, "Ubuntu-24.04").sh("echo hi")
    assert runner.calls[0][:3] == ["wsl.exe", "-d", "Ubuntu-24.04"]


def test_wsl_without_a_distro_uses_the_default():
    runner = FakeRunner()
    Wsl(runner).sh("echo hi")
    assert "-d" not in runner.calls[0]


def test_wsl_passes_the_user_flag():
    runner = FakeRunner()
    Wsl(runner, "Ubuntu-24.04").sh("id", user="root")
    assert "-u" in runner.calls[0] and "root" in runner.calls[0]


def test_write_file_carries_content_as_base64():
    """Content must never go through a shell-quoting round trip."""
    runner = FakeRunner()
    payload = "tricky 'quotes' $VARS \\backslashes\n"
    Wsl(runner, "Ubuntu-24.04").write_file("/etc/thing", payload)
    assert runner.written_content(0) == payload
    assert payload not in " ".join(runner.calls[0])


def test_write_file_installs_with_the_requested_mode_and_owner():
    runner = FakeRunner()
    Wsl(runner, "Ubuntu-24.04").write_file("/etc/x", "y", mode="0600", owner="root:docker")
    script = runner.decoded_script(0)
    assert "-m 0600" in script
    assert "-o root -g docker" in script


def test_write_file_always_runs_as_root():
    runner = FakeRunner()
    Wsl(runner, "Ubuntu-24.04").write_file("/etc/x", "y")
    assert "root" in runner.calls[0]


def test_read_file_returns_empty_when_absent():
    runner = FakeRunner().on("cat /nope", Result(1, "", "No such file"))
    assert Wsl(runner, "Ubuntu-24.04").read_file("/nope") == ""


def test_result_helpers():
    assert Result(0, " value \n").out == "value"
    assert Result(0).ok
    assert not Result(1).ok


# ── dry run ────────────────────────────────────────────────────────────────


def test_dry_run_executes_declared_reads():
    inner = FakeRunner().out("wsl.exe -l -q", "Ubuntu-24.04\n")
    runner = DryRunRunner(inner)
    assert runner.run(["wsl.exe", "-l", "-q"], read_only=True).out == "Ubuntu-24.04"


def test_dry_run_skips_writes():
    inner = FakeRunner()
    runner = DryRunRunner(inner)
    runner.run(["wsl.exe", "--", "bash", "-lc", "apt-get install -y docker.io"])
    assert inner.calls == []
    assert runner.skipped


def test_dry_run_defaults_to_treating_a_command_as_a_write():
    """The fail-safe direction: an unmarked command costs a log line, not a change."""
    inner = FakeRunner()
    DryRunRunner(inner).run(["something", "unrecognised"])
    assert inner.calls == []


def test_dry_run_passes_the_read_flag_through():
    """The inner runner should see the same declaration, not a rewritten one."""
    inner = FakeRunner()
    DryRunRunner(inner).run(["wsl.exe", "-l", "-q"], read_only=True)
    assert inner.read_flags == [True]


# ── TLS rendering (pure) ───────────────────────────────────────────────────


def test_openssl_config_includes_the_configured_sans():
    """Modern clients ignore the CN; a missing SAN fails the handshake."""
    conf = tls.openssl_config(resolve())
    assert "DNS.1 = localhost" in conf
    assert "IP.1 = 127.0.0.1" in conf


def test_openssl_config_honours_extra_sans():
    cfg = resolve(overrides={"tls": {"san_dns": ["a", "b"], "san_ip": ["10.0.0.1"]}})
    conf = tls.openssl_config(cfg)
    assert "DNS.2 = b" in conf
    assert "IP.1 = 10.0.0.1" in conf


def test_windows_cert_dir_defaults_to_the_engine_convention(tmp_path):
    assert tls.windows_cert_dir(resolve(), DOCKER, tmp_path) == tmp_path / ".docker"


def test_windows_cert_dir_can_be_overridden(tmp_path):
    cfg = resolve(overrides={"tls": {"windows_cert_dir": str(tmp_path / "certs")}})
    assert tls.windows_cert_dir(cfg, DOCKER, tmp_path) == tmp_path / "certs"


def test_only_client_material_is_exported():
    """The CA key and server key must never leave the distro."""
    assert set(tls.CLIENT_FILES) == {"ca.pem", "cert.pem", "key.pem"}
    assert "ca-key.pem" not in tls.CLIENT_FILES
    assert "server-key.pem" not in tls.CLIENT_FILES


# ── script transport ───────────────────────────────────────────────────────
#
# Regression tests for a bug found by running bosun against a live distro: a
# script handed to wsl.exe as a command-line argument loses its single quotes
# somewhere between Windows argv and bash. `awk '{print $1}'` arrived as
# `awk {print $1}`, bash expanded $1 to nothing, and the systemd-unit probe
# reported every unit missing — so `bosun up` decided the engine needed
# reinstalling and would have purged a working docker.io for docker-ce.


def test_scripts_are_base64_encoded_in_transit():
    runner = FakeRunner()
    Wsl(runner, "Ubuntu-24.04").sh("awk '{print $1}'", read_only=True)
    sent = " ".join(runner.calls[0])
    assert "base64 -d" in sent
    assert "awk" not in sent, "the raw script must not travel through Windows argv"


def test_the_encoded_payload_round_trips():
    import base64

    script = "systemctl list-unit-files | awk '{print $1}' | grep -qx docker.service"
    runner = FakeRunner()
    Wsl(runner, "Ubuntu-24.04").sh(script, read_only=True)
    payload = runner.calls[0][-1].split("echo ", 1)[1].split(" |", 1)[0]
    assert base64.b64decode(payload).decode() == script


def test_the_payload_is_free_of_characters_that_get_mangled():
    """Base64 is alphanumeric plus +/= — nothing for Windows or wsl.exe to eat."""
    runner = FakeRunner()
    Wsl(runner, "Ubuntu-24.04").sh("x='$1'; echo \"a b\" | tr ' ' '_'", read_only=True)
    payload = runner.calls[0][-1].split("echo ", 1)[1].split(" |", 1)[0]
    assert not set(payload) & set("'\"$ ")


def test_the_readable_script_is_kept_for_display():
    """--verbose and --dry-run must show the script, not the envelope."""
    runner = FakeRunner()
    Wsl(runner, "Ubuntu-24.04").sh("systemctl restart docker")
    assert runner.displays[0] == "systemctl restart docker"


def test_a_script_needing_stdin_bypasses_the_envelope():
    """The envelope feeds the inner bash on stdin, so data on stdin cannot use it.

    chpasswd is the remaining case: the password goes on stdin specifically to
    keep it out of the process list.
    """
    runner = FakeRunner()
    Wsl(runner, "Ubuntu-24.04").sh("chpasswd", user="root", stdin="dev:secret\n")
    assert "base64 -d" not in " ".join(runner.calls[0])
    assert runner.stdins[0] == "dev:secret\n"


def test_write_file_depends_on_no_shell_path_lookup():
    """Unquoted $PATH inside WSL contains `Program Files (x86)`, a syntax error."""
    runner = FakeRunner()
    Wsl(runner, "Ubuntu-24.04").write_file("/etc/docker/daemon.json", "{}")
    script = runner.decoded_script(0)
    assert "$PATH" not in script
    assert "/usr/bin/install" in script
    assert "/usr/bin/base64" in script


# ── output decoding ────────────────────────────────────────────────────────
#
# Hard-coding one encoding breaks the other half of the calls. wsl.exe writes
# its own messages as UTF-16LE while anything run inside the distro writes
# UTF-8, so before this existed every wsl.exe error reached the user as text
# with a NUL between each character: "There is no distribution with the
# supplied name" was unreadable exactly when it mattered.

UTF16 = "There is no distribution with the supplied name.".encode("utf-16-le")
NORWEGIAN = "hei-\u00e6\u00f8\u00e5"


def test_utf16_from_wsl_exe_is_decoded():
    assert decode_output(UTF16) == "There is no distribution with the supplied name."


def test_utf8_from_inside_the_distro_is_decoded():
    assert decode_output(NORWEGIAN.encode("utf-8")) == NORWEGIAN


def test_non_ascii_survives_intact():
    """Codepoints, not glyphs: a console that cannot print them is a separate
    problem from a decoder that loses them."""
    decoded = decode_output(NORWEGIAN.encode("utf-8"))
    assert [ord(c) for c in decoded[-3:]] == [0xE6, 0xF8, 0xE5]


def test_empty_output_is_empty():
    assert decode_output(b"") == ""
    assert decode_output(None) == ""


def test_the_encoding_is_detected_not_supplied():
    """A caller-supplied encoding is one that can be supplied wrongly, and
    decoding arbitrary even-length bytes as UTF-16 succeeds while producing
    garbage — so a wrong hint would win silently rather than fall back."""
    import inspect

    assert list(inspect.signature(decode_output).parameters) == ["raw"]


def test_undecodable_bytes_degrade_rather_than_raise():
    """A decoder that raises turns a diagnostic into a crash."""
    assert decode_output(b"\xff\xfe\x00\x00garbage")


def test_the_runner_decodes_without_being_told_the_encoding():
    """The listing and the error messages arrive from the same binary."""
    runner = FakeRunner()
    assert runner.run(["wsl.exe", "-l", "-q"]).returncode == 0


# ── result helpers ─────────────────────────────────────────────────────────


def test_combined_covers_both_streams():
    """wsl.exe writes its failures to stdout, so an error message built from
    stderr alone is empty exactly when it is needed."""
    assert Result(1, "on stdout", "").combined == "on stdout"
    assert Result(1, "", "on stderr").combined == "on stderr"


def test_a_timeout_is_not_ok_even_with_a_zero_return_code():
    assert Result(0, timed_out=True).ok is False
    assert Result(0).ok is True


# ── Windows command lines ──────────────────────────────────────────────────


def test_a_path_with_spaces_is_quoted():
    line = command_line([r"C:\Program Files\py\pythonw.exe", "-m", "bosun"])
    assert line.startswith('"C:\Program Files\py\pythonw.exe"')


def test_an_embedded_quote_is_escaped():
    """Quoting on spaces alone silently changes where the argument boundaries
    fall, so the logon entry would run something other than what was meant."""
    import subprocess

    parts = [r"C:\t\x.exe", "--flag", 'a"b']
    assert command_line(parts) == subprocess.list2cmdline(parts)
    assert '\\"' in command_line(parts)


def test_a_simple_command_line_is_left_alone():
    assert command_line(["python", "-m", "bosun"]) == "python -m bosun"
