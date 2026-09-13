"""Finding, installing and preparing the WSL distro.

Privilege note — bosun does **not** create a temporary NOPASSWD sudoers
drop-in, and does not ask for a Linux password in order to provision.

``wsl.exe -u root`` already gives an unauthenticated root shell inside the
distro: the Windows user owns the distro image, so WSL does not pretend
otherwise. The pre-bosun script prompted for a sudo password, wrote a
``/etc/sudoers.d`` entry granting NOPASSWD on a list that included
``/usr/bin/bash`` (which is simply unrestricted root, spelled at length), and
removed it in a ``finally`` — leaving the machine permanently weakened if the
process was killed between the two. Running the privileged steps as root
directly is both simpler and strictly safer: nothing persists, nothing can leak,
and no secret is ever collected.

The only remaining password prompt is for setting a *new* Linux user's password,
which is for the human's later convenience and is never needed by bosun itself.
"""

from __future__ import annotations

import getpass
import time
from collections.abc import Callable

from . import wslconf
from .config import Config
from .exec import WSL_LIST_ENCODING, BosunError, Result, Runner, Wsl


def list_distros(runner: Runner) -> list[str]:
    """Registered WSL distro names.

    ``wsl.exe -l -q`` emits UTF-16-LE; decoding it as UTF-8 yields NUL-separated
    mojibake that looks like a single unparseable name, so the encoding is
    passed explicitly. Falls back to the verbose table when the quiet listing
    comes back empty, whose header is localised (English "NAME", Norwegian
    "NAVN", ...) and so is skipped by position rather than by matching text.
    """
    res = runner.run(
        ["wsl.exe", "-l", "-q"], timeout=20, encoding=WSL_LIST_ENCODING, read_only=True
    )
    names = [ln.strip() for ln in (res.stdout or "").splitlines() if ln.strip()]
    if names:
        return names

    res = runner.run(
        ["wsl.exe", "-l", "-v"], timeout=20, encoding=WSL_LIST_ENCODING, read_only=True
    )
    out: list[str] = []
    for i, line in enumerate([ln for ln in (res.stdout or "").splitlines() if ln.strip()]):
        if i == 0:  # header row, whatever language it is in
            continue
        s = line.strip().lstrip("*").strip()
        if s:
            out.append(s.split()[0])
    return out


def find(runner: Runner, cfg: Config) -> str | None:
    """The distro bosun should target, or None when none is registered.

    Prefers the configured name exactly; otherwise adopts the first registered
    distro whose name contains ``[distro].match``, so an existing Ubuntu-22.04
    is reused rather than a second distro being installed beside it.
    """
    names = list_distros(runner)
    if cfg.distro_name in names:
        return cfg.distro_name
    for name in names:
        if cfg.distro_match and cfg.distro_match in name.lower():
            return name
    return None


def is_launchable(runner: Runner, name: str, *, timeout: int = 25) -> bool:
    """True when a root shell can be started — i.e. the distro really is ready.

    Checked instead of trusting the listing: a distro can appear in ``wsl -l``
    while still unpacking, and every later step would fail confusingly.
    """
    return runner.run(
        ["wsl.exe", "-d", name, "-u", "root", "--", "true"], timeout=timeout, read_only=True
    ).ok


def install(runner: Runner, cfg: Config, log: Callable[[str], None]) -> None:
    """Install the configured distro, trying each available mechanism in turn.

    Three strategies, because which of them works varies between Windows
    installations in ways bosun cannot detect up front: the edition, whether the
    Store is reachable, and how the machine is provisioned all matter.
    """
    name = cfg.distro_name

    log(f"installing {name} (this can take several minutes)")
    res = runner.run(["wsl.exe", "--install", "-d", name], timeout=1800)
    if res.ok:
        return

    log(f"wsl --install failed ({res.stderr.strip() or res.returncode}); trying winget")
    if runner.run(["winget", "--version"], timeout=30, read_only=True).ok:
        pkg = _winget_id(name)
        # winget exits non-zero when the package is already installed, so its
        # exit code cannot distinguish success from failure here; the
        # launchability probe in ensure_ready is the real check.
        runner.run(
            [
                "winget",
                "install",
                "--exact",
                "--id",
                pkg,
                "--accept-package-agreements",
                "--accept-source-agreements",
            ],
            timeout=1800,
        )
        return

    raise BosunError(
        f"could not install {name}. Install it manually (Microsoft Store, or "
        f"'wsl --install -d {name}' from an elevated prompt) and re-run bosun up."
    )


def _winget_id(distro_name: str) -> str:
    """Map a WSL distro name to its winget package id."""
    digits = "".join(ch for ch in distro_name if ch.isdigit())
    return f"Canonical.Ubuntu.{digits}" if digits else "Canonical.Ubuntu"


def register(runner: Runner, name: str, log: Callable[[str], None]) -> None:
    """Run the distro's launcher once to unpack its filesystem.

    An installed-but-unregistered distro has no filesystem yet. ``install
    --root`` performs that first-run unpack without the interactive
    username/password wizard, which would otherwise block bosun forever waiting
    on stdin that nobody is watching.
    """
    for launcher in ("ubuntu2404.exe", "ubuntu2204.exe", "ubuntu.exe"):
        if runner.run(["where", launcher], timeout=10, read_only=True).ok:
            log(f"registering via {launcher}")
            runner.run([launcher, "install", "--root"], timeout=1800)
            return


