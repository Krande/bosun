"""The four commands, wired from the adapters.

Each flow takes every side-effecting dependency as an argument — the process
runner, the logger, the confirmation prompt — so the whole surface runs under
pytest with fakes. Nothing here imports subprocess, reads argv or calls print
directly.
"""

from __future__ import annotations

import pathlib
from collections.abc import Callable

from . import client, diagnose, distro, engines, provision, tls, vhdx
from . import keepalive as keepalive_mod
from .config import Config
from .exec import BosunError, Runner, Wsl

Logger = Callable[[str], None]
Confirm = Callable[[str], bool]


def up(
    runner: Runner,
    cfg: Config,
    log: Logger,
    *,
    prompt: bool = True,
    home: pathlib.Path | None = None,
) -> int:
    """Provision the distro into a working container host.

    Idempotent by construction: every step checks the live state first, so a
    second run on a healthy machine changes nothing and restarts nothing.
    """
    spec = engines.get(cfg.engine_name)
    log(f"engine: {spec.name}    exposure: {cfg.expose}")

    name = distro.ensure_ready(runner, cfg, log)
    wsl = Wsl(runner, name)

    user = distro.resolve_user(wsl, cfg, prompt=prompt)
    distro.ensure_user(wsl, user, log, prompt=prompt)

    # wsl.conf governs systemd and the default user, and only takes effect on a
    # cold start — so it is written before anything depends on it.
    if distro.configure_wsl_conf(wsl, cfg, user, log):
        distro.restart(wsl, runner, log)

    if cfg.distro.get("systemd", True) and not distro.systemd_active(wsl):
        raise BosunError(
            f"systemd is enabled in /etc/wsl.conf but is not PID 1 in {name!r}. "
            "Run 'wsl --shutdown', wait a few seconds, then re-run 'bosun up'. "
            "If it persists, the WSL version may predate systemd support "
            "(needs 0.67.6 or newer — check with 'wsl --version')."
        )

    provision.ensure_engine(wsl, cfg, spec, log)

    cert_dir: pathlib.Path | None = None
    if cfg.tls_enabled:
        provision.apt_install(wsl, cfg, ["openssl"], log)
        tls.generate(wsl, cfg, log)
        cert_dir = tls.export_to_windows(wsl, cfg, spec, log, home)

    daemon_changed = provision.configure_daemon(wsl, cfg, spec, log)
    if daemon_changed or not provision.wait_for_engine(wsl, spec, log, attempts=1, delay=0):
        provision.restart_engine(wsl, spec, log)

    # Group membership is only read at login, so a change here means the distro
    # has to be restarted before the user can reach the socket without root.
    if distro.ensure_in_group(wsl, user, spec.group, log):
        distro.restart(wsl, runner, log)
        provision.wait_for_engine(wsl, spec, log)

    provision.verify(wsl, spec, log)

    # The engine can be listening inside the distro while Windows still cannot
    # reach it: WSL's localhost relay misses listeners that came up during the
    # distro's own boot. Restarting the service once the distro is fully up is
    # what makes the relay notice — so check from the Windows side and repair.
    if cfg.expose != "unix" and not client.endpoint_reachable(cfg):
        log("endpoint not reachable from Windows yet; restarting the engine")
        provision.restart_engine(wsl, spec, log)
        if not client.endpoint_reachable(cfg):
            log(
                f"warning: {cfg.endpoint} is still not reachable from Windows. "
                f"The engine is running inside {name}; try 'wsl --shutdown' and re-run."
            )

    if client.ensure_cli(runner, cfg, spec, log):
        client.ensure_context(runner, cfg, spec, log, cert_dir)
        # `docker compose` / `docker buildx` are separate executables the CLI
        # discovers in a plugins directory; installing them onto PATH is not
        # enough on its own.
        client.wire_plugins(runner, cfg, spec, log, home)

    # Last, because it is what keeps everything above it true: WSL idles the
    # instance out about a minute after the last Windows-side command, taking
    # the endpoint with it.
    if cfg.keepalive.get("enabled", True):
        # dbus-launch is the one command that both satisfies WSL's idle
        # accounting and returns immediately; a minimal image may not ship it.
        if not wsl.ok("command -v dbus-launch", user="root", timeout=20):
            provision.apt_install(wsl, cfg, [keepalive_mod.KEEPALIVE_PACKAGE], log, check=False)
        keepalive_mod.KeepAlive(runner, log).enable(wsl, name)

    _summarise(cfg, spec, name, log)
    return 0


