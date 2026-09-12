"""Process adapters — the only module in bosun that imports ``subprocess``.

Everything above this layer takes a :class:`Runner` as an argument, so the
decision logic runs under pytest against :class:`~tests.fakes.FakeRunner` with
no Windows host, no WSL and no container engine present. That is the same
split deputy uses for the GitHub API: pure logic in the middle, one thin
injectable adapter at the edge.

Three runners ship here:

``SubprocessRunner``  the real one.
``DryRunRunner``      logs what would run, executes only reads (``--dry-run``).
``Wsl``               not a Runner itself — a thin wrapper that turns shell
                      snippets into ``wsl.exe`` invocations against one distro.
"""

from __future__ import annotations

import itertools
import os
import shutil
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol

# Names the scratch files write_file streams through. A counter rather than a
# timestamp: two writes inside the same millisecond would otherwise collide and
# the second would install the first one's content.
_tmp_counter = itertools.count()

# wsl.exe writes its distro listings as UTF-16-LE, unlike every other stream it
# produces. Callers that parse `-l` output must pass this explicitly.
WSL_LIST_ENCODING = "utf-16-le"


class BosunError(RuntimeError):
    """A step failed in a way bosun cannot work around."""


@dataclass(frozen=True)
class Result:
    """The outcome of one process invocation."""

    returncode: int
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def out(self) -> str:
        """stdout, stripped — the common case when probing for a value."""
        return (self.stdout or "").strip()


class Runner(Protocol):
    """Runs a command and returns its :class:`Result`. Never raises on non-zero."""

    def run(
        self,
        cmd: Sequence[str],
        *,
        stdin: str | None = None,
        timeout: int | None = None,
        encoding: str | None = None,
        capture: bool = True,
    ) -> Result: ...


@dataclass
class SubprocessRunner:
    """The real adapter. Echoes each command when ``verbose``."""

    verbose: bool = False

    def run(
        self,
        cmd: Sequence[str],
        *,
        stdin: str | None = None,
        timeout: int | None = None,
        encoding: str | None = None,
        capture: bool = True,
    ) -> Result:
        if self.verbose:
            print(f"  $ {' '.join(cmd)}", file=sys.stderr)
        try:
            cp = subprocess.run(
                list(cmd),
                input=stdin,
                capture_output=capture,
                text=True,
                encoding=encoding,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return Result(124, "", f"timed out after {timeout}s: {' '.join(cmd)}")
        except FileNotFoundError as exc:
            return Result(127, "", str(exc))
        return Result(cp.returncode, cp.stdout or "", cp.stderr or "")


# Commands that only observe. Under --dry-run these still execute, so that a
# dry run reports on the machine's actual state rather than on a guess.
READONLY_HINTS = (
    "-l",
    "--list",
    "info",
    "version",
    "ls",
    "cat",
    "test",
    "id",
    "groups",
    "whoami",
    "command",
    "grep",
    "getent",
    "ps",
)


@dataclass
class DryRunRunner:
    """Wraps a real runner: executes reads, logs and skips everything else."""

    inner: Runner
    skipped: list[list[str]] = field(default_factory=list)

    def run(
        self,
        cmd: Sequence[str],
        *,
        stdin: str | None = None,
        timeout: int | None = None,
        encoding: str | None = None,
        capture: bool = True,
    ) -> Result:
        if self._is_read(cmd):
            return self.inner.run(
                cmd, stdin=stdin, timeout=timeout, encoding=encoding, capture=capture
            )
        self.skipped.append(list(cmd))
        print(f"  [dry-run] {' '.join(cmd)}")
        return Result(0, "", "")

    # Redirections that discard output. Stripped before the write check, because
    # `probe >/dev/null` is a probe, not a write — treating the `>` as evidence
    # of mutation made --dry-run skip pure state queries and answer them with a
    # synthetic success, which is how a dry run ends up describing a machine
    # that does not exist.
    _DISCARDS = (">/dev/null", "> /dev/null", "2>/dev/null", "2> /dev/null", "2>&1")

    @classmethod
    def _is_read(cls, cmd: Sequence[str]) -> bool:
        joined = " ".join(cmd)
        for discard in cls._DISCARDS:
            joined = joined.replace(discard, " ")
        # A shell snippet that redirects, installs or removes is a write even if
        # it mentions a read-only verb somewhere in the pipeline.
        if any(tok in joined for tok in (">", "install", "rm ", "tee ", "apt-get", "usermod")):
            return False
        return any(tok in cmd or tok in joined for tok in READONLY_HINTS)


def have(name: str) -> bool:
    """True when ``name`` resolves on the host PATH."""
    return shutil.which(name) is not None


@dataclass
class Wsl:
    """Runs shell snippets inside one WSL distro.

    ``distro`` may be ``None``, in which case wsl.exe picks the default distro —
    which is what the very first probes have to do, before bosun knows which
    distro it is targeting.
    """

    runner: Runner
    distro: str | None = None

    def _base(self, user: str | None) -> list[str]:
        base = ["wsl.exe"]
        if self.distro:
            base += ["-d", self.distro]
        if user:
            base += ["-u", user]
        return base

    def sh(
        self,
        script: str,
        *,
        user: str | None = None,
        timeout: int | None = 60,
        stdin: str | None = None,
    ) -> Result:
        """Run ``script`` through ``bash -lc`` inside the distro."""
        return self.runner.run(
            [*self._base(user), "--", "bash", "-lc", script], stdin=stdin, timeout=timeout
        )

    def ok(self, script: str, *, user: str | None = None, timeout: int | None = 30) -> bool:
        """True when ``script`` exits zero — for probes."""
        return self.sh(script, user=user, timeout=timeout).ok

    def write_file(
        self,
        path: str,
        content: str,
        *,
        mode: str = "0644",
        owner: str = "root:root",
        timeout: int = 120,
    ) -> Result:
        """Write text into the distro by streaming it over stdin.

        Deliberately avoids heredocs, base64 and mktemp: the content never goes
        through a shell-quoting round trip, so certificates, JSON and sudoers
        entries all survive verbatim regardless of what characters they contain.
        """
        owner_user, _, owner_group = owner.partition(":")
        owner_group = owner_group or "root"
        tmp = f"/tmp/bosun_{os.getpid()}_{next(_tmp_counter)}"
        script = (
            'export PATH="/usr/sbin:/usr/bin:/sbin:/bin:$PATH"; '
            f'cat > "{tmp}"; '
            f'install -o {owner_user} -g {owner_group} -m {mode} "{tmp}" "{path}"; '
            f'rm -f "{tmp}"'
        )
        return self.runner.run(
            [*self._base("root"), "--", "bash", "-lc", script], stdin=content, timeout=timeout
        )

    def read_file(self, path: str, *, timeout: int = 30) -> str:
        """Return the contents of ``path``, or "" when it does not exist.

        Reads as root so that files mode 0600 (private keys, sudoers) are
        readable without a permission dance.
        """
        res = self.runner.run([*self._base("root"), "--", "cat", path], timeout=timeout)
        return res.stdout if res.ok else ""

    def shutdown(self, *, timeout: int = 30) -> Result:
        return self.runner.run(["wsl.exe", "--shutdown"], timeout=timeout)

    def terminate(self, *, timeout: int = 30) -> Result:
        if not self.distro:
            return Result(0)
        return self.runner.run(["wsl.exe", "--terminate", self.distro], timeout=timeout)
