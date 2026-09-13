"""End-to-end flows against a scripted machine.

These are the tests that would have caught the ordering bugs: that wsl.conf is
written before systemd is required, that a group change forces a restart, and
that a healthy machine is left completely alone on a second run.
"""

from __future__ import annotations

import pytest

from bosun import flows
from bosun.config import resolve
from bosun.engines import UnsupportedEngine
from bosun.exec import BosunError, Result
from fakes import FakeRunner, healthy_machine


@pytest.fixture(autouse=True)
def host_cli_present(monkeypatch):
    """Pin the host-PATH probe so these tests do not depend on the dev box."""
    monkeypatch.setattr("bosun.client.have", lambda _name: True)


def log_to(sink: list[str]):
    return sink.append


def test_up_succeeds_on_a_healthy_machine():
    runner = healthy_machine()
    out: list[str] = []
    assert flows.up(runner, resolve(), log_to(out), prompt=False) == 0
    assert "Done." in out


def test_up_is_idempotent_on_a_provisioned_machine():
    """The whole point of re-runnability: no writes, no restarts, no downtime."""
    runner = healthy_machine()
    # daemon.json already says exactly what bosun wants.
    from bosun import daemonjson
    from bosun.engines import DOCKER

    runner.out("cat /etc/docker/daemon.json", daemonjson.render("", resolve(), DOCKER))
    runner.out(
        "cat /etc/systemd/system/docker.service.d/bosun.conf", daemonjson.systemd_override(DOCKER)
    )

    flows.up(runner, resolve(), lambda _: None, prompt=False)

    assert not runner.ran("systemctl restart docker")
    assert not runner.ran("wsl.exe --shutdown")
    assert not runner.ran("apt-get")


def test_up_restarts_wsl_when_group_membership_changes():
    """Group membership is only read at login, so the restart is mandatory."""
    runner = healthy_machine(in_group=False)
    flows.up(runner, resolve(), lambda _: None, prompt=False)
    assert runner.ran("usermod -aG docker")
    assert runner.ran("wsl.exe --shutdown")


def test_up_fails_clearly_when_systemd_never_comes_up():
    runner = healthy_machine(systemd=False)
    with pytest.raises(BosunError, match="systemd"):
        flows.up(runner, resolve(), lambda _: None, prompt=False)


def test_up_skips_systemd_requirement_when_disabled():
    runner = healthy_machine(systemd=False)
    cfg = resolve(overrides={"distro": {"systemd": False}})
    # Still fails later for other reasons on a non-systemd box, but not on the
    # systemd precondition — which is what this asserts.
    try:
        flows.up(runner, cfg, lambda _: None, prompt=False)
    except BosunError as exc:
        assert "systemd is enabled" not in str(exc)


def test_up_installs_the_engine_when_absent():
    runner = healthy_machine()
    runner.fail("command -v docker")
    runner.out("list-unit-files", "")
    # The engine never appears on PATH in this fake, so the run ends in the
    # honest failure rather than a false success — what matters is that it
    # tried to install before giving up.
    with pytest.raises(BosunError, match="not on PATH"):
        flows.up(runner, resolve(), lambda _: None, prompt=False)
    assert runner.ran("apt-get")


def test_engine_install_falls_back_to_the_upstream_repo():
    """Distro packages are tried first; the vendor repo is the fallback."""
    runner = healthy_machine()
    runner.fail("command -v docker")
    runner.out("list-unit-files", "")
    with pytest.raises(BosunError):
        flows.up(runner, resolve(), lambda _: None, prompt=False)
    assert runner.ran("download.docker.com")
    assert runner.ran("apt-get -y purge docker.io")


def test_up_rejects_an_unimplemented_engine_before_touching_anything():
    """Refusing up front beats half-provisioning a machine."""
    runner = FakeRunner()
    with pytest.raises(UnsupportedEngine, match="not implemented"):
        flows.up(runner, resolve(overrides={"engine": {"name": "podman"}}), lambda _: None)
    assert runner.calls == []


def test_unix_mode_creates_no_context():
    runner = healthy_machine()
    cfg = resolve(overrides={"engine": {"expose": "unix"}})
    flows.up(runner, cfg, lambda _: None, prompt=False)
    assert not runner.ran("context create")


