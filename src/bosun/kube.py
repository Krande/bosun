"""kubectl inside the distro, and a kubeconfig for a managed cluster.

Opt-in: nothing here runs unless ``[kubernetes].enabled`` is set. A container
host does not need kubectl, and installing three apt repositories on a machine
that never asked for them is not a favour.

``kubectl`` comes from the Kubernetes project's own apt repository and is
provider-neutral. Everything cloud-specific — the CLI, the login flow, the
credential plugin — lives in :mod:`bosun.providers`, so nothing in this module
names a cloud.

Two details that are easy to get wrong:

**There is no "latest" channel.** Every pkgs.k8s.io URL names one minor
version, so a channel has to be chosen before the source file can even be
written. bosun asks dl.k8s.io what stable is, and falls back to a pinned minor
only when that is unreachable.

**Version skew matters.** kubectl supports one minor either side of the API
server, and managed offerings deliberately trail upstream stable — so a fresh
install from the stable channel is out of skew against a real cluster more
often than not. :meth:`Kubectl.align_to_server` re-points the repository at the
cluster's own minor once a cluster is known.

Installs run as root; anything touching a credential runs as the Linux user.
``~/.kube/config`` and the provider's own credential directory are per-user, and
a root-owned pair is invisible from the shell the user actually types in — they
would see "the connection to the server localhost:8080 was refused" while a
perfectly good kubeconfig sat in root's home.
"""

from __future__ import annotations

import re
from collections.abc import Callable

from .config import Config
from .exec import BosunError, Wsl
from .providers import KEYRING_DIR, Provider

KUBECTL_KEYRING = f"{KEYRING_DIR}/kubernetes-apt-keyring.gpg"
KUBECTL_LIST = "/etc/apt/sources.list.d/kubernetes.list"
KUBECTL_STABLE_URL = "https://dl.k8s.io/release/stable.txt"
KUBECTL_CHANNEL_URL = "https://pkgs.k8s.io/core:/stable:/{channel}/deb/"

# Only reached when dl.k8s.io is unreachable. Stale by design rather than
# wrong: align_to_server() re-points the repository at the cluster's own minor
# as soon as a cluster is known, which is the version that actually matters.
KUBECTL_FALLBACK_CHANNEL = "v1.31"

# kubectl supports one minor of skew either side of the API server.
MAX_SKEW = 1

_MINOR = re.compile(r"v?(\d+)\.(\d+)")


def minor_of(version: str | None) -> tuple[int, int] | None:
    """Parse ``v1.31.2`` / ``1.31`` into ``(1, 31)``, or None."""
    if not version:
        return None
    match = _MINOR.search(version)
    return (int(match.group(1)), int(match.group(2))) if match else None


def channel_of(version: str | None) -> str | None:
    """The pkgs.k8s.io channel for a version, e.g. ``v1.31``."""
    parsed = minor_of(version)
    return f"v{parsed[0]}.{parsed[1]}" if parsed else None


def skew(client: str | None, server: str | None) -> int | None:
    """Minor versions between client and server, or None if either is unknown."""
    a, b = minor_of(client), minor_of(server)
    if a is None or b is None:
        return None
    return (a[0] - b[0]) * 100 + (a[1] - b[1])


def within_skew(client: str | None, server: str | None) -> bool:
    """True when kubectl is close enough to the API server to be supported."""
    distance = skew(client, server)
    return distance is None or abs(distance) <= MAX_SKEW


