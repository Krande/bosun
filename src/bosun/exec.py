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

import base64
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


# The byte that gives UTF-16 away, spelled without an escape so this file
# contains no literal NUL of its own.
NUL = bytes([0])

# wsl.exe writes its own messages — distro listings, and every error it raises


# before handing off to the distro — as UTF-16LE, while the commands run inside


# the distro write UTF-8. Kept as a hint for callers that know which they are


# getting; decode_output() sniffs regardless, so being wrong is survivable.


WSL_LIST_ENCODING = "utf-16-le"


def decode_output(raw: bytes | None) -> str:
    """Decode child output, detecting the encoding rather than being told it.

    Hard-coding one encoding breaks the other half of the calls: wsl.exe writes
    its own messages as UTF-16LE, while anything run inside the distro writes
    UTF-8. Decoding UTF-16 as a byte encoding yields text with a NUL between
    every character, which is what every wsl.exe error message looked like
    before this existed - "There is no distribution with the supplied name"
    arrived unreadable, exactly when it was needed.

    Detected rather than passed in, because an encoding supplied by hand is one
    that can be supplied wrongly: UTF-16 decoding of arbitrary even-length bytes
    succeeds and produces garbage instead of raising, so a wrong hint would win
    silently. Interleaved NULs in the first bytes are the reliable giveaway, and
    wsl.exe's messages are ASCII text, so they always carry them.
    """
    if not raw:
        return ""
    if NUL in raw[:100]:
        try:
            return raw.decode("utf-16-le").replace(NUL.decode(), "")
        except UnicodeDecodeError:
            pass
    for encoding in ("utf-8", "mbcs", "latin-1"):
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


class BosunError(RuntimeError):
    """A step failed in a way bosun cannot work around."""


@dataclass(frozen=True)
class Result:
    """The outcome of one process invocation."""

    returncode: int

    stdout: str = ""

    stderr: str = ""

    timed_out: bool = False

    @property
    def ok(self) -> bool:

        return self.returncode == 0 and not self.timed_out

    @property
    def out(self) -> str:
        """stdout, stripped — the common case when probing for a value."""

        return (self.stdout or "").strip()

    @property
    def combined(self) -> str:
        """Both streams, for error messages.





        wsl.exe writes its own failures to stdout rather than stderr, so an


        error message built from stderr alone is routinely empty exactly when


        it is needed.


        """

        return f"{self.stdout}\n{self.stderr}".strip()


class Runner(Protocol):
    """Runs a command and returns its :class:`Result`. Never raises on non-zero.





    ``read_only`` declares that a command observes without changing anything.


    It defaults to False, so a command is assumed to mutate unless its caller


    says otherwise — see :class:`DryRunRunner` for why that default matters.


    """

    def run(
        self,
        cmd: Sequence[str],
        *,
        stdin: str | None = None,
        timeout: int | None = None,
        capture: bool = True,
        read_only: bool = False,
        display: str | None = None,
    ) -> Result: ...

    def spawn(self, cmd: Sequence[str]) -> bool:
        """Start a detached background process. True when it was launched.





        Distinct from :meth:`run` because nothing waits for it and there is no


        output to collect — the keep-alive supervisor outlives the bosun run


        that started it.


        """

        ...


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
        capture: bool = True,
        read_only: bool = False,
        display: str | None = None,
    ) -> Result:

        if self.verbose:
            print(f"  $ {display or ' '.join(cmd)}", file=sys.stderr)

        payload = stdin.encode("utf-8") if stdin is not None else None

        try:
            cp = subprocess.run(
                list(cmd),
                input=payload,
                # Closed stdin when nothing is being fed in. wsl.exe asks the
                # user to press a key in some states; with stdin inherited that
                # blocks until the timeout fires, so a prompt nobody can see
                # becomes a multi-second stall. Closed stdin fails it fast.
                stdin=None if payload is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE if capture else None,
                stderr=subprocess.PIPE if capture else None,
                timeout=timeout,
                check=False,
                # Output is piped, so no window is wanted — and the keep-alive
                # supervisor runs under pythonw.exe with no console to inherit,
                # so without this Windows pops a fresh black one for every poll,
                # once a minute, forever.
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )

        except subprocess.TimeoutExpired as exc:
            return Result(
                124,
                decode_output(exc.stdout if isinstance(exc.stdout, bytes) else None),
                f"timed out after {timeout}s: {' '.join(cmd)}",
                timed_out=True,
            )

        except FileNotFoundError as exc:
            return Result(127, "", str(exc))

        return Result(
            cp.returncode,
            decode_output(cp.stdout),
            decode_output(cp.stderr),
        )

    def spawn(self, cmd: Sequence[str]) -> bool:
        """Start a detached background process.





        DETACHED_PROCESS so it survives this run and owns no console: the


        supervisor is meant to outlive `bosun up` without leaving a window


        open. The flags do not exist off Windows, hence the getattr.


        """

        if self.verbose:
            print(f"  $ (detached) {' '.join(cmd)}", file=sys.stderr)

        flags = (
            getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )

        try:
            subprocess.Popen(
                list(cmd),
                creationflags=flags,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
            )

        except OSError:
            return False

        return True


