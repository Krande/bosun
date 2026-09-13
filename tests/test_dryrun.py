"""--dry-run must not change the machine.

Regression tests for a bug found by running `bosun up --dry-run` against a live
distro: DryRunRunner inferred read-vs-write from the command string, and
`bash -lc` contains `-l`, which was on the read-only hint list. So every command
sent into WSL looked like a read unless it happened to also contain one of a few
write keywords — and `systemctl restart docker` did not, so the dry run
restarted the service. Neither did `docker system prune -af`.

Callers now declare `read_only` explicitly and the default is "this writes", so
an unmarked command is skipped rather than executed. These tests pin the
dangerous commands specifically, plus the general rule.
"""

from __future__ import annotations

import contextlib

import pytest

from bosun import flows, provision, vhdx
from bosun.config import resolve
from bosun.engines import DOCKER
from bosun.exec import BosunError, DryRunRunner, Result, Wsl
from fakes import FakeRunner, healthy_machine


@pytest.fixture(autouse=True)
def host_cli_present(monkeypatch):
    monkeypatch.setattr("bosun.client.have", lambda _name: True)


def dry(inner=None):
    return DryRunRunner(inner or FakeRunner())


# ── the specific commands that escaped before ──────────────────────────────

DANGEROUS = [
    "systemctl restart docker",
    "systemctl enable docker",
    "systemctl stop unattended-upgrades.service",
    "docker system prune -af",
    "apt-get -y purge docker.io",
    "usermod -aG docker dev",
    "fstrim -av",
    "dpkg --configure -a",
    "rm -rf /etc/docker",
]


@pytest.mark.parametrize("script", DANGEROUS)
def test_mutating_shell_commands_are_skipped(script):
    inner = FakeRunner()
    Wsl(dry(inner), "Ubuntu-24.04").sh(script, user="root")
    assert inner.calls == [], f"dry run executed: {script}"


@pytest.mark.parametrize(
    "cmd",
    [
        ["wsl.exe", "--shutdown"],
        ["wsl.exe", "--unregister", "Ubuntu-24.04"],
        ["diskpart"],
        ["docker", "context", "create", "bosun", "--docker", "host=tcp://127.0.0.1:2375"],
        ["docker", "context", "rm", "-f", "bosun"],
        ["pixi", "global", "install", "docker-cli"],
    ],
)
def test_mutating_host_commands_are_skipped(cmd):
    inner = FakeRunner()
    dry(inner).run(cmd)
    assert inner.calls == [], f"dry run executed: {' '.join(cmd)}"


def test_bash_lc_alone_does_not_make_a_command_a_read():
    """`bash -lc` contains `-l`, which is what broke the old inference."""
    inner = FakeRunner()
    Wsl(dry(inner), "Ubuntu-24.04").sh("anything at all", user="root")
    assert inner.calls == []


def test_writing_a_file_is_skipped():
    inner = FakeRunner()
    Wsl(dry(inner), "Ubuntu-24.04").write_file("/etc/docker/daemon.json", "{}")
    assert inner.calls == []


# ── declared reads still execute, so a dry run reports real state ──────────


def test_declared_reads_execute():
    inner = FakeRunner().out("whoami", "dev\n")
    assert Wsl(dry(inner), "Ubuntu-24.04").sh("whoami", read_only=True).out == "dev"
    assert inner.calls


def test_probes_execute():
    inner = FakeRunner().out("wsl.exe -l -q", "Ubuntu-24.04\n")
    assert dry(inner).run(["wsl.exe", "-l", "-q"], read_only=True).out == "Ubuntu-24.04"


def test_ok_is_read_only_by_default():
    """A probe answered by a synthetic success sends the run down a false branch."""
    inner = FakeRunner().on("command -v docker", Result(1))
    assert Wsl(dry(inner), "Ubuntu-24.04").ok("command -v docker") is False
    assert inner.calls


def test_read_file_executes():
    inner = FakeRunner().out("cat /etc/wsl.conf", "[boot]\nsystemd=true\n")
    assert "systemd=true" in Wsl(dry(inner), "Ubuntu-24.04").read_file("/etc/wsl.conf")


# ── whole flows ────────────────────────────────────────────────────────────


def test_up_changes_nothing_under_dry_run():
    inner = healthy_machine()
    flows.up(dry(inner), resolve(), lambda _: None, prompt=False)
    for joined in inner.displays:
        for forbidden in (
            "apt-get",
            "systemctl restart",
            "systemctl enable",
            "usermod",
            "--shutdown",
            "context create",
            "install -o",
        ):
            assert forbidden not in joined, f"dry run executed: {joined}"


