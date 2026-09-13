"""Reclaiming disk space — ``bosun shrink``.

A WSL distro's virtual disk grows to accommodate what you put in it and never
shrinks back on its own. Delete a hundred gigabytes of container layers and the
ext4 filesystem inside reports the space as free while ``ext4.vhdx`` on the
Windows side stays exactly as large as it ever got. Reclaiming it is a two-part
job: free the space inside the distro, then compact the disk image from outside
it with every handle closed.
"""

from __future__ import annotations

import os
import pathlib
from collections.abc import Callable

from .config import Config
from .engines import EngineSpec
from .exec import BosunError, Runner, Wsl

# Where WSL keeps distro disks. Store-installed distros land under Packages;
# distros installed by modern `wsl --install` land under the wsl directory.
SEARCH_ROOTS = (
    ("LOCALAPPDATA", "Packages"),
    ("LOCALAPPDATA", "wsl"),
)


def discover(cfg: Config, env: dict[str, str] | None = None) -> list[pathlib.Path]:
    """Find candidate ``ext4.vhdx`` files for the configured distro.

    Matched on ``[distro].match`` appearing anywhere in the path, which is how
    the Store package directory encodes the distro name. Returns every match
    rather than guessing, so an ambiguous result can be reported instead of the
    wrong disk being compacted.
    """
    env = os.environ if env is None else env
    found: list[pathlib.Path] = []
    needle = cfg.distro_match or cfg.distro_name.lower()

    for var, sub in SEARCH_ROOTS:
        base = env.get(var)
        if not base:
            continue
        root = pathlib.Path(base) / sub
        if not root.is_dir():
            continue
        for path in root.glob("*/**/ext4.vhdx"):
            if needle in str(path).lower():
                found.append(path)

    # Deduplicate while preserving discovery order.
    return list(dict.fromkeys(found))


def resolve_path(cfg: Config, env: dict[str, str] | None = None) -> pathlib.Path:
    """The single disk image to compact, or a clear error explaining why not."""
    configured = str(cfg.vhdx.get("path") or "").strip()
    if configured:
        path = pathlib.Path(os.path.expandvars(configured)).expanduser()
        if not path.is_file():
            raise BosunError(f"[vhdx].path does not exist: {path}")
        return path

    candidates = discover(cfg, env)
    if not candidates:
        raise BosunError(
            "could not find an ext4.vhdx for this distro. Locate it and set "
            "[vhdx].path in bosun.toml (or pass --vhdx)."
        )
    if len(candidates) > 1:
        listing = "\n  ".join(str(c) for c in candidates)
        raise BosunError(
            f"found {len(candidates)} candidate disks; set [vhdx].path (or pass "
            f"--vhdx) to choose one:\n  {listing}"
        )
    return candidates[0]


def size_gb(path: pathlib.Path) -> float:
    try:
        return path.stat().st_size / (1024**3)
    except OSError:
        return 0.0


def reclaim_inside(wsl: Wsl, spec: EngineSpec, log: Callable[[str], None]) -> None:
    """Free space inside the distro before the image is compacted.

    Compacting only recovers blocks the filesystem has released, so this has to
    happen first or the shrink reclaims almost nothing. ``fstrim`` is the step
    that actually matters: it tells the virtual disk which blocks are now unused.
    """
    log("cleaning package caches")
    for script in ("apt-get clean", "apt-get -y autoremove"):
        wsl.sh(script, user="root", timeout=600)

    if wsl.ok(f"command -v {spec.name}", user="root", timeout=15):
        log(f"pruning unused {spec.name} data")
        wsl.sh(f"{spec.name} system prune -af", user="root", timeout=900)

    log("trimming the filesystem")
    wsl.sh("fstrim -av", user="root", timeout=600)


def compact(runner: Runner, path: pathlib.Path, log: Callable[[str], None]) -> None:
    """Compact the disk image with diskpart.

    WSL must be fully shut down first — diskpart cannot attach a disk that is
    still open, and the failure mode is a confusing "virtual disk file is
    already in use" rather than anything mentioning WSL.
    """
    log("shutting WSL down")
    runner.run(["wsl.exe", "--shutdown"], timeout=60)

    script = (
        f'select vdisk file="{path}"\nattach vdisk readonly\ncompact vdisk\ndetach vdisk\nexit\n'
    )
    log(f"compacting {path}")
    res = runner.run(["diskpart"], stdin=script, timeout=3600)

    # diskpart reports failures in stdout while still exiting zero, so the
    # output has to be inspected rather than the return code trusted.
    output = (res.stdout or "") + (res.stderr or "")
    if not res.ok or "DiskPart successfully compacted" not in output:
        raise BosunError(
            "diskpart did not report a successful compaction. It needs an "
            "elevated prompt - re-run bosun from an Administrator terminal.\n\n"
            f"{output.strip()}"
        )
