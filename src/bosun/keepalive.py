"""Holding the distro open, so the engine stays reachable from Windows.

WSL shuts an instance down roughly a minute after it goes idle, and the
localhost forward to the engine's port dies with it. So ``docker`` from a
Windows shell works right after ``bosun up`` and then stops working, while
every diagnostic you run to investigate wakes the distro and reports it
healthy.

Current WSL has a setting for exactly this: ``[general] instanceIdleTimeout``
in ``%USERPROFILE%\\.wslconfig`` (milliseconds; ``-1`` never idles out). It
governs the *instance*, unlike ``[wsl2] vmIdleTimeout`` below. Measured on WSL
3.0.5 with the dbus keep-alive switched off, sampling every 30 s:

=============================================================  ==============
no setting                                                      stopped < 40 s
``[general] instanceIdleTimeout=-1``                            up at 246 s
=============================================================  ==============

So on a WSL that has it, bosun writes that one key and runs nothing else: no
supervisor, no logon item, no dbus. It is read when the VM starts, so it takes
effect after ``wsl --shutdown`` — ``bosun up`` writes it before its own WSL
restart for that reason.

Everything below is the fallback for older WSL, where there was no setting.
What was measured there, rather than assumed:

=============================================================  ==============
``.wslconfig`` ``[experimental] vmIdleTimeout``                 unknown key
``.wslconfig`` ``[wsl2] vmIdleTimeout=-1`` / a large value      no effect
a systemd unit running ``sleep infinity`` inside the distro     no effect
``wsl.conf`` ``[boot] command=dbus-launch true``                no effect
``wsl.exe --exec dbus-launch true`` from Windows                **works**
=============================================================  ==============

``vmIdleTimeout`` cannot help by construction: it governs the *virtual
machine*, whose timer only starts once every *instance* has already terminated.
The instance timeout is separate and not configurable.

The pattern behind those results is that WSL counts only processes created by a
Windows-side ``wsl.exe`` invocation; anything spawned inside the instance, by
systemd or by a boot command, is ignored. That is the regression tracked as
microsoft/WSL#13416 — fine on 2.5.10, broken from 2.6.1 on.

``dbus-launch`` is what this module uses because it is the one command that both
satisfies WSL's accounting *and* returns immediately: it forks a session bus and
exits, so nothing waits on the Windows side. A detached ``setsid sleep
infinity`` does **not** work, so this is not simply "leave any process running"
— do not swap it for something that looks equivalent without re-testing the idle
window.

Arming it once at logon is not enough either, for two reasons:

* A logon item can fire before the machine can actually start a distro. Network
  stacks and virtualisation services are still coming up, and a one-shot that
  fires into that has nothing left to try again with.
* A launcher the shell has to interpret — a ``.vbs`` or ``.cmd`` — may be
  unavailable, and when a hidden launcher fails it has nowhere to report it. So
  the logon entry runs an interpreter directly rather than a script file.

Hence a supervisor that retries until something answers, keeps watching
afterwards, and writes what it did to a log, so the next failure is readable
instead of invisible.
"""

from __future__ import annotations

import os
import pathlib
import re
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from . import wslconf
from .exec import Runner, Wsl, command_line

# The .wslconfig key that keeps an idle instance running. See the module doc.
IDLE_SECTION = "general"
IDLE_KEY = "instanceIdleTimeout"
IDLE_NEVER = "-1"

# The oldest WSL the setting has been verified on. Set from a measurement, not
# from release notes, so it may well be lower — but a WSL that does not know
# the key ignores it silently and the instance idles out, which is the exact
# failure this module exists to prevent. Lower it only after re-measuring.
IDLE_SETTING_MIN = (3, 0, 5)