def test_up_reports_the_endpoint_and_context():
    runner = healthy_machine()
    out: list[str] = []
    flows.up(runner, resolve(), log_to(out), prompt=False)
    joined = "\n".join(out)
    assert "tcp://127.0.0.1:2375" in joined
    assert "bosun" in joined


# ── down ───────────────────────────────────────────────────────────────────


def test_down_requires_confirmation():
    runner = healthy_machine()
    assert flows.down(runner, resolve(), lambda _: None, lambda _: False) == 1
    assert not runner.ran("--unregister")


def test_down_unregisters_when_confirmed():
    runner = healthy_machine()
    assert flows.down(runner, resolve(), lambda _: None, lambda _: True) == 0
    assert runner.ran("--unregister Ubuntu-24.04")


def test_down_skips_the_prompt_with_assume_yes():
    runner = healthy_machine()

    def never(_):
        raise AssertionError("should not have prompted")

    assert flows.down(runner, resolve(), lambda _: None, never, assume_yes=True) == 0


def test_down_removes_the_stale_context():
    """A context pointing at a deleted distro breaks every later client command."""
    runner = healthy_machine()
    flows.down(runner, resolve(), lambda _: None, lambda _: True, assume_yes=True)
    assert runner.ran("context rm")


def test_down_on_a_clean_machine_is_a_noop():
    runner = FakeRunner().out("wsl.exe -l -q", "")
    out: list[str] = []
    assert flows.down(runner, resolve(), log_to(out), lambda _: True) == 0
    assert "nothing to do" in "\n".join(out)


def test_down_reports_an_unregister_failure():
    runner = healthy_machine()
    runner.on("--unregister", Result(1, "", "access denied"))
    with pytest.raises(BosunError, match="access denied"):
        flows.down(runner, resolve(), lambda _: None, lambda _: True, assume_yes=True)


# ── status ─────────────────────────────────────────────────────────────────


def test_status_passes_on_a_healthy_machine():
    runner = healthy_machine()
    assert flows.status(runner, resolve(), lambda _: None) == 0


def test_status_fails_and_names_the_first_problem():
    runner = healthy_machine()
    runner.out("systemctl is-active docker", "inactive\n")
    out: list[str] = []
    assert flows.status(runner, resolve(), log_to(out)) == 1
    assert "First failure" in "\n".join(out)


def test_status_changes_nothing():
    """A diagnostic that mutates the machine is not a diagnostic."""
    runner = healthy_machine()
    flows.status(runner, resolve(), lambda _: None)
    for joined in runner.displays:
        for forbidden in ("apt-get", "systemctl restart", "usermod", "--unregister", "--shutdown"):
            assert forbidden not in joined


def test_status_on_a_machine_with_no_distro():
    runner = FakeRunner().out("wsl.exe -l -q", "")
    assert flows.status(runner, resolve(), lambda _: None) == 1


# ── shrink ─────────────────────────────────────────────────────────────────


def test_shrink_clean_only_skips_the_compaction():
    runner = healthy_machine()
    assert flows.shrink(runner, resolve(), lambda _: None, clean_only=True) == 0
    assert runner.ran("fstrim")
    assert not runner.ran("diskpart")


def test_shrink_trims_before_compacting():
    """Compaction only reclaims blocks the filesystem has already released."""
    runner = healthy_machine()
    runner.out("diskpart", "DiskPart successfully compacted the virtual disk file")
    flows.shrink(runner, resolve(overrides={"vhdx": {"path": __file__}}), lambda _: None)
    # displays, not calls: Wsl.sh base64-encodes scripts on the way out.
    order = runner.displays
    assert next(i for i, c in enumerate(order) if "fstrim" in c) < next(
        i for i, c in enumerate(order) if "diskpart" in c
    )


def test_shrink_reports_a_diskpart_failure_that_exited_zero():
    """diskpart reports failures in stdout while still exiting 0."""
    runner = healthy_machine()
    runner.out("diskpart", "Virtual Disk Service error: The process cannot access the file")
    with pytest.raises(BosunError, match="Administrator"):
        flows.shrink(runner, resolve(overrides={"vhdx": {"path": __file__}}), lambda _: None)


def test_shrink_without_a_distro_fails_clearly():
    runner = FakeRunner().out("wsl.exe -l -q", "")
    with pytest.raises(BosunError, match="registered"):
        flows.shrink(runner, resolve(), lambda _: None)
