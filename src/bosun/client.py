"""The Windows side: the host CLI and its context.

``bosun up`` finishes by making the engine usable from a normal Windows shell,
which means two things: the client binary is on PATH, and a context points it at
the distro's endpoint. Both are optional — ``[client].install_cli`` and
``[client].manage_context`` turn them off for machines where the CLI is managed
by something else.
"""

from __future__ import annotations

import pathlib
from collections.abc import Callable

from .config import Config
from .engines import EngineSpec
from .exec import Runner, have


def ensure_cli(runner: Runner, cfg: Config, spec: EngineSpec, log: Callable[[str], None]) -> bool:
    """Make sure the host client binary exists. Returns True when usable.

    Installed with whatever ``[client].installer`` names — pixi by default,
    since that is how bosun itself is installed. A missing CLI is a warning
    rather than an error: the engine inside the distro is fully provisioned and
    usable via ``wsl`` regardless, and printing the two commands to run by hand
    beats failing the whole run at the last step.
    """
    if have(spec.host_cli):
        return True

    if not cfg.client.get("install_cli", True):
        log(f"{spec.host_cli} is not on PATH and [client].install_cli is false")
        return False

    installer = str(cfg.client.get("installer") or "pixi")
    if not have(installer):
        log(f"cannot install {spec.host_cli}: {installer} is not on PATH")
        return False

    log(f"installing {' '.join(spec.host_cli_packages)} via {installer}")
    res = runner.run([installer, "global", "install", *spec.host_cli_packages], timeout=1800)
    if not res.ok:
        log(f"warning: {installer} could not install the client: {res.stderr.strip()}")
    return have(spec.host_cli)


def context_exists(runner: Runner, spec: EngineSpec, name: str) -> bool:
    """True when a context called ``name`` is already defined."""
    res = runner.run([spec.host_cli, "context", "ls", "--format", "{{.Name}}"], timeout=60)
    if not res.ok:
        return False
    return name in {ln.strip() for ln in res.stdout.splitlines() if ln.strip()}


def context_endpoint(runner: Runner, spec: EngineSpec, name: str) -> str:
    """The endpoint a context currently points at, or "" if unknown."""
    res = runner.run(
        [spec.host_cli, "context", "inspect", name, "--format", "{{.Endpoints.docker.Host}}"],
        timeout=60,
    )
    return res.out if res.ok else ""


def ensure_context(
    runner: Runner,
    cfg: Config,
    spec: EngineSpec,
    log: Callable[[str], None],
    cert_dir: pathlib.Path | None = None,
) -> bool:
    """Create or update the context and select it. Returns True on success.

    Recreated rather than edited when the endpoint has drifted, because the CLI
    offers no way to change a context's TLS material in place — and a context
    left pointing at a stale port fails in a way that looks like the daemon is
    down.
    """
    if not cfg.client.get("manage_context", True):
        return True
    if cfg.expose == "unix":
        log("expose = unix — nothing to reach from Windows, skipping the context")
        return True
    if not have(spec.host_cli):
        _print_manual_instructions(cfg, spec, log, cert_dir)
        return False

    name = cfg.context
    endpoint = cfg.endpoint
    spec_parts = [f"host={endpoint}"]
    if cfg.tls_enabled:
        if cert_dir is None:
            raise ValueError("TLS mode requires the exported certificate directory")
        spec_parts += [
            f"ca={cert_dir / 'ca.pem'}",
            f"cert={cert_dir / 'cert.pem'}",
            f"key={cert_dir / 'key.pem'}",
        ]
    docker_arg = ",".join(spec_parts)

    if context_exists(runner, spec, name):
        if context_endpoint(runner, spec, name) == endpoint and not cfg.tls_enabled:
            log(f"context {name!r} already points at {endpoint}")
        else:
            log(f"recreating context {name!r}")
            runner.run([spec.host_cli, "context", "rm", "-f", name], timeout=60)
            runner.run(
                [spec.host_cli, "context", "create", name, "--docker", docker_arg], timeout=60
            )
    else:
        log(f"creating context {name!r} -> {endpoint}")
        res = runner.run(
            [spec.host_cli, "context", "create", name, "--docker", docker_arg], timeout=60
        )
        if not res.ok:
            log(f"warning: could not create the context: {res.stderr.strip()}")
            return False

    res = runner.run([spec.host_cli, "context", "use", name], timeout=60)
    if not res.ok:
        log(f"warning: could not select the context: {res.stderr.strip()}")
        return False
    return True


def _print_manual_instructions(
    cfg: Config, spec: EngineSpec, log: Callable[[str], None], cert_dir: pathlib.Path | None
) -> None:
    """Tell the user exactly what to run once they have a client binary."""
    parts = [f"host={cfg.endpoint}"]
    if cfg.tls_enabled and cert_dir is not None:
        parts += [
            f"ca={cert_dir / 'ca.pem'}",
            f"cert={cert_dir / 'cert.pem'}",
            f"key={cert_dir / 'key.pem'}",
        ]
    log(
        f"{spec.host_cli} is not on PATH, so the context was not created.\n"
        f"    The engine inside the distro is ready — reach it with "
        f"'wsl -d {cfg.distro_name} {spec.name} info'.\n"
        f"    Once a client is installed, run:\n"
        f"      {spec.host_cli} context create {cfg.context} --docker {','.join(parts)}\n"
        f"      {spec.host_cli} context use {cfg.context}"
    )


def remove_context(
    runner: Runner, cfg: Config, spec: EngineSpec, log: Callable[[str], None]
) -> None:
    """Drop the context and fall back to the default one.

    Part of ``bosun down``: leaving a context behind that points at a distro
    that no longer exists makes every later client command fail with a
    connection error rather than an obvious "you removed this".
    """
    if not have(spec.host_cli):
        return
    if not context_exists(runner, spec, cfg.context):
        return
    log(f"removing context {cfg.context!r}")
    runner.run([spec.host_cli, "context", "use", "default"], timeout=60)
    runner.run([spec.host_cli, "context", "rm", "-f", cfg.context], timeout=60)
