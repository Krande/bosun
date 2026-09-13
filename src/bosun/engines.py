"""Container-engine profiles — the seam that keeps bosun from being Docker-only.

Everything the rest of bosun needs to know about a specific engine lives in an
:class:`EngineSpec`: which apt packages provide it, which systemd unit runs it,
where its daemon config and socket live, which Unix group grants non-root
access, and what the host-side CLI is called. :mod:`bosun.flows` reads those
fields and never names an engine directly, so teaching bosun a second engine is
a matter of adding a spec here rather than editing the provisioning logic.

Docker is implemented. Podman is registered as a known name that is not wired up
yet, so asking for it fails immediately with a clear message instead of half
provisioning a machine.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class AptRepo:
    """An apt repository to add when the distro's own packages fall short."""

    key_url: str
    # $ARCH and $CODENAME are substituted from the live distro at call time.
    list_line: str
    keyring: str
    list_file: str
    packages: tuple[str, ...]


@dataclass(frozen=True)
class EngineSpec:
    """Everything bosun needs in order to provision one container engine."""

    name: str
    # Tried first — the distro's own packages, which need no third-party repo.
    distro_packages: tuple[str, ...]
    # Fallback when the distro packages don't yield a working engine.
    upstream_repo: AptRepo | None
    # Packages to purge before installing from the upstream repo, to avoid the
    # two providers fighting over the same binaries.
    conflicts: tuple[str, ...]
    service: str
    # The daemon's process name, for spotting one that systemd does not own.
    daemon: str
    daemon_json: str
    socket: str
    group: str
    # The client binary on the Windows side, and how to install it.
    host_cli: str
    host_cli_packages: tuple[str, ...]
    # Directory under %USERPROFILE% where the client looks for TLS certs.
    cert_dir: str
    # Command line the systemd drop-in pins the daemon to. Needed because the
    # packaged unit passes -H fd://, which conflicts with any "hosts" key in
    # daemon.json and makes the service refuse to start.
    exec_start: str
    # Binaries that are docker CLI plugins rather than standalone commands.
    # Installed on PATH by pixi, they have to be copied into the CLI's
    # cli-plugins directory before `docker <name>` resolves them.
    cli_plugins: tuple[str, ...] = ()
    # Sub-commands checked by `bosun status` once the engine is up.
    verify: tuple[str, ...] = field(default_factory=tuple)
    implemented: bool = True

    def daemon_dir(self) -> str:
        return self.daemon_json.rsplit("/", 1)[0]


DOCKER = EngineSpec(
    name="docker",
    distro_packages=(
        "docker.io",
        "ca-certificates",
        "curl",
        "gnupg",
        "lsb-release",
        "docker-buildx-plugin",
        "docker-compose-plugin",
    ),
    upstream_repo=AptRepo(
        key_url="https://download.docker.com/linux/ubuntu/gpg",
        list_line=(
            "deb [arch=$ARCH signed-by=/etc/apt/keyrings/docker.gpg] "
            "https://download.docker.com/linux/ubuntu $CODENAME stable"
        ),
        keyring="/etc/apt/keyrings/docker.gpg",
        list_file="/etc/apt/sources.list.d/docker.list",
        packages=(
            "docker-ce",
            "docker-ce-cli",
            "containerd.io",
            "docker-buildx-plugin",
            "docker-compose-plugin",
        ),
    ),
    conflicts=("docker.io",),
    service="docker",
    daemon="dockerd",
    daemon_json="/etc/docker/daemon.json",
    socket="/var/run/docker.sock",
    group="docker",
    host_cli="docker",
    host_cli_packages=("docker-cli", "docker-compose", "docker-buildx"),
    cert_dir=".docker",
    cli_plugins=("docker-buildx", "docker-compose"),
    exec_start="/usr/bin/dockerd --containerd=/run/containerd/containerd.sock",
    verify=("buildx version", "compose version"),
)

# Registered but not wired up. Podman's rootless model means it needs a
# different exposure path (a per-user systemd socket unit rather than a
# system-wide daemon with a hosts array), so it is a real piece of work rather
# than a table entry — hence failing loudly instead of pretending.
PODMAN = EngineSpec(
    name="podman",
    distro_packages=("podman",),
    upstream_repo=None,
    conflicts=(),
    service="podman",
    daemon="podman",
    daemon_json="/etc/containers/containers.conf",
    socket="/run/podman/podman.sock",
    group="podman",
    host_cli="podman",
    host_cli_packages=("podman",),
    cert_dir=".config/containers",
    exec_start="/usr/bin/podman system service",
    implemented=False,
)

ENGINES: dict[str, EngineSpec] = {spec.name: spec for spec in (DOCKER, PODMAN)}


class UnsupportedEngine(ValueError):
    """The requested engine is unknown, or known but not implemented yet."""


def get(name: str) -> EngineSpec:
    """Look up an engine spec by name, failing before any side effect."""
    spec = ENGINES.get(name.strip().lower())
    if spec is None:
        known = ", ".join(sorted(ENGINES))
        raise UnsupportedEngine(f"unknown engine {name!r} — bosun knows: {known}")
    if not spec.implemented:
        raise UnsupportedEngine(
            f"engine {spec.name!r} is not implemented yet; bosun currently provisions "
            f"{', '.join(sorted(s.name for s in ENGINES.values() if s.implemented))}"
        )
    return spec
