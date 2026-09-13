"""``bosun.toml`` — declarative config, plus bosun's built-in defaults.

Every command resolves its settings from three layers, most-specific first::

    CLI flag  ->  env var (BOSUN_*)  ->  bosun.toml  ->  built-in default

so a machine keeps its stable choices (distro, Linux username, engine, exposure
mode) in ``bosun.toml`` and passes only what varies per run on the command line.
bosun runs with no config file at all — the defaults below are a working setup.

Nothing here is specific to one person or one employer. The Linux username, the
distro, the ports, the client context name and the TLS certificate subject are
all settings, not constants, precisely so the defaults can be shipped publicly.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_CONFIG_FILE = "bosun.toml"
CONFIG_ENV = "BOSUN_TOML"

# How the engine's API is reached from Windows.
EXPOSE_MODES = ("tcp", "tls", "unix")

DEFAULTS: dict[str, Any] = {
    "distro": {
        # The distro bosun installs and targets. Any WSL distro name works;
        # the apt-based install path assumes a Debian derivative.
        "name": "Ubuntu-24.04",
        # Substring used to adopt an already-installed distro when "name" is
        # absent — so an existing Ubuntu-22.04 is reused rather than a second
        # distro being installed alongside it.
        "match": "ubuntu",
        # Linux username inside the distro. Empty means: reuse the distro's
        # existing default user, else prompt. Never hardcode a person here.
        "user": "",
        # Install the distro when it is missing. False makes `up` fail instead,
        # which is what you want where the distro comes from somewhere else.
        "install": True,
        # Enable systemd via /etc/wsl.conf. The engine's service unit needs it.
        "systemd": True,
    },
    "engine": {
        # Which container engine to install inside the distro. See engines.py.
        "name": "docker",
        # "tcp"  — plain TCP on [engine].port, loopback only (default)
        # "tls"  — mutual TLS on [engine].tls_port, certs exported to Windows
        # "unix" — unix socket only; nothing exposed to Windows
        "expose": "tcp",
        "host": "127.0.0.1",
        "port": 2375,
        "tls_port": 2376,
    },
    "client": {
        # Name of the context created on the Windows side.
        "context": "bosun",
        "manage_context": True,
        # Install the host-side CLI when missing.
        "install_cli": True,
        "installer": "pixi",
        # Copy docker CLI plugins (compose, buildx) into ~/.docker/cli-plugins.
        # Without this `docker compose` fails with "unknown command" even though
        # `docker-compose` works, because the CLI only finds plugins there.
        "wire_plugins": True,
    },
    "keepalive": {
        # Hold the distro open so the endpoint keeps answering. WSL idles an
        # instance out about a minute after its last Windows-side command, and
        # the localhost forward dies with it — so without this, docker works
        # right after `bosun up` and stops working shortly afterwards.
        "enabled": True,
    },
    "tls": {
        "dir": "/etc/docker/ssl",
        # Certificate subject. Blank fields are omitted from the DN, so the
        # default subject carries no organisation or country at all.
        "country": "",
        "org": "bosun",
        "ca_cn": "bosun-ca",
        "server_cn": "localhost",
        "client_cn": "bosun-client",
        "key_bits": 4096,
        "ca_days": 3650,
        "cert_days": 1825,
        "san_dns": ["localhost"],
        "san_ip": ["127.0.0.1"],
        # Where client certs land on Windows. Empty = the engine's conventional
        # directory under %USERPROFILE% (e.g. ~/.docker).
        "windows_cert_dir": "",
    },
    "apt": {
        # WSL's NAT can make IPv6 apt fetches hang; forcing IPv4 with short
        # timeouts is what keeps `up` from stalling for minutes on some networks.
        "force_ipv4": True,
        "retries": 3,
        "timeout": 15,
    },
    "vhdx": {
        # Path to the distro's ext4.vhdx for `bosun shrink`. Empty = discover it
        # under %LOCALAPPDATA%\\Packages.
        "path": "",
    },
}

# env var -> (section, key, caster). The middle layer of the three.
ENV_OVERRIDES: dict[str, tuple[str, str, str]] = {
    "BOSUN_DISTRO": ("distro", "name", "str"),
    "BOSUN_USER": ("distro", "user", "str"),
    "BOSUN_ENGINE": ("engine", "name", "str"),
    "BOSUN_EXPOSE": ("engine", "expose", "str"),
    "BOSUN_HOST": ("engine", "host", "str"),
    "BOSUN_PORT": ("engine", "port", "int"),
    "BOSUN_TLS_PORT": ("engine", "tls_port", "int"),
    "BOSUN_CONTEXT": ("client", "context", "str"),
    "BOSUN_INSTALL_CLI": ("client", "install_cli", "bool"),
    "BOSUN_KEEPALIVE": ("keepalive", "enabled", "bool"),
    "BOSUN_VHDX": ("vhdx", "path", "str"),
}

TRUTHY = ("1", "true", "yes", "on")


class ConfigError(ValueError):
    """bosun.toml (or an env override) asks for something bosun cannot do."""


def deep_merge(base: dict, over: dict) -> dict:
    """Recursively merge ``over`` onto ``base`` (lists/scalars replace)."""
    out = dict(base)
    for key, val in over.items():
        if isinstance(val, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], val)
        else:
            out[key] = val
    return out


def load_toml(path: str | None = None) -> dict:
    """Load bosun.toml. Order: explicit ``path``, ``$BOSUN_TOML``, ``./bosun.toml``.

    Returns ``{}`` when no file is found — the built-in defaults are a complete,
    working configuration on their own.
    """
    candidate = path or os.environ.get(CONFIG_ENV) or DEFAULT_CONFIG_FILE
    p = Path(candidate)
    if not p.is_file():
        if path:  # explicitly asked for, so its absence is an error
            raise ConfigError(f"config file not found: {p}")
        return {}
    with p.open("rb") as fh:
        return tomllib.load(fh)


def _cast(raw: str, kind: str) -> Any:
    if kind == "int":
        try:
            return int(raw)
        except ValueError as exc:
            raise ConfigError(f"expected an integer, got {raw!r}") from exc
    if kind == "bool":
        return raw.strip().lower() in TRUTHY
    return raw


def apply_env(data: dict, env: dict[str, str] | None = None) -> dict:
    """Overlay the ``BOSUN_*`` env vars onto ``data``."""
    env = os.environ if env is None else env
    out = dict(data)
    for var, (section, key, kind) in ENV_OVERRIDES.items():
        raw = env.get(var)
        if raw is None or raw == "":
            continue
        out = deep_merge(out, {section: {key: _cast(raw, kind)}})
    return out


@dataclass(frozen=True)
class Config:
    """Fully resolved settings. Built by :func:`resolve`; never mutated."""

    distro: dict[str, Any] = field(default_factory=dict)
    engine: dict[str, Any] = field(default_factory=dict)
    client: dict[str, Any] = field(default_factory=dict)
    keepalive: dict[str, Any] = field(default_factory=dict)
    tls: dict[str, Any] = field(default_factory=dict)
    apt: dict[str, Any] = field(default_factory=dict)
    vhdx: dict[str, Any] = field(default_factory=dict)

    # ── the handful of values read often enough to deserve an accessor ──
    @property
    def distro_name(self) -> str:
        return str(self.distro["name"])

    @property
    def distro_match(self) -> str:
        return str(self.distro["match"]).lower()

    @property
    def user(self) -> str:
        return str(self.distro.get("user") or "")

    @property
    def engine_name(self) -> str:
        return str(self.engine["name"])

    @property
    def expose(self) -> str:
        return str(self.engine["expose"])

    @property
    def tls_enabled(self) -> bool:
        return self.expose == "tls"

    @property
    def port(self) -> int:
        return int(self.engine["tls_port"] if self.tls_enabled else self.engine["port"])

    @property
    def endpoint(self) -> str:
        """The tcp:// URL Windows talks to. Empty in unix-only mode."""
        if self.expose == "unix":
            return ""
        return f"tcp://{self.engine['host']}:{self.port}"

    @property
    def context(self) -> str:
        return str(self.client["context"])

    def apt_flags(self) -> list[str]:
        """apt-get options assembled from ``[apt]``."""
        flags: list[str] = []
        if self.apt.get("force_ipv4", True):
            flags += ["-o", "Acquire::ForceIPv4=true"]
        flags += ["-o", f"Acquire::Retries={int(self.apt.get('retries', 3))}"]
        timeout = int(self.apt.get("timeout", 15))
        flags += ["-o", f"Acquire::http::Timeout={timeout}"]
        flags += ["-o", f"Acquire::https::Timeout={timeout}"]
        return flags

    def subject(self, cn: str) -> str:
        """An openssl subject string for ``cn``, omitting blank DN components.

        Keeping country and organisation optional is what lets the shipped
        default carry no identifying information.
        """
        parts = []
        if self.tls.get("country"):
            parts.append(f"/C={self.tls['country']}")
        if self.tls.get("org"):
            parts.append(f"/O={self.tls['org']}")
        parts.append(f"/CN={cn}")
        return "".join(parts)