@dataclass
class DryRunRunner:
    """Wraps a real runner: executes declared reads, logs and skips everything else.





    Whether a command mutates is declared by its caller via ``read_only``, never


    inferred from the command string. An earlier version of this class did try


    to infer it, and the result was a ``--dry-run`` that changed the machine:





    ``bash -lc`` contains ``-l``, which was on the read-only hint list, so every


    single command sent into WSL matched "read" unless it happened to also


    contain one of a handful of write keywords. ``systemctl restart docker`` did


    not, so a dry run restarted the service. Neither did ``docker system prune


    -af``, so a dry run would have destroyed every image and container on the


    machine.





    Hence the default: a command with no explicit ``read_only=True`` is treated


    as a write and skipped. Getting that wrong now costs a missing line of dry


    run output, rather than an unwanted change to a live system.


    """

    inner: Runner

    skipped: list[list[str]] = field(default_factory=list)

    #: Marks this runner as a dry run for the few callers that write to the

    #: Windows filesystem directly rather than through a subprocess.

    dry_run: bool = True

    def run(
        self,
        cmd: Sequence[str],
        *,
        stdin: str | None = None,
        timeout: int | None = None,
        capture: bool = True,
        read_only: bool = False,
        display: str | None = None,
    ) -> Result:

        if read_only:
            return self.inner.run(
                cmd,
                stdin=stdin,
                timeout=timeout,
                capture=capture,
                read_only=True,
                display=display,
            )

        self.skipped.append(list(cmd))

        # The readable form, not the base64 envelope Wsl.sh wraps scripts in.

        print(f"  [dry-run] {display or ' '.join(cmd)}")

        return Result(0, "", "")

    def spawn(self, cmd: Sequence[str]) -> bool:

        self.skipped.append(list(cmd))

        print(f"  [dry-run] (detached) {' '.join(cmd)}")

        return True


def is_dry_run(runner: Runner) -> bool:
    """True when ``runner`` is a dry run.





    For the handful of places that touch the Windows filesystem directly — the


    TLS certificate export — where there is no subprocess for


    :class:`DryRunRunner` to intercept.


    """

    return getattr(runner, "dry_run", False)