def wsl_version(runner: Runner) -> tuple[int, ...] | None:
    """The installed WSL version, or None when it cannot be determined.

    The first dotted number in ``wsl --version`` is WSL's own; the labels are
    localised, so the number is matched rather than the text before it. Builds
    old enough to lack ``--version`` print usage instead, which has no version
    number in it and so correctly comes back as None.
    """
    res = runner.run(["wsl.exe", "--version"], timeout=30, read_only=True)
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", res.out) if res.ok else None
    return tuple(int(part) for part in match.groups()) if match else None


def idle_setting_supported(runner: Runner) -> bool:
    version = wsl_version(runner)
    return version is not None and version >= IDLE_SETTING_MIN


def wslconfig_path(home: pathlib.Path | None = None) -> pathlib.Path:
    return (home or pathlib.Path.home()) / ".wslconfig"


def idle_setting_present(home: pathlib.Path | None = None) -> bool:
    path = wslconfig_path(home)
    text = path.read_text(encoding="utf-8-sig") if path.is_file() else ""
    return wslconf.get(text, IDLE_SECTION, IDLE_KEY) == IDLE_NEVER


def write_idle_setting(
    log: Callable[[str], None], home: pathlib.Path | None = None, *, enabled: bool = True
) -> bool:
    """Set (or remove) the idle timeout in .wslconfig. Returns True when changed.

    Edited in place with the same section-preserving editor as wsl.conf: this
    file also carries memory, networking and DNS settings bosun must not touch.
    """
    path = wslconfig_path(home)
    current = path.read_text(encoding="utf-8-sig") if path.is_file() else ""
    if enabled:
        wanted = wslconf.set_key(current, IDLE_SECTION, IDLE_KEY, IDLE_NEVER)
    else:
        wanted = wslconf.remove_key(current, IDLE_SECTION, IDLE_KEY)
    if wanted == current or (not current and not enabled):
        return False
    path.write_text(wanted, encoding="utf-8")
    verb = "set" if enabled else "removed"
    log(f"{verb} [{IDLE_SECTION}] {IDLE_KEY}={IDLE_NEVER} in {path}")
    return True


# dbus-launch lives here, and a minimal image does not always ship it.
KEEPALIVE_PACKAGE = "dbus-x11"

# The session bus dbus-launch forks, told apart from the system bus systemd
# always runs (which is `--nofork`, and does not contain `--fork`).
#
# The brackets are load-bearing. `pgrep -f` matches against every command line
# including the shell running the pgrep, whose arguments contain this very
# pattern — so an unbracketed marker matches itself and reports a keep-alive
# that is not there, silently skipping the arming. `--[f]ork` is the same regex
# but is not itself matched by it. `pkill` in disarm() relies on the same
# property, or it would kill the shell instead of the bus.
KEEPALIVE_MARKER = "dbus-daemon.*--[f]ork"

LAUNCHER_NAME = "bosun-keepalive"
LOG_NAME = f"{LAUNCHER_NAME}.log"

# Per-user, needs no administrator rights, and Task Manager lists it under
# Startup apps — so this tool is not the only way to see or stop it. A
# scheduled task was the alternative: `schtasks /SC ONLOGON` needs
# administrator rights even for a task scoped to the current user.
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

# The internal mode the logon entry runs. Also how a running supervisor is
# recognised: matching the command line beats a pid file, which goes stale
# across a crash and reports a supervisor that died hours ago.
SUPERVISE_FLAGS = ("keepalive", "--supervise")

# Seconds. The first two are sized for a slow logon — minutes can pass before
# wsl.exe can start anything — so give up only well past that.
#
# The third matches WSL's own idle window. Once armed the bus holds on its own,
# so this poll only matters after an explicit `wsl --shutdown` or a resume that
# dropped the instance — and then it decides how long `docker ps` keeps failing.
ARM_EVERY = 15
ARM_ATTEMPTS = 40
POLL_EVERY = 60

# Exit code meaning "nothing was holding the distro, so I started a bus". Any
# value outside sh's own range works; 10 is far from the 1/2/126/127 a shell
# produces on its own.
ARMED_NOW = 10


