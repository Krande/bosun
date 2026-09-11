"""Installing and configuring the container engine inside the distro.

All privileged work runs through ``wsl -u root`` (see :mod:`bosun.distro` for
why there is no sudoers drop-in). Each step is idempotent: re-running ``bosun
up`` on a provisioned machine should change nothing and restart nothing.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence

from . import daemonjson
from .config import Config
from .engines import EngineSpec
from .exec import BosunError, Wsl

# Processes that hold the apt/dpkg locks. An install that starts while one of
# these is mid-run fails with a lock error that reads like a bosun bug.
LOCK_HOLDERS = ("apt", "apt-get", "dpkg", "unattended-upgrade", "snapd")


def wait_for_apt(wsl: Wsl, log: Callable[[str], None], *, patience: int = 60) -> None:
    """Wait for apt/dpkg locks to clear, then force the issue if they don't.

    Fresh WSL distros routinely run unattended-upgrades on first boot, so this
    is the normal case rather than an edge case. After ``patience`` seconds the
    upgrade is stopped and any interrupted dpkg state is repaired, because
    waiting indefinitely on a background job nobody asked for is worse than
    pre-empting it.
    """
    pattern = "|".join(LOCK_HOLDERS)
    deadline = time.monotonic() + patience
    while time.monotonic() < deadline:
        busy = wsl.sh(f"pgrep -f '({pattern})' >/dev/null", user="root", timeout=15).ok
        if not busy:
            return
        log("waiting for apt locks to clear")
        time.sleep(3)

    log("apt still busy; stopping unattended-upgrades and repairing dpkg state")
    wsl.sh("systemctl stop unattended-upgrades.service", user="root", timeout=60)
    wsl.sh("DEBIAN_FRONTEND=noninteractive dpkg --configure -a", user="root", timeout=300)


def apt_update(wsl: Wsl, cfg: Config, log: Callable[[str], None]) -> None:
    flags = " ".join(cfg.apt_flags())
    log("apt update")
    res = wsl.sh(
        f"DEBIAN_FRONTEND=noninteractive apt-get {flags} update -y",
        user="root",
        timeout=600,
    )
    if not res.ok:
        raise BosunError(
            "apt update failed inside the distro. This is almost always DNS or a "
            f"proxy rather than bosun:\n{res.stderr.strip()}"
        )


def apt_install(
    wsl: Wsl,
    cfg: Config,
    packages: Sequence[str],
    log: Callable[[str], None],
    *,
    check: bool = True,
) -> bool:
    """Install packages. Returns success; raises only when ``check``."""
    flags = " ".join(cfg.apt_flags())
    names = " ".join(packages)
    log(f"apt install {names}")
    res = wsl.sh(
        f"DEBIAN_FRONTEND=noninteractive apt-get {flags} install -y "
        f"--no-install-recommends {names}",
        user="root",
        timeout=1800,
    )
    if not res.ok and check:
        raise BosunError(f"failed to install {names}:\n{res.stderr.strip()}")
    return res.ok


def engine_present(wsl: Wsl, spec: EngineSpec) -> bool:
    return wsl.ok(f"command -v {spec.name}", user="root", timeout=15)


def service_unit_present(wsl: Wsl, spec: EngineSpec) -> bool:
    return wsl.ok(
        f"systemctl list-unit-files | awk '{{print $1}}' | grep -qx {spec.service}.service",
        user="root",
        timeout=20,
    )


def add_upstream_repo(wsl: Wsl, cfg: Config, spec: EngineSpec, log: Callable[[str], None]) -> None:
    """Add the engine vendor's apt repo and signing key."""
    repo = spec.upstream_repo
    if repo is None:
        raise BosunError(f"no upstream repo configured for engine {spec.name!r}")

    log(f"adding the {spec.name} upstream apt repository")
    wsl.sh("install -d -m 0755 -o root -g root /etc/apt/keyrings", user="root", timeout=30)
    apt_install(wsl, cfg, ["ca-certificates", "curl", "gnupg"], log)

    res = wsl.sh(
        f"curl -fsSL {repo.key_url} | gpg --batch --yes --dearmor -o {repo.keyring}",
        user="root",
        timeout=120,
    )
    if not res.ok:
        raise BosunError(f"could not fetch the {spec.name} signing key:\n{res.stderr.strip()}")
    wsl.sh(f"chmod a+r {repo.keyring}", user="root", timeout=15)

    # $ARCH and $CODENAME are left for the distro's own shell to expand, so the
    # line is correct on arm64 and on whichever release is actually installed.
    line = repo.list_line.replace("$ARCH", "$(dpkg --print-architecture)").replace(
        "$CODENAME", "$(. /etc/os-release && echo $VERSION_CODENAME)"
    )
    res = wsl.sh(f'echo "{line}" > {repo.list_file}', user="root", timeout=30)
    if not res.ok:
        raise BosunError(f"could not write {repo.list_file}:\n{res.stderr.strip()}")

    apt_update(wsl, cfg, log)