def test_shrink_changes_nothing_under_dry_run():
    """The destructive one: prune and diskpart both ran for real before."""
    inner = healthy_machine()
    runner = dry(inner)
    # diskpart is skipped under dry run, so the compaction check reports failure.
    # That is the correct outcome here; what matters is what never ran.
    with contextlib.suppress(BosunError):
        flows.shrink(runner, resolve(overrides={"vhdx": {"path": __file__}}), lambda _: None)
    for joined in inner.displays:
        assert "prune" not in joined
        assert "diskpart" not in joined
        assert "fstrim" not in joined


def test_down_does_not_unregister_under_dry_run():
    inner = healthy_machine()
    flows.down(dry(inner), resolve(), lambda _: None, lambda _: True, assume_yes=True)
    assert not inner.ran("--unregister")


def test_skipped_commands_are_recorded():
    runner = dry()
    runner.run(["wsl.exe", "--shutdown"])
    assert runner.skipped == [["wsl.exe", "--shutdown"]]


# ── the marking itself ─────────────────────────────────────────────────────


def test_diagnose_marks_every_probe_read_only():
    """`bosun status` must work under --dry-run, so all of it must be declared."""
    from bosun import diagnose

    runner = healthy_machine()
    diagnose.run_checks(runner, resolve(), DOCKER)
    assert all(runner.read_flags), "a status check was not marked read_only"


def test_engine_probes_are_marked_read_only():
    runner = healthy_machine()
    wsl = Wsl(runner, "Ubuntu-24.04")
    provision.engine_present(wsl, DOCKER)
    provision.service_unit_present(wsl, DOCKER)
    assert all(runner.read_flags)


def test_apt_is_never_marked_read_only():
    runner = FakeRunner()
    wsl = Wsl(runner, "Ubuntu-24.04")
    provision.run_apt(wsl, "apt-get update", lambda _: None)
    assert not any(runner.read_flags)


def test_vhdx_cleanup_is_never_marked_read_only():
    runner = FakeRunner()
    wsl = Wsl(runner, "Ubuntu-24.04")
    vhdx.reclaim_inside(wsl, DOCKER, lambda _: None)
    prune = [f for d, f in zip(runner.displays, runner.read_flags, strict=False) if "prune" in d]
    assert not any(prune)


# ── the Windows side of the endpoint ───────────────────────────────────────
#
# Found on a live machine: the engine was listening inside the distro, `ss`
# confirmed it, and Windows still got connection refused. WSL relays Windows
# localhost into the distro, and the relay misses a listener that came up while
# the distro was still booting — which is exactly what systemd does at boot.
# A check run inside the distro reports healthy for a setup that does not work.


def unreachable(monkeypatch):
    def boom(*_a, **_k):
        raise OSError("connection refused")

    monkeypatch.setattr("bosun.client.socket.create_connection", boom)


def test_reachability_is_measured_from_windows(monkeypatch):
    from bosun import client

    assert client.endpoint_reachable(resolve()) is True
    unreachable(monkeypatch)
    assert client.endpoint_reachable(resolve()) is False


def test_unix_mode_has_nothing_to_reach(monkeypatch):
    from bosun import client

    unreachable(monkeypatch)
    cfg = resolve(overrides={"engine": {"expose": "unix"}})
    assert client.endpoint_reachable(cfg) is True


def test_status_fails_when_windows_cannot_reach_the_engine(monkeypatch):
    """The check that used to pass while docker was unusable from Windows."""
    from bosun import diagnose

    unreachable(monkeypatch)
    checks = diagnose.run_checks(healthy_machine(), resolve(), DOCKER)
    failed = [c.name for c in checks if c.required and not c.ok]

    assert "endpoint reachable from Windows" in failed
    assert not diagnose.healthy(checks)


def test_up_restarts_the_engine_when_windows_cannot_reach_it(monkeypatch):
    """Restarting once the distro is fully up is what makes the relay notice."""
    unreachable(monkeypatch)
    runner = healthy_machine()
    logs: list[str] = []

    flows.up(runner, resolve(), logs.append, prompt=False)

    assert runner.ran("systemctl restart docker")
    assert any("not reachable from Windows" in line for line in logs)
    assert any("warning" in line for line in logs), "must not claim success when it failed"
