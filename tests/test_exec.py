"""The process adapter: command construction, dry-run behaviour, TLS rendering."""

from __future__ import annotations

from bosun import tls
from bosun.config import resolve
from bosun.engines import DOCKER
from bosun.exec import DryRunRunner, Result, Wsl
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


def test_write_file_streams_content_over_stdin():
    """Content must never go through a shell-quoting round trip."""
    runner = FakeRunner()
    payload = "tricky 'quotes' $VARS \\backslashes\n"
    Wsl(runner, "Ubuntu-24.04").write_file("/etc/thing", payload)
    assert runner.stdins[0] == payload
    assert payload not in " ".join(runner.calls[0])


def test_write_file_installs_with_the_requested_mode_and_owner():
    runner = FakeRunner()
    Wsl(runner, "Ubuntu-24.04").write_file("/etc/x", "y", mode="0600", owner="root:docker")
    joined = " ".join(runner.calls[0])
    assert "-m 0600" in joined
    assert "-o root -g docker" in joined


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


def test_dry_run_executes_reads():
    inner = FakeRunner().out("wsl.exe -l -q", "Ubuntu-24.04\n")
    runner = DryRunRunner(inner)
    assert runner.run(["wsl.exe", "-l", "-q"]).out == "Ubuntu-24.04"


def test_dry_run_skips_writes():
    inner = FakeRunner()
    runner = DryRunRunner(inner)
    runner.run(["wsl.exe", "--", "bash", "-lc", "apt-get install -y docker.io"])
    assert inner.calls == []
    assert runner.skipped


def test_dry_run_treats_a_redirect_as_a_write():
    inner = FakeRunner()
    DryRunRunner(inner).run(["wsl.exe", "--", "bash", "-lc", "cat /etc/x > /etc/y"])
    assert inner.calls == []


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
