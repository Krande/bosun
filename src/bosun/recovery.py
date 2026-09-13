"""Unsticking a WSL install that has stopped responding.

For the state where ``wsl.exe`` hangs, or a distro will not start and will not
stop, and ``bosun up`` therefore cannot get far enough to repair anything.

What is deliberately **not** here: unregistering distros, and toggling Windows
features with DISM. Both destroy a distro's filesystem, and a command called
``repair`` must not do that behind your back. ``bosun down`` exists for when you
actually mean it, and asks first.

The escalation is least to most disruptive: shut WSL down, clear the host-side
processes that can outlive the shutdown and keep a dead session pinned, then
bounce the service — the last of which needs an elevated prompt and is skipped
rather than failed when it is not available, because the first two steps fix
most of these on their own.
"""

from __future__ import annotations

from collections.abc import Callable

from .exec import Runner

# Host-side processes that can outlive `wsl --shutdown` and keep the old
# session alive. Ordered least to most privileged.
LEFTOVER_PROCESSES = ("wslhost.exe", "wslservice.exe")

LXSS_SERVICE = "LxssManager"


class Recovery:
    """Escalating attempts to get WSL responding again."""

    def __init__(self, runner: Runner, log: Callable[[str], None]) -> None:
        self.runner = runner
        self.log = log

    def is_elevated(self) -> bool:
        """True when running in an elevated prompt.

        ``net session`` is refused without elevation and succeeds with it,
        which is the cheapest reliable probe that needs no extra tooling.
        """
        return self.runner.run(["net", "session"], timeout=30, read_only=True).ok

    def shutdown(self) -> None:
        self.log("shutting WSL down")
        self.runner.run(["wsl.exe", "--shutdown"], timeout=90)

    def clear_leftovers(self) -> list[str]:
        """Kill host-side processes that survived the shutdown."""
        killed = []
        for image in LEFTOVER_PROCESSES:
            if self.runner.run(["taskkill", "/f", "/im", image], timeout=30).ok:
                killed.append(image)
                self.log(f"cleared {image}")
        return killed

    def restart_service(self) -> bool:
        """Bounce the WSL service. Needs elevation, so it is skipped without it."""
        if not self.is_elevated():
            self.log(
                f"skipping the {LXSS_SERVICE} restart - it needs an Administrator terminal. "
                "The steps above fix most cases; re-run elevated if this one did not."
            )
            return False
        self.log(f"restarting {LXSS_SERVICE}")
        self.runner.run(["net", "stop", LXSS_SERVICE], timeout=120)
        return self.runner.run(["net", "start", LXSS_SERVICE], timeout=120).ok

    def recover(self) -> None:
        """Run the escalation in order, least disruptive first."""
        self.shutdown()
        self.clear_leftovers()
        self.restart_service()