class LogonRegistry(Protocol):
    """The per-user logon entries. A seam, so this module imports anywhere.

    ``winreg`` exists only on Windows, and bosun's tests run on Linux.
    """

    def get(self, name: str) -> str | None: ...

    def set(self, name: str, command: str) -> bool: ...

    def delete(self, name: str) -> bool: ...


@dataclass
class WindowsRunKey:
    """The real thing: HKCU\\...\\Run."""

    key: str = RUN_KEY

    def _winreg(self):
        import winreg

        return winreg

    def get(self, name: str) -> str | None:
        try:
            winreg = self._winreg()
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.key) as handle:
                value, _ = winreg.QueryValueEx(handle, name)
                return str(value)
        except (OSError, ImportError):
            return None

    def set(self, name: str, command: str) -> bool:
        try:
            winreg = self._winreg()
            with winreg.CreateKeyEx(
                winreg.HKEY_CURRENT_USER, self.key, 0, winreg.KEY_SET_VALUE
            ) as handle:
                winreg.SetValueEx(handle, name, 0, winreg.REG_SZ, command)
        except (OSError, ImportError):
            return False
        return True

    def delete(self, name: str) -> bool:
        try:
            winreg = self._winreg()
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, self.key, 0, winreg.KEY_SET_VALUE
            ) as handle:
                winreg.DeleteValue(handle, name)
        except (OSError, ImportError):
            return False
        return True


def log_path() -> pathlib.Path:
    local = os.environ.get("LOCALAPPDATA") or str(pathlib.Path.home() / "AppData" / "Local")
    return pathlib.Path(local) / LOG_NAME


