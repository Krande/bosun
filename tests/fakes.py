"""In-memory stand-ins for the process layer.

:class:`FakeRunner` satisfies the ``Runner`` protocol, so every module above
:mod:`bosun.exec` can be exercised with no WSL, no Windows and no container
engine. Rules are matched against the joined command line, most-recently-added
first, which lets a test state the general case once and then override one
specific probe.
"""

from __future__ import annotations

import base64
from collections.abc import Callable, Sequence

from bosun import daemonjson
from bosun.config import resolve
from bosun.engines import DOCKER
from bosun.exec import Result


class FakeRunner:
    """Records commands and answers them from a rule table."""

    def __init__(self, rules: list[tuple[str, Result]] | None = None) -> None:
        # (substring, result) — later entries win, so tests can layer overrides.
        self.rules: list[tuple[str, Result | Callable[[str], Result]]] = list(rules or [])
        self.calls: list[list[str]] = []
        self.stdins: list[str | None] = []
        # Records whether each call declared itself read-only, so tests can
        # assert that mutating commands are never marked as reads.
        self.read_flags: list[bool] = []
        # The human-readable form of each call (the script, not its envelope).
        self.displays: list[str] = []
        # Detached background launches, recorded separately from run() calls.
        self.spawned: list[list[str]] = []
        self.default = Result(0)

    def on(self, substring: str, result: Result | Callable[[str], Result]) -> FakeRunner:
        """Add a rule. Returns self so rules can be chained."""
        self.rules.append((substring, result))
        return self

    def fail(self, substring: str, stderr: str = "boom", code: int = 1) -> FakeRunner:
        return self.on(substring, Result(code, "", stderr))

    def out(self, substring: str, stdout: str) -> FakeRunner:
        return self.on(substring, Result(0, stdout))

    def run(
        self,
        cmd: Sequence[str],
        *,
        stdin: str | None = None,
        timeout: int | None = None,
        encoding: str | None = None,
        capture: bool = True,
        read_only: bool = False,
        display: str | None = None,
    ) -> Result:
        self.calls.append(list(cmd))
        self.stdins.append(stdin)
        self.read_flags.append(read_only)
        # Match and record against the readable script rather than the base64
        # envelope Wsl.sh wraps it in, so rules and assertions stay legible.
        joined = display or " ".join(cmd)
        self.displays.append(joined)
        for substring, result in reversed(self.rules):
            if substring in joined:
                return result(joined) if callable(result) else result
        return self.default

    def spawn(self, cmd: Sequence[str]) -> bool:
        self.calls.append(list(cmd))
        self.stdins.append(None)
        self.read_flags.append(False)
        self.displays.append("(detached) " + " ".join(cmd))
        self.spawned.append(list(cmd))
        return True

    # ── assertions used by the tests ───────────────────────────────────────
    def ran(self, substring: str) -> bool:
        return any(substring in d for d in self.displays)

    def count(self, substring: str) -> int:
        return sum(1 for d in self.displays if substring in d)

    def matching(self, substring: str) -> list[list[str]]:
        return [c for c, d in zip(self.calls, self.displays, strict=False) if substring in d]

    def decoded_script(self, index: int = -1) -> str:
        """The script a recorded call actually runs, past the base64 envelope."""
        token = self.calls[index][-1]
        if "base64 -d" not in token:
            return token
        payload = token.split("echo ", 1)[1].split(" |", 1)[0]
        return base64.b64decode(payload).decode()

    def written_content(self, index: int = -1) -> str:
        """The file content a recorded write_file call carries (doubly encoded)."""
        script = self.decoded_script(index)
        payload = script.split("echo ", 1)[1].split(" |", 1)[0]
        return base64.b64decode(payload).decode()

    def marked_read_only(self, substring: str) -> bool:
        """True when every call matching ``substring`` declared read_only."""
        flags = [
            flag
            for display, flag in zip(self.displays, self.read_flags, strict=False)
            if substring in display
        ]
        return bool(flags) and all(flags)


def healthy_machine(
    distro: str = "Ubuntu-24.04",
    user: str = "dev",
    *,
    engine: str = "docker",
    in_group: bool = True,
    systemd: bool = True,
) -> FakeRunner:
    """A runner scripted to look like a fully provisioned machine.

    Individual tests degrade it with ``.fail(...)`` / ``.out(...)`` to describe
    the one thing that is broken, rather than building a machine from scratch
    each time.
    """
    runner = FakeRunner()
    runner.out("wsl.exe -l -q", f"{distro}\n")
    runner.out("whoami", f"{user}\n")
    runner.out("ps -p 1 -o comm=", "systemd\n" if systemd else "init\n")
    runner.out(f"command -v {engine}", f"/usr/bin/{engine}\n")
    runner.out(f"systemctl is-active {engine}", "active\n")
    runner.out("list-unit-files", f"{engine}.service\n")
    # pgrep exits 1 when nothing matches: no apt run is in flight.
    runner.on("pgrep -f", Result(1))
    runner.on("grep -qx docker", Result(0 if in_group else 1))
    runner.out("context ls", "default\nbosun\n")
    runner.out(f"{engine} info", "Server Version: 27.0.0\n")
    runner.out("cat /etc/wsl.conf", "[boot]\nsystemd=true\n\n[user]\ndefault=dev\n")
    # A provisioned machine already has the daemon config bosun would write;
    # without these the "healthy" fixture would report itself as unconfigured.
    runner.out("cat /etc/docker/daemon.json", daemonjson.render("", resolve(), DOCKER))
    runner.out(
        "cat /etc/systemd/system/docker.service.d/bosun.conf",
        daemonjson.systemd_override(DOCKER),
    )
    return runner