def ensure_engine(wsl: Wsl, cfg: Config, spec: EngineSpec, log: Callable[[str], None]) -> None:
    """Install the engine, preferring the distro's own packages.

    The distro packages need no third-party repo and are usually sufficient, so
    they are tried first; the vendor repo is a fallback rather than the default.
    Both paths end at the same verification, and a failed first attempt is not
    an error — only a failed second one is.
    """
    if engine_present(wsl, spec) and service_unit_present(wsl, spec):
        log(f"{spec.name} already installed")
        return

    wait_for_apt(wsl, log)
    apt_update(wsl, cfg, log)

    log(f"installing {spec.name} from the distro repositories")
    apt_install(wsl, cfg, spec.distro_packages, log, check=False)
    if engine_present(wsl, spec) and service_unit_present(wsl, spec):
        log(f"{spec.name} installed from the distro repositories")
        return

    if spec.upstream_repo is None:
        raise BosunError(
            f"{spec.name} is not available from the distro repositories and has no "
            "upstream repo configured"
        )

    log(f"distro packages insufficient; falling back to the {spec.name} upstream repo")
    for pkg in spec.conflicts:
        wsl.sh(f"apt-get -y purge {pkg}", user="root", timeout=300)
    add_upstream_repo(wsl, cfg, spec, log)
    apt_install(wsl, cfg, spec.upstream_repo.packages, log)

    if not engine_present(wsl, spec):
        raise BosunError(f"{spec.name} still not on PATH after installation")


def configure_daemon(wsl: Wsl, cfg: Config, spec: EngineSpec, log: Callable[[str], None]) -> bool:
    """Write daemon.json and the systemd drop-in. Returns True when changed.

    Only writes when the live file does not already express the desired state,
    so a no-op ``bosun up`` does not restart the engine and kill containers.
    """
    current = wsl.read_file(spec.daemon_json)
    changed = False

    if daemonjson.needs_update(current, cfg, spec):
        log(f"writing {spec.daemon_json}")
        wsl.sh(f"mkdir -p {spec.daemon_dir()}", user="root", timeout=15)
        if current.strip():
            wsl.sh(f"cp {spec.daemon_json} {spec.daemon_json}.bosun.bak", user="root", timeout=15)
        res = wsl.write_file(spec.daemon_json, daemonjson.render(current, cfg, spec), mode="0644")
        if not res.ok:
            raise BosunError(f"could not write {spec.daemon_json}: {res.stderr.strip()}")
        changed = True
    else:
        log(f"{spec.daemon_json} already correct")

    override_dir = f"/etc/systemd/system/{spec.service}.service.d"
    override_path = f"{override_dir}/bosun.conf"
    want_override = daemonjson.systemd_override(spec)
    if wsl.read_file(override_path).strip() != want_override.strip():
        log("writing the systemd drop-in")
        wsl.sh(f"mkdir -p {override_dir}", user="root", timeout=15)
        wsl.write_file(override_path, want_override, mode="0644")
        wsl.sh("systemctl daemon-reload", user="root", timeout=60)
        changed = True

    return changed


def restart_engine(wsl: Wsl, spec: EngineSpec, log: Callable[[str], None]) -> None:
    """Restart the engine and wait for its API to answer."""
    log(f"restarting {spec.service}")
    wsl.sh(f"systemctl enable {spec.service}", user="root", timeout=90)
    res = wsl.sh(f"systemctl restart {spec.service}", user="root", timeout=180)
    if not res.ok:
        # The daemon's own journal says why; systemctl's exit code does not.
        journal = wsl.sh(f"journalctl -u {spec.service} -n 30 --no-pager", user="root", timeout=60)
        raise BosunError(
            f"{spec.service} failed to start:\n{res.stderr.strip()}\n\n"
            f"--- last journal lines ---\n{journal.stdout.strip()}"
        )
    wait_for_engine(wsl, spec, log)


def wait_for_engine(
    wsl: Wsl, spec: EngineSpec, log: Callable[[str], None], *, attempts: int = 20, delay: int = 3
) -> bool:
    """Poll the engine's API until it answers.

    Probed as root: at this point the invoking user's new group membership has
    not taken effect yet, so a permission error on the socket would be
    indistinguishable from a daemon that never came up.
    """
    for _ in range(attempts):
        if wsl.ok(f"{spec.name} info", user="root", timeout=20):
            log(f"{spec.name} is responding")
            return True
        time.sleep(delay)
    log(f"warning: {spec.name} is not responding yet; continuing")
    return False


def verify(wsl: Wsl, spec: EngineSpec, log: Callable[[str], None]) -> None:
    """Report the versions of the engine's bundled sub-commands."""
    for sub in spec.verify:
        res = wsl.sh(f"{spec.name} {sub}", user="root", timeout=30)
        label = f"{spec.name} {sub}"
        log(f"  {label}: {res.out.splitlines()[0] if res.ok and res.out else 'unavailable'}")