def validate(data: dict) -> None:
    """Reject impossible combinations before any side effect happens."""
    expose = data["engine"]["expose"]
    if expose not in EXPOSE_MODES:
        raise ConfigError(
            f"[engine].expose must be one of {', '.join(EXPOSE_MODES)} — got {expose!r}"
        )
    for key in ("port", "tls_port"):
        port = data["engine"][key]
        if not isinstance(port, int) or not (1 <= port <= 65535):
            raise ConfigError(f"[engine].{key} must be a port number 1-65535 — got {port!r}")
    if not str(data["distro"].get("name") or "").strip():
        raise ConfigError("[distro].name must not be empty")
    if not str(data["client"].get("context") or "").strip():
        raise ConfigError("[client].context must not be empty")


def resolve(
    path: str | None = None,
    overrides: dict | None = None,
    env: dict[str, str] | None = None,
) -> Config:
    """Build the effective :class:`Config` from all three layers."""
    data = deep_merge(DEFAULTS, load_toml(path))
    data = apply_env(data, env)
    if overrides:
        data = deep_merge(data, {k: v for k, v in overrides.items() if v})
    validate(data)
    return Config(
        distro=data["distro"],
        engine=data["engine"],
        client=data["client"],
        keepalive=data["keepalive"],
        tls=data["tls"],
        apt=data["apt"],
        vhdx=data["vhdx"],
    )