def ensure_ready(runner: Runner, cfg: Config, log: Callable[[str], None]) -> str:
    """Return the name of a launchable distro, installing one if needed."""
    name = find(runner, cfg)

    if name is None:
        if not cfg.distro.get("install", True):
            raise BosunError(
                f"no WSL distro matching {cfg.distro_name!r} is registered and "
                "[distro].install is false"
            )
        install(runner, cfg, log)
        name = find(runner, cfg) or cfg.distro_name
        if not is_launchable(runner, name):
            register(runner, name, log)
            name = find(runner, cfg) or name

    if not is_launchable(runner, name):
        raise BosunError(
            f"WSL distro {name!r} is registered but will not start. Run 'wsl -l -v' "
            "to inspect it, start it once manually, then re-run bosun up."
        )

    log(f"distro ready: {name}")
    return name


def current_user(wsl: Wsl) -> str | None:
    """The distro's current default login user, if it has one."""
    res = wsl.sh("whoami", timeout=15, read_only=True)
    name = res.out
    return name if res.ok and name and name != "root" else None


def resolve_user(wsl: Wsl, cfg: Config, *, prompt: bool = True) -> str:
    """Decide which Linux username to provision.

    Order: ``[distro].user`` / ``BOSUN_USER`` -> the distro's existing default
    user -> an interactive prompt. Deliberately never defaults to the Windows
    account name, which is how a personal or employer-specific identity ends up
    baked into a config that gets committed.
    """
    if cfg.user:
        return cfg.user

    existing = current_user(wsl)
    if existing:
        return existing

    if not prompt:
        raise BosunError(
            "no Linux username configured. Set [distro].user in bosun.toml, "
            "pass --user, or set BOSUN_USER."
        )

    entered = input("Linux username to create: ").strip()
    if not entered:
        raise BosunError("a Linux username is required")
    return entered


def ensure_user(wsl: Wsl, user: str, log: Callable[[str], None], *, prompt: bool = True) -> None:
    """Create ``user`` if missing and put them in the sudo group.

    Runs as root over ``wsl -u root``, so it needs no existing credentials. A
    password is requested only when the account is genuinely new, and only
    because a sudo-capable account with no password is awkward to use later —
    bosun itself never needs it and never stores it.
    """
    if wsl.ok(f"id -u {user}", user="root", timeout=15):
        log(f"user {user} exists")
    else:
        log(f"creating user {user}")
        res = wsl.sh(f"useradd -m -s /bin/bash {user}", user="root", timeout=60)
        if not res.ok:
            raise BosunError(f"could not create user {user}: {res.stderr.strip()}")
        if prompt:
            _set_password(wsl, user, log)

    wsl.sh(f"usermod -aG sudo {user}", user="root", timeout=30)


def _set_password(wsl: Wsl, user: str, log: Callable[[str], None]) -> None:
    """Set the new account's password, piping it to chpasswd over stdin.

    Passed on stdin rather than interpolated into the shell command so it never
    appears in a process listing or a shell history inside the distro.
    """
    try:
        pw = getpass.getpass(f"Password for new Linux user {user} (blank to skip): ")
    except (EOFError, KeyboardInterrupt):
        print()
        return
    if not pw:
        log(f"no password set for {user} — set one later with: wsl -u root passwd {user}")
        return
    res = wsl.sh("chpasswd", user="root", stdin=f"{user}:{pw}\n", timeout=30)
    if not res.ok:
        log(f"warning: could not set password for {user}: {res.stderr.strip()}")


def ensure_in_group(wsl: Wsl, user: str, group: str, log: Callable[[str], None]) -> bool:
    """Add ``user`` to ``group``. Returns True when membership changed.

    The return value matters: group membership is only picked up by new login
    sessions, so a change means the caller must restart WSL before the engine is
    usable without sudo.
    """
    if wsl.ok(f"id -nG {user} | tr ' ' '\\n' | grep -qx {group}", user="root", timeout=15):
        return False
    res = wsl.sh(f"usermod -aG {group} {user}", user="root", timeout=30)
    if not res.ok:
        raise BosunError(f"could not add {user} to the {group} group: {res.stderr.strip()}")
    log(f"added {user} to the {group} group (needs a WSL restart to take effect)")
    return True


def configure_wsl_conf(wsl: Wsl, cfg: Config, user: str, log: Callable[[str], None]) -> bool:
    """Set the default user and systemd flag in ``/etc/wsl.conf``.

    Returns True when the file changed, meaning WSL must be restarted for the
    settings to apply. Read-modify-write through :mod:`bosun.wslconf` so that
    unrelated sections already in the file survive.
    """
    current = wsl.read_file("/etc/wsl.conf")
    want = wslconf.with_default_user(current, user)
    if cfg.distro.get("systemd", True):
        want = wslconf.with_systemd(want)

    if want.strip() == current.strip():
        return False

    log("updating /etc/wsl.conf")
    res = wsl.write_file("/etc/wsl.conf", want, mode="0644", owner="root:root")
    if not res.ok:
        raise BosunError(f"could not write /etc/wsl.conf: {res.stderr.strip()}")
    return True


def systemd_active(wsl: Wsl) -> bool:
    """True when systemd is PID 1 inside the distro."""
    res = wsl.sh("ps -p 1 -o comm=", user="root", timeout=15, read_only=True)
    return res.ok and res.out == "systemd"


def restart(wsl: Wsl, runner: Runner, log: Callable[[str], None], *, wait: int = 3) -> None:
    """Shut WSL down so wsl.conf and group changes take effect on next start."""
    log("restarting WSL")
    wsl.shutdown()
    time.sleep(wait)
    if wsl.distro:
        runner.run(["wsl.exe", "-d", wsl.distro, "-u", "root", "--", "true"], timeout=60)


def unregister(runner: Runner, name: str, timeout: int = 300) -> Result:
    """Permanently delete a distro and everything in it."""
    runner.run(["wsl.exe", "--shutdown"], timeout=60)
    return runner.run(["wsl.exe", "--unregister", name], timeout=timeout)