def command_line(parts: Sequence[str]) -> str:
    """Render an argv list as one Windows command line.

    A ``Run`` registry value holds a *string*, so the argv used everywhere else
    has to be quoted back into one. subprocess.list2cmdline implements the rules
    Windows actually parses by — quoting on spaces alone is not enough, because
    a backslash before a quote, or a quote inside an argument, changes where the
    boundaries fall.
    """
    return subprocess.list2cmdline(list(parts))


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
        read_only: bool = False,
        display: str | None = None,
    ) -> Result:
        """Run ``script`` through bash inside the distro.





        The script is base64-encoded and decoded on the far side, because a


        script handed to ``wsl.exe`` as a command-line argument does not arrive


        intact. Windows rebuilds the command line from argv, wsl.exe re-parses


        it, and single quotes are consumed along the way — so bash receives


        ``awk {print $1}`` where the caller wrote ``awk '{print $1}'``, expands


        ``$1`` to nothing, and silently runs a different program. That is not a


        hypothetical: it made the systemd-unit probe report every unit missing,


        so ``bosun up`` concluded the engine needed reinstalling and would have


        purged a working docker.io to replace it with docker-ce.





        Base64 is alphanumeric plus ``+/=`` — no quotes, no ``$``, no spaces —


        so it survives every layer unchanged, and the far side reconstructs the


        script byte for byte.





        The exception is a script that needs stdin for data: the envelope uses


        the inner bash's stdin for the script itself, so those are passed


        directly and must be written without quoting that matters


        (see :meth:`write_file`).





        Pass ``read_only=True`` for a script that only observes, so that it


        still runs under ``--dry-run``. The default assumes a write.


        """

        if stdin is None:
            payload = base64.b64encode(script.encode("utf-8")).decode("ascii")

            inner = f"echo {payload} | base64 -d | bash -l"

        else:
            inner = script

        return self.runner.run(
            [*self._base(user), "--", "bash", "-lc", inner],
            stdin=stdin,
            timeout=timeout,
            read_only=read_only,
            display=display or script,
        )

    def ok(
        self,
        script: str,
        *,
        user: str | None = None,
        timeout: int | None = 30,
        read_only: bool = True,
    ) -> bool:
        """True when ``script`` exits zero.





        Read-only by default: every caller of this is a probe testing for a


        condition, and a probe that gets skipped under --dry-run would answer


        with a synthetic success and send the run down the wrong branch.


        """

        return self.sh(script, user=user, timeout=timeout, read_only=read_only).ok

    def write_file(
        self,
        path: str,
        content: str,
        *,
        mode: str = "0644",
        owner: str = "root:root",
        timeout: int = 120,
    ) -> Result:
        """Write text into the distro, carrying the content as base64.





        The content is encoded and decoded on the far side, so it never passes


        through a shell-quoting round trip: certificates, JSON and unit files


        survive verbatim regardless of the characters they contain. The script


        that does it then rides the same base64 envelope as every other script


        (see :meth:`sh`), so nothing here depends on quoting surviving Windows


        argv either.





        An earlier version streamed the content over stdin instead, which meant


        the script itself had to travel as a plain command-line argument. Trying


        to make that script quote-proof by dropping the quotes around ``$PATH``


        broke it outright: inside WSL, ``$PATH`` includes the Windows path, and


        an unquoted ``/mnt/c/Program Files (x86)/...`` is a bash syntax error.


        The write then failed silently and the caller carried on regardless.





        Absolute paths for the binaries, so nothing depends on PATH at all.


        """

        owner_user, _, owner_group = owner.partition(":")

        owner_group = owner_group or "root"

        tmp = f"/tmp/bosun_{os.getpid()}_{next(_tmp_counter)}"

        payload = base64.b64encode(content.encode("utf-8")).decode("ascii")

        script = (
            f"echo {payload} | /usr/bin/base64 -d > {tmp} && "
            f"/usr/bin/install -o {owner_user} -g {owner_group} -m {mode} {tmp} {path}; "
            f"rc=$?; /bin/rm -f {tmp}; exit $rc"
        )

        return self.sh(
            script, user="root", timeout=timeout, display=f"write {path} ({mode} {owner})"
        )

    def read_file(self, path: str, *, timeout: int = 30) -> str:
        """Return the contents of ``path``, or "" when it does not exist.





        Reads as root so that files mode 0600 (private keys, sudoers) are


        readable without a permission dance.


        """

        res = self.runner.run(
            [*self._base("root"), "--", "cat", path], timeout=timeout, read_only=True
        )

        return res.stdout if res.ok else ""

    def shutdown(self, *, timeout: int = 30) -> Result:

        return self.runner.run(["wsl.exe", "--shutdown"], timeout=timeout)

    def terminate(self, *, timeout: int = 30) -> Result:

        if not self.distro:
            return Result(0)

        return self.runner.run(["wsl.exe", "--terminate", self.distro], timeout=timeout)
