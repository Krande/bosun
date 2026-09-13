"""Unsticking a wedged WSL install.

The important property is what `repair` refuses to do: unregistering a distro
or toggling Windows features destroys a filesystem, and a command called repair
must not do that behind your back.
"""

from __future__ import annotations

from bosun import flows
from bosun.config import resolve
from bosun.exec import Result
from bosun.recovery import LEFTOVER_PROCESSES, Recovery
from fakes import FakeRunner, healthy_machine


def rec(runner, logs=None):
    return Recovery(runner, (logs if logs is not None else []).append)


def test_repair_never_destroys_a_distro():
    """`bosun down` is where that lives, behind a confirmation."""
    runner = healthy_machine()
    flows.repair(runner, resolve(), lambda _: None)
    for forbidden in ("--unregister", "dism", "DISM", "--import", "rm -rf"):
        assert not runner.ran(forbidden), f"repair ran {forbidden}"


def test_the_escalation_runs_least_disruptive_first():
    runner = FakeRunner()
    rec(runner).recover()
    order = runner.displays
    shutdown = next(i for i, d in enumerate(order) if "--shutdown" in d)
    taskkill = next(i for i, d in enumerate(order) if "taskkill" in d)
    assert shutdown < taskkill


def test_leftover_host_processes_are_cleared():
    """They outlive `wsl --shutdown` and keep a dead session pinned."""
    runner = FakeRunner()
    assert rec(runner).clear_leftovers() == list(LEFTOVER_PROCESSES)
    for image in LEFTOVER_PROCESSES:
        assert runner.ran(f"/im {image}")


def test_a_process_that_was_not_running_is_not_reported_as_killed():
    runner = FakeRunner().on("taskkill", Result(128, "", "not found"))
    assert rec(runner).clear_leftovers() == []


def test_the_service_restart_is_skipped_without_elevation():
    """Skipped rather than failed: the earlier steps fix most cases."""
    runner = FakeRunner().on("net session", Result(1, "", "Access is denied"))
    logs: list[str] = []
    assert rec(runner, logs).restart_service() is False
    assert any("Administrator" in line for line in logs)
    assert not runner.ran("net stop")


def test_the_service_is_restarted_when_elevated():
    runner = FakeRunner()
    assert rec(runner).restart_service() is True
    assert runner.ran("net stop LxssManager")
    assert runner.ran("net start LxssManager")


def test_elevation_is_probed_read_only():
    runner = FakeRunner()
    rec(runner).is_elevated()
    assert runner.marked_read_only("net session")


def test_repair_reports_a_distro_that_still_will_not_start():
    runner = healthy_machine().fail("-u root -- true")
    logs: list[str] = []
    assert flows.repair(runner, resolve(), logs.append) == 1
    assert any("still will not start" in line for line in logs)


def test_repair_checks_the_rest_once_wsl_responds():
    runner = healthy_machine()
    logs: list[str] = []
    assert flows.repair(runner, resolve(), logs.append) == 0
    assert any("responding again" in line for line in logs)


def test_repair_with_no_distro_says_so():
    runner = FakeRunner().out("wsl.exe -l -q", "")
    assert flows.repair(runner, resolve(), lambda _: None) == 1


def test_recovery_runs_no_destructive_command():
    """Guards against someone helpfully adding one.

    Asserted on what it actually executes rather than on the source text — the
    module's own docstring names DISM precisely to say it does not use it.
    """
    runner = FakeRunner()
    rec(runner).recover()
    for forbidden in ("--unregister", "dism", "--import", "format", "rmdir"):
        assert not runner.ran(forbidden), f"recovery ran {forbidden}"
