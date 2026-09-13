"""Holding the distro open.

WSL idles an instance out about a minute after its last Windows-side command,
taking the localhost forward to the engine with it. The subtleties encoded here
were measured rather than reasoned about, so the tests pin the specific
mechanics — if someone swaps dbus-launch for something that looks equivalent,
or drops the brackets from the pgrep pattern, these fail.
"""

from __future__ import annotations

import pathlib
import sys

import pytest

from bosun import flows
from bosun.config import resolve
from bosun.exec import Result, Wsl
from bosun.keepalive import ARMED_NOW, KEEPALIVE_MARKER, KeepAlive
from fakes import FakeRunner, healthy_machine


class Registry:
    def __init__(self, values=None):
        self.values = dict(values or {})

    def get(self, name):
        return self.values.get(name)

    def set(self, name, command):
        self.values[name] = command
        return True

    def delete(self, name):
        return self.values.pop(name, None) is not None


def keeper(runner=None, registry=None, logs=None):
    return KeepAlive(
        runner or FakeRunner(), (logs if logs is not None else []).append, registry or Registry()
    )


# ── the arming mechanics ───────────────────────────────────────────────────


def test_the_marker_cannot_match_itself():
    """`pgrep -f` sees its own shell's command line, which contains the pattern.

    Without the brackets the probe always reports a keep-alive that is not
    there, so nothing ever gets armed — silently.
    """
    assert "--[f]ork" in KEEPALIVE_MARKER
    assert "--fork" not in KEEPALIVE_MARKER.replace("--[f]ork", "")


def test_arming_is_conditional():
    """Unconditional dbus-launch would leak one bus per poll, forever."""
    script = KeepAlive.arm_script()
    assert script.startswith("pgrep -f")
    assert "||" in script
    assert "dbus-launch" in script


def test_arming_reports_a_distinct_code_when_it_acted():
    """Otherwise 'already held' and 'the distro had died' look identical."""
    assert f"exit {ARMED_NOW}" in KeepAlive.arm_script()
    assert ARMED_NOW not in (0, 1, 2, 126, 127)


def test_the_arm_command_goes_through_wsl_exe():
    """WSL counts only processes created by a Windows-side invocation.

    A systemd unit or a wsl.conf boot command inside the instance does not
    count, which is why arming from the Windows side is the whole mechanism.
    """
    cmd = keeper().arm_command("Ubuntu-24.04")
    assert cmd[0] == "wsl.exe"
    assert "--exec" in cmd
    assert "dbus-launch" in " ".join(cmd)


@pytest.mark.parametrize("code", [0, ARMED_NOW])
def test_both_success_codes_are_accepted(code):
    runner = FakeRunner().on("dbus-launch", Result(code))
    assert keeper(runner).arm("Ubuntu-24.04") is True


def test_a_failure_to_arm_is_reported():
    logs: list[str] = []
    runner = FakeRunner().on("dbus-launch", Result(1, "", "no such distro"))
    assert keeper(runner, logs=logs).arm("Ubuntu-24.04") is False
    assert any("could not arm" in line for line in logs)


def test_disarm_does_not_stop_the_distro():
    """Turning the keep-alive off means stop holding it, not shut it down."""
    runner = FakeRunner()
    keeper(runner).disarm("Ubuntu-24.04")
    assert runner.ran("pkill")
    assert not runner.ran("--terminate")
    assert not runner.ran("--shutdown")


# ── the logon entry ────────────────────────────────────────────────────────


def test_the_logon_entry_runs_an_interpreter_not_a_script():
    """A launcher the shell has to interpret can be unavailable, and a hidden
    one that fails has nowhere to report it.

    Asserted against the running interpreter rather than a filename pattern:
    the executable is `python3.14` on a Linux CI runner and `pythonw.exe` on a
    Windows desktop, and the point is neither of those spellings — it is that
    the entry invokes an interpreter with a module, not a script file.
    """
    cmd = keeper().supervisor_command("Ubuntu-24.04")
    interpreter = pathlib.Path(cmd[0])

    assert interpreter.parent == pathlib.Path(sys.executable).parent
    assert interpreter.stem.startswith("python")
    assert "-m" in cmd, "runs a module, so there is no script file to be blocked"
    assert not any(part.endswith((".vbs", ".cmd", ".bat", ".ps1")) for part in cmd)


def test_the_logon_entry_names_the_distro():
    assert "Ubuntu-24.04" in keeper().supervisor_command("Ubuntu-24.04")


def test_registering_is_visible_and_reversible():
    registry = Registry()
    keep = keeper(registry=registry)

    assert keep.is_registered() is False
    assert keep.register("Ubuntu-24.04") is True
    assert keep.is_registered() is True
    assert keep.unregister() is True
    assert keep.is_registered() is False


def test_a_registry_failure_is_reported_not_swallowed():
    class Refusing(Registry):
        def set(self, name, command):
            return False

    logs: list[str] = []
    assert keeper(registry=Refusing(), logs=logs).register("Ubuntu-24.04") is False
    assert any("could not register" in line for line in logs)