class Kubectl:
    """Installs and version-aligns kubectl inside the distro."""

    def __init__(self, wsl: Wsl, cfg: Config, log: Callable[[str], None]) -> None:
        self.wsl = wsl
        self.cfg = cfg
        self.log = log

    # ── state ─────────────────────────────────────────────────────────────
    def installed(self) -> bool:
        return self.wsl.ok("command -v kubectl", user="root", timeout=20)

    def client_version(self) -> str | None:
        res = self.wsl.sh(
            "kubectl version --client -o json 2>/dev/null", user="root", timeout=30, read_only=True
        )
        if not res.ok:
            return None
        import json

        try:
            return json.loads(res.out).get("clientVersion", {}).get("gitVersion")
        except (json.JSONDecodeError, AttributeError):
            return None

    def current_channel(self) -> str | None:
        """The channel the apt source file currently points at."""
        content = self.wsl.read_file(KUBECTL_LIST)
        match = re.search(r"stable:/(v\d+\.\d+)/", content or "")
        return match.group(1) if match else None

    def latest_channel(self) -> str:
        """Ask dl.k8s.io what stable is; fall back to a pinned minor."""
        res = self.wsl.sh(
            f"curl -fsSL --max-time 15 {KUBECTL_STABLE_URL}",
            user="root",
            timeout=45,
            read_only=True,
        )
        channel = channel_of(res.out) if res.ok else None
        if channel is None:
            self.log(
                f"could not reach {KUBECTL_STABLE_URL}; using {KUBECTL_FALLBACK_CHANNEL} "
                "(realigned once a cluster is known)"
            )
            return KUBECTL_FALLBACK_CHANNEL
        return channel

    # ── install ───────────────────────────────────────────────────────────
    def add_repository(self, channel: str) -> None:
        """Point apt at one pkgs.k8s.io channel. Replaces any previous one."""
        url = KUBECTL_CHANNEL_URL.format(channel=channel)
        self.wsl.sh(f"install -d -m 0755 -o root -g root {KEYRING_DIR}", user="root", timeout=30)

        res = self.wsl.sh(
            f"curl -fsSL {url}Release.key | gpg --batch --yes --dearmor -o {KUBECTL_KEYRING}",
            user="root",
            timeout=120,
        )
        if not res.ok:
            raise BosunError(f"could not fetch the kubectl signing key:\n{res.stderr.strip()}")
        self.wsl.sh(f"chmod a+r {KUBECTL_KEYRING}", user="root", timeout=15)

        line = f"deb [signed-by={KUBECTL_KEYRING}] {url} /"
        res = self.wsl.sh(f"echo '{line}' > {KUBECTL_LIST}", user="root", timeout=30)
        if not res.ok:
            raise BosunError(f"could not write {KUBECTL_LIST}:\n{res.stderr.strip()}")

    def ensure_installed(self, apt_install, apt_update) -> None:
        """Install kubectl if missing. Takes the apt helpers as arguments."""
        if self.installed():
            self.log(f"kubectl already installed ({self.client_version() or 'unknown version'})")
            return

        channel = str(self.cfg.kubernetes.get("channel") or "") or self.latest_channel()
        self.log(f"installing kubectl from the {channel} channel")
        self.add_repository(channel)
        apt_update(self.wsl, self.cfg, self.log)
        apt_install(self.wsl, self.cfg, ["kubectl"], self.log)

    def align_to_server(self, server_version: str | None, apt_install, apt_update) -> bool:
        """Re-point the repository at the cluster's minor when out of skew.

        Managed offerings trail upstream stable deliberately, so the newest
        kubectl is routinely too new for the cluster you actually have.
        """
        if server_version is None:
            return False
        client = self.client_version()
        if within_skew(client, server_version):
            return False

        channel = channel_of(server_version)
        if channel is None or channel == self.current_channel():
            return False

        self.log(f"kubectl {client} is out of skew with the cluster ({server_version}); ")
        self.log(f"switching to the {channel} channel")
        self.add_repository(channel)
        apt_update(self.wsl, self.cfg, self.log)
        apt_install(self.wsl, self.cfg, ["kubectl"], self.log)
        return True


class ProviderTools:
    """Installs one provider's CLI and its credential plugin."""

    def __init__(
        self, wsl: Wsl, cfg: Config, provider: Provider, log: Callable[[str], None]
    ) -> None:
        self.wsl = wsl
        self.cfg = cfg
        self.provider = provider
        self.log = log

    def installed(self) -> bool:
        return self.wsl.ok(f"command -v {self.provider.cli}", user="root", timeout=20)

    def missing_tools(self) -> list[str]:
        return [
            tool
            for tool in self.provider.extra_tools
            if not self.wsl.ok(f"command -v {tool}", user="root", timeout=20)
        ]

    def add_repository(self, apt_update) -> None:
        source = self.provider.apt_source
        if source is None:
            return

        self.log(f"adding the {self.provider.title} apt repository")
        self.wsl.sh(f"install -d -m 0755 -o root -g root {KEYRING_DIR}", user="root", timeout=30)

        res = self.wsl.sh(
            f"curl -fsSL {source.key_url} | gpg --batch --yes --dearmor -o {source.keyring}",
            user="root",
            timeout=120,
        )
        if not res.ok:
            raise BosunError(
                f"could not fetch the {self.provider.title} signing key:\n{res.stderr.strip()}"
            )
        self.wsl.sh(f"chmod a+r {source.keyring}", user="root", timeout=15)

        line = (
            source.list_line.format(keyring=source.keyring)
            .replace("$ARCH", "$(dpkg --print-architecture)")
            .replace("$CODENAME", "$(. /etc/os-release && echo $VERSION_CODENAME)")
        )
        res = self.wsl.sh(f'echo "{line}" > {source.list_file}', user="root", timeout=30)
        if not res.ok:
            raise BosunError(f"could not write {source.list_file}:\n{res.stderr.strip()}")
        apt_update(self.wsl, self.cfg, self.log)

    def ensure_installed(self, apt_install, apt_update) -> None:
        if not self.installed() and self.provider.apt_source is not None:
            self.add_repository(apt_update)
            apt_install(self.wsl, self.cfg, list(self.provider.apt_source.packages), self.log)
        else:
            self.log(f"{self.provider.cli} already installed")

    def fix_credential_ownership(self, user: str) -> None:
        """Hand the credential directories back to the Linux user.

        Everything above installs as root, and a root-owned ~/.kube/config is
        invisible from the shell the user actually types in: they get "the
        connection to the server localhost:8080 was refused" while a perfectly
        good kubeconfig sits in root's home.
        """
        for directory in self.provider.credential_dirs:
            self.wsl.sh(
                f"install -d -o {user} -g {user} -m 0700 ~{user}/{directory}",
                user="root",
                timeout=30,
            )
            self.wsl.sh(f"chown -R {user}:{user} ~{user}/{directory}", user="root", timeout=60)
