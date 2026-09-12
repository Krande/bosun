"""apt lock handling.

Regression tests for a bug found by running `bosun up --dry-run` on a real
machine: the original code probed for a busy apt by process name, which matched
snapd and snapfuse — daemons that run permanently and never hold the apt lock.
Every run therefore waited out its full patience window and then forced a dpkg
repair that nothing had asked for.

The replacement runs the command and reads the failure, so these tests pin the
retry behaviour rather than any particular way of detecting contention.
"""

from __future__ import annotations

import pytest

from bosun import provision
from bosun.config import resolve
from bosun.exec import BosunError, Result, Wsl
from fakes import FakeRunner

LOCKED = Result(
    100,
    "",
    "E: Could not get lock /var/lib/dpkg/lock-frontend. It is held by process 42\n"
    "E: Unable to acquire the dpkg frontend lock, is another process using it?",
)
BROKEN_MIRROR = Result(100, "", "E: Failed to fetch http://archive.ubuntu.com/... 404 Not Found")


def wsl_with(rule, result):
    return Wsl(FakeRunner().on(rule, result), "Ubuntu-24.04")


def test_lock_error_is_recognised():
    assert provision.is_lock_error(LOCKED)


def test_ordinary_failure_is_not_a_lock_error():
    assert not provision.is_lock_error(BROKEN_MIRROR)
    assert not provision.is_lock_error(Result(0, "done"))


def test_lock_markers_match_real_apt_wording():
    """The markers are matched against apt's actual output, so keep them honest."""
    for text in (
        "E: Could not get lock /var/lib/apt/lists/lock",
        "Unable to acquire the dpkg frontend lock",
        "is another process using it?",
        "Waiting for cache lock: Resource temporarily unavailable",
    ):
        assert provision.is_lock_error(Result(1, "", text)), text


def test_success_returns_immediately():
    runner = FakeRunner()
    wsl = Wsl(runner, "Ubuntu-24.04")
    res = provision.run_apt(wsl, "apt-get update", lambda _: None)
    assert res.ok
    assert runner.count("apt-get update") == 1


def test_non_lock_failure_does_not_retry():
    """Retrying a 404 six times just delays the real error by a minute."""
    runner = FakeRunner().on("apt-get update", BROKEN_MIRROR)
    wsl = Wsl(runner, "Ubuntu-24.04")
    res = provision.run_apt(wsl, "apt-get update", lambda _: None)
    assert not res.ok
    assert runner.count("apt-get update") == 1


def test_lock_contention_retries_then_forces():
    runner = FakeRunner().on("apt-get update", LOCKED)
    wsl = Wsl(runner, "Ubuntu-24.04")
    logs: list[str] = []
    provision.run_apt(wsl, "apt-get update", logs.append, attempts=3)

    # Three attempts, then the forced repair, then one final attempt.
    assert runner.count("apt-get update") == 4
    assert runner.ran("systemctl stop unattended-upgrades")
    assert runner.ran("dpkg --configure -a")
    assert any("retrying" in line for line in logs)


def test_retry_succeeds_without_forcing():
    """A lock that clears on its own must not trigger the dpkg repair."""
    calls = {"n": 0}

    def flaky(_joined):
        calls["n"] += 1
        return LOCKED if calls["n"] == 1 else Result(0, "Reading package lists... Done")

    runner = FakeRunner().on("apt-get update", flaky)
    wsl = Wsl(runner, "Ubuntu-24.04")
    res = provision.run_apt(wsl, "apt-get update", lambda _: None, attempts=4)

    assert res.ok
    assert calls["n"] == 2
    assert not runner.ran("dpkg --configure -a")


def test_running_snapd_does_not_cause_a_wait():
    """The original bug: snapd and snapfuse run permanently and matched the probe."""
    runner = FakeRunner()
    runner.out("pgrep", "112 snapfuse /var/lib/snapd/snaps/core.snap\n257 snapd\n")
    wsl = Wsl(runner, "Ubuntu-24.04")

    logs: list[str] = []
    res = provision.run_apt(wsl, "apt-get update", logs.append)

    assert res.ok
    assert not any("retrying" in line for line in logs)
    assert not runner.ran("dpkg --configure -a")


def test_apt_update_raises_with_the_underlying_message():
    wsl = wsl_with("apt-get", BROKEN_MIRROR)
    with pytest.raises(BosunError, match="404"):
        provision.apt_update(wsl, resolve(), lambda _: None)


def test_apt_install_can_fail_softly():
    """The distro-packages attempt is allowed to fail; the upstream one is not."""
    wsl = wsl_with("apt-get", BROKEN_MIRROR)
    assert (
        provision.apt_install(wsl, resolve(), ["docker.io"], lambda _: None, check=False) is False
    )
    with pytest.raises(BosunError):
        provision.apt_install(wsl, resolve(), ["docker.io"], lambda _: None)


def test_no_process_probing_remains():
    """Guards against reintroducing the pre-flight probe in any form."""
    source = (provision.run_apt.__doc__ or "") + (provision.__doc__ or "")
    assert not hasattr(provision, "wait_for_apt")
    assert not hasattr(provision, "LOCK_HOLDERS")
    assert "snapd" not in source