# ── the supervisor ─────────────────────────────────────────────────────────


def test_the_supervisor_query_filters_by_process_name():
    """Without the filter, the PowerShell running the query matches itself —
    so a supervisor always looks present and none is ever started."""
    runner = FakeRunner()
    keeper(runner).supervisor_running()
    script = " ".join(runner.calls[0])
    assert "pythonw.exe" in script
    assert "Win32_Process" in script


def test_the_supervisor_is_found_by_command_line_not_a_pid_file():
    """A pid file goes stale across a crash and reports a dead supervisor."""
    runner = FakeRunner().out("Win32_Process", "4242\n")
    assert keeper(runner).supervisor_running() is True


def test_starting_the_supervisor_detaches_it():
    runner = FakeRunner().out("Win32_Process", "")
    assert keeper(runner).start_supervisor("Ubuntu-24.04") is True
    assert runner.spawned, "the supervisor must outlive the bosun run"


def test_an_already_running_supervisor_is_not_started_twice():
    runner = FakeRunner().out("Win32_Process", "4242\n")
    assert keeper(runner).start_supervisor("Ubuntu-24.04") is True
    assert not runner.spawned


def test_the_supervisor_probe_is_read_only():
    runner = FakeRunner()
    keeper(runner).supervisor_running()
    assert runner.marked_read_only("Win32_Process")


# ── enable / disable / describe ────────────────────────────────────────────


def test_enable_warns_when_dbus_launch_is_missing():
    """Without it the endpoint dies a minute later, so saying nothing is worse."""
    runner = healthy_machine().on("command -v dbus-launch", Result(1))
    logs: list[str] = []
    keeper(runner, logs=logs).enable(Wsl(runner, "Ubuntu-24.04"), "Ubuntu-24.04")
    assert any("dbus-launch is missing" in line for line in logs)
    assert not runner.ran("dbus-launch true")


def test_disable_stops_the_supervisor_before_releasing():
    """The other order lets the supervisor re-arm within the poll interval,
    so the command looks like it did nothing."""
    runner = FakeRunner().out("Win32_Process", "4242\n")
    keep = keeper(runner, registry=Registry({"bosun-keepalive": "x"}))
    keep.disable(Wsl(runner, "Ubuntu-24.04"), "Ubuntu-24.04")

    order = runner.displays
    stop = next(i for i, d in enumerate(order) if "Stop-Process" in d)
    release = next(i for i, d in enumerate(order) if "pkill" in d)
    assert stop < release


def test_describe_reports_each_part_separately():
    runner = FakeRunner().out("Win32_Process", "")
    text = keeper(runner, registry=Registry()).describe(Wsl(runner, "Ubuntu-24.04"), True)
    assert "NOT supervised" in text
    assert "NOT re-armed at logon" in text


def test_describe_notices_a_stopped_distro():
    runner = FakeRunner()
    assert "distro stopped" in keeper(runner).describe(Wsl(runner, "Ubuntu-24.04"), False)


# ── wiring ─────────────────────────────────────────────────────────────────


def test_up_enables_the_keepalive(monkeypatch):
    monkeypatch.setattr("bosun.client.have", lambda _n: True)
    runner = healthy_machine()
    flows.up(runner, resolve(), lambda _: None, prompt=False)
    assert runner.ran("dbus-launch"), "up must leave the endpoint holding"


def test_keepalive_can_be_switched_off_in_config(monkeypatch):
    monkeypatch.setattr("bosun.client.have", lambda _n: True)
    runner = healthy_machine()
    cfg = resolve(overrides={"keepalive": {"enabled": False}})
    flows.up(runner, cfg, lambda _: None, prompt=False)
    assert not runner.ran("dbus-launch")


def test_the_keepalive_flow_reports_status():
    runner = healthy_machine()
    logs: list[str] = []
    assert flows.keepalive(runner, resolve(), logs.append, "status") == 0
    assert any("keep-alive:" in line for line in logs)


def test_the_keepalive_flow_needs_a_distro():
    runner = FakeRunner().out("wsl.exe -l -q", "")
    logs: list[str] = []
    assert flows.keepalive(runner, resolve(), logs.append, "status") == 1


def test_the_log_is_rotated_rather_than_growing(tmp_path, monkeypatch):
    log = tmp_path / "keepalive.log"
    log.write_text("x" * (65 * 1024), encoding="utf-8")
    monkeypatch.setattr("bosun.keepalive.log_path", lambda: log)

    keeper().note("after rotation")

    assert log.read_text(encoding="utf-8").count("after rotation") == 1
    assert log.stat().st_size < 1024


def test_a_log_that_cannot_be_written_does_not_stop_the_supervisor(monkeypatch):
    monkeypatch.setattr(
        "bosun.keepalive.log_path", lambda: __import__("pathlib").Path("/nope/nope/x.log")
    )
    keeper().note("this must not raise")