def _summarise(cfg: Config, spec: engines.EngineSpec, name: str, log: Logger) -> None:
    log("")
    log("Done.")
    log(f"  distro:   {name}")
    log(f"  engine:   {spec.name} ({cfg.expose})")
    if cfg.expose != "unix":
        log(f"  endpoint: {cfg.endpoint}")
        log(f"  context:  {cfg.context}")
    log("")
    log("  Verify:")
    log(f"    wsl -d {name} -- {spec.name} info")
    if cfg.expose != "unix":
        log(f"    {spec.host_cli} --context {cfg.context} info")


def down(
    runner: Runner,
    cfg: Config,
    log: Logger,
    confirm: Confirm,
    *,
    assume_yes: bool = False,
) -> int:
    """Unregister the distro, destroying everything inside it.

    Guarded by an explicit confirmation because this is unrecoverable: images,
    volumes, and any work left in the distro's filesystem all go with it, and
    there is no WSL equivalent of a recycle bin.
    """
    name = distro.find(runner, cfg)
    if name is None:
        log(f"no WSL distro matching {cfg.distro_name!r} is registered; nothing to do")
        return 0

    if not assume_yes and not confirm(
        f"Unregister WSL distro {name!r}? This permanently deletes everything in it"
    ):
        log("aborted")
        return 1

    spec = engines.ENGINES.get(cfg.engine_name)
    if spec is not None:
        client.remove_context(runner, cfg, spec, log)

    log(f"unregistering {name}")
    res = distro.unregister(runner, name)
    if not res.ok:
        raise BosunError(f"could not unregister {name}: {res.stderr.strip() or res.stdout.strip()}")

    log(f"{name} unregistered")
    return 0


def status(runner: Runner, cfg: Config, log: Logger) -> int:
    """Report the health of the setup. Exit 0 when every required check passes."""
    spec = engines.get(cfg.engine_name)
    log(f"bosun status — distro {cfg.distro_name!r}, engine {spec.name!r}")
    log("")
    checks = diagnose.run_checks(runner, cfg, spec)
    log(diagnose.render(checks))
    log("")

    if diagnose.healthy(checks):
        log("All required checks passed.")
        return 0

    first = next((c for c in checks if c.required and not c.ok), None)
    if first is not None:
        log(f"First failure: {first.name}")
        log("Run 'bosun up' to repair, or 'bosun up --verbose' to see each step.")
    return 1


def shrink(
    runner: Runner,
    cfg: Config,
    log: Logger,
    *,
    clean_only: bool = False,
    vhdx_path: str | None = None,
) -> int:
    """Reclaim disk space from the distro's virtual disk."""
    spec = engines.get(cfg.engine_name)
    name = distro.find(runner, cfg)
    if name is None:
        raise BosunError(f"no WSL distro matching {cfg.distro_name!r} is registered")

    wsl = Wsl(runner, name)
    provision_ok = distro.is_launchable(runner, name)
    if provision_ok:
        vhdx.reclaim_inside(wsl, spec, log)
    else:
        log(f"{name} will not start; skipping the in-distro cleanup")

    if clean_only:
        log("cleaned inside the distro; skipping the image compaction (--clean-only)")
        return 0

    if vhdx_path:
        cfg = Config(
            distro=cfg.distro,
            engine=cfg.engine,
            client=cfg.client,
            tls=cfg.tls,
            apt=cfg.apt,
            vhdx={**cfg.vhdx, "path": vhdx_path},
        )

    path = vhdx.resolve_path(cfg)
    before = vhdx.size_gb(path)
    vhdx.compact(runner, path, log)
    after = vhdx.size_gb(path)

    log("")
    log(f"  {path}")
    log(f"  {before:.1f} GB -> {after:.1f} GB  (reclaimed {max(0.0, before - after):.1f} GB)")
    return 0


def keepalive(
    runner: Runner,
    cfg: Config,
    log: Logger,
    action: str,
    *,
    distro_name: str | None = None,
) -> int:
    """Turn the keep-alive on or off, report it, or run the supervisor."""
    keeper = keepalive_mod.KeepAlive(runner, log)
    name = distro_name or distro.find(runner, cfg)

    if action == "supervise":
        if name is None:
            raise BosunError("no distro to supervise")
        return keeper.supervise(name)

    if name is None:
        log(f"no WSL distro matching {cfg.distro_name!r} is registered")
        return 1

    wsl = Wsl(runner, name)
    running = distro.is_launchable(runner, name)

    if action == "on":
        keeper.enable(wsl, name)
    elif action == "off":
        keeper.disable(wsl if running else None, name)

    log(f"keep-alive: {keeper.describe(wsl, running)}")
    return 0