@dataclass
class KeepAlive:
    """Holds the distro open, now and after every logon."""

    runner: Runner
    log: Callable[[str], None]
    registry: LogonRegistry | None = None
    # Where .wslconfig lives; None is the real %USERPROFILE%. A seam for tests.
    home: pathlib.Path | None = None

    def __post_init__(self) -> None:
        if self.registry is None:
            self.registry = WindowsRunKey()
        self._uses_setting: bool | None = None

    @property
    def uses_setting(self) -> bool:
        """True when this WSL keeps the instance up from .wslconfig alone."""
        if self._uses_setting is None:
            self._uses_setting = idle_setting_supported(self.runner)
        return self._uses_setting

    # ── arming ────────────────────────────────────────────────────────────
    @staticmethod
    def arm_script() -> str:
        """Check and arm in one shot, so repeats do not stack session buses.

        Running dbus-launch unconditionally would fork a fresh bus on every poll
        and leak one process per interval, forever.

        The distinct exit code is what makes the log worth reading: without it,
        "already held" and "the distro was down and I just restarted it" look
        identical — and when the endpoint went away is the one question the log
        exists to answer.
        """
        return (
            f"pgrep -f '{KEEPALIVE_MARKER}' >/dev/null "
            f"|| {{ dbus-launch true && exit {ARMED_NOW}; exit 1; }}"
        )

    def arm_command(self, distro: str) -> list[str]:
        # --exec, not `bash -lc`: the point is a Windows-side invocation, and
        # --exec skips the login shell that would add nothing here.
        return ["wsl.exe", "-d", distro, "--exec", "bash", "-c", self.arm_script()]

    def is_armed(self, wsl: Wsl) -> bool:
        """True when a session bus is already holding this instance open."""
        return wsl.ok(f"pgrep -f '{KEEPALIVE_MARKER}' >/dev/null", timeout=30)

    def _try_arm(self, distro: str):
        return self.runner.run(self.arm_command(distro), timeout=180)

    def arm(self, distro: str) -> bool:
        """Start the distro if needed and leave a session bus holding it."""
        result = self._try_arm(distro)
        if result.returncode not in (0, ARMED_NOW):
            self.log(
                f"warning: could not arm the keep-alive: {result.stderr.strip() or result.returncode}"
            )
            return False
        return True

    def disarm(self, distro: str) -> bool:
        """Let go of the distro, so WSL idles it out within about a minute.

        Deliberately does not stop the distro: something else may be using it,
        and turning the keep-alive off only means "stop holding this open".
        """
        return self.runner.run(
            ["wsl.exe", "-d", distro, "--exec", "bash", "-c", f"pkill -f '{KEEPALIVE_MARKER}'"],
            timeout=60,
        ).ok

    # ── the logon entry ───────────────────────────────────────────────────
    def supervisor_command(self, distro: str) -> list[str]:
        """The interpreter directly, not a script file.

        pythonw.exe when it exists, so the supervisor owns no console window.
        Not a .vbs or .cmd: a launcher the shell has to interpret may be
        unavailable, and a hidden launcher that fails has nowhere to say so.
        """
        interpreter = pathlib.Path(sys.executable)
        pythonw = interpreter.with_name("pythonw.exe")
        return [
            str(pythonw if pythonw.exists() else interpreter),
            "-m",
            "bosun",
            *SUPERVISE_FLAGS,
            "--distro",
            distro,
        ]

    def is_registered(self) -> bool:
        return self.registry.get(LAUNCHER_NAME) is not None

    def register(self, distro: str) -> bool:
        if not self.registry.set(LAUNCHER_NAME, command_line(self.supervisor_command(distro))):
            self.log("warning: could not register the logon item")
            return False
        self.log(f"logon item registered: {LAUNCHER_NAME}")
        return True

    def unregister(self) -> bool:
        return self.registry.delete(LAUNCHER_NAME)

    # ── the supervisor process ────────────────────────────────────────────
    def _process_query(self, tail: str) -> str:
        """Find the running supervisor by command line, via PowerShell CIM.

        Matched on its arguments rather than a pid file: a pid file goes stale
        across a crash or a reboot and would report a supervisor that stopped
        running hours ago.

        The name filter is not an optimisation. Without it the PowerShell
        process running this very query matches itself — the pattern is right
        there in its own command line — so the answer would always be "yes, a
        supervisor is running" and nothing would ever start one.
        """
        pattern = "*" + "*".join(SUPERVISE_FLAGS) + "*"
        script = (
            "Get-CimInstance Win32_Process -Filter "
            "\"Name='pythonw.exe' OR Name='python.exe'\" | "
            f"Where-Object {{ $_.CommandLine -like '{pattern}' }} | {tail}"
        )
        result = self.runner.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            timeout=60,
            read_only=True,
        )
        return result.out if result.ok else ""

    def supervisor_running(self) -> bool:
        return bool(self._process_query("Select-Object -First 1 -ExpandProperty ProcessId"))

    def start_supervisor(self, distro: str) -> bool:
        """Run the supervisor now, so setup does not require a logon to take effect."""
        if self.supervisor_running():
            return True
        return self.runner.spawn(self.supervisor_command(distro))

    def stop_supervisor(self) -> bool:
        if not self.supervisor_running():
            return False
        self._process_query("ForEach-Object { Stop-Process -Id $_.ProcessId -Force }")
        return True

    # ── the loop ──────────────────────────────────────────────────────────
    def note(self, message: str) -> None:
        """Append one line to the log, rotating it rather than letting it grow."""
        path = log_path()
        try:
            if path.exists() and path.stat().st_size > 64 * 1024:
                path.unlink()
            with path.open("a", encoding="utf-8") as handle:
                handle.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {message}\n")
        except OSError:
            pass  # a supervisor that cannot log must still keep supervising

    def supervise(self, distro: str) -> int:
        """The logon entry point: arm, retry while it fails, then keep watch.

        Runs with no console and nobody reading stdout, so every observation
        goes to the log file.
        """
        self.note(f"supervisor started for {distro}")
        armed = False
        attempts = 0
        while True:
            result = self._try_arm(distro)
            if result.returncode in (0, ARMED_NOW):
                if result.returncode == ARMED_NOW:
                    self.note("distro was not being held; armed a session bus")
                elif not armed:
                    self.note("keep-alive already armed")
                armed, attempts = True, 0
                time.sleep(POLL_EVERY)
                continue

            if armed:
                self.note("keep-alive lost; re-arming")
            armed = False
            attempts += 1
            if attempts > ARM_ATTEMPTS:
                self.note(f"gave up after {attempts} attempts; run 'bosun status'")
                return 1
            time.sleep(ARM_EVERY)

    # ── orchestration ─────────────────────────────────────────────────────
    def _retire_legacy(self, wsl: Wsl | None, distro: str | None) -> None:
        """Remove the dbus keep-alive a pre-setting bosun left behind.

        Harmless to leave running, but it is a startup item and a background
        process doing nothing useful once .wslconfig holds the instance.
        """
        if self.stop_supervisor():
            self.log("stopped the old keep-alive supervisor")
        if self.unregister():
            self.log(f"removed the old logon item: {LAUNCHER_NAME}")
        if wsl is not None and distro is not None and self.is_armed(wsl):
            self.disarm(distro)

    def enable(self, wsl: Wsl, distro: str) -> bool:
        """Hold the distro open now, and again after every logon.

        Returns True when .wslconfig changed, which only takes effect after the
        WSL VM restarts — the caller decides whether to do that.
        """
        if self.uses_setting:
            changed = write_idle_setting(self.log, self.home)
            self._retire_legacy(wsl, distro)
            if changed:
                self.log("keep-alive takes effect after 'wsl --shutdown'")
            else:
                self.log(f"keep-alive: {IDLE_KEY}={IDLE_NEVER} already set in .wslconfig")
            return changed

        self._enable_legacy(wsl, distro)
        return False

    def _enable_legacy(self, wsl: Wsl, distro: str) -> None:
        if not wsl.ok("command -v dbus-launch", user="root", timeout=20):
            self.log(
                f"warning: dbus-launch is missing; install {KEEPALIVE_PACKAGE} in the distro, "
                "or the endpoint will stop answering about a minute after this run."
            )
            return

        if self.is_armed(wsl):
            self.log("keep-alive already running")
        elif self.arm(distro):
            self.log("keep-alive armed")

        self.register(distro)
        if self.start_supervisor(distro):
            self.log(f"supervisor running; log: {log_path()}")

    def disable(self, wsl: Wsl | None, distro: str | None) -> None:
        """Stop holding the distro open, now and at logon.

        Order matters: stop the supervisor before letting go of the bus, or it
        re-arms within the poll interval and the command looks like it did
        nothing.
        """
        if self.uses_setting and write_idle_setting(self.log, self.home, enabled=False):
            self.log("WSL will idle the distro out again after the next 'wsl --shutdown'")
        if self.stop_supervisor():
            self.log("supervisor stopped")
        if self.unregister():
            self.log("logon item removed")
        if wsl is not None and distro is not None and self.is_armed(wsl):
            self.disarm(distro)
            self.log("released the distro; WSL will idle it out in about a minute")

    def describe(self, wsl: Wsl | None, running: bool) -> str:
        """Report what is actually true, not what was attempted.

        Registering the logon item can fail, and a summary that claims
        otherwise sends you hunting for the wrong problem later.
        """
        if self.uses_setting:
            if idle_setting_present(self.home):
                return f"held by .wslconfig ([{IDLE_SECTION}] {IDLE_KEY}={IDLE_NEVER})"
            return f"NOT held - [{IDLE_SECTION}] {IDLE_KEY} is not set in .wslconfig"

        if wsl is None:
            held = "no distro"
        elif not running:
            held = "distro stopped"
        else:
            held = "armed" if self.is_armed(wsl) else "NOT armed"
        supervisor = "supervised" if self.supervisor_running() else "NOT supervised"
        at_logon = "re-armed at logon" if self.is_registered() else "NOT re-armed at logon"
        return f"{held}, {supervisor}, {at_logon}"
