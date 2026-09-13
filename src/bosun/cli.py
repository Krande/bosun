"""Command-line entry point.

    bosun up                       provision the distro into a container host
    bosun status                   report what works and what does not
    bosun shrink                   reclaim disk space from the virtual disk
    bosun down                     unregister the distro (destructive)
    bosun keepalive [on|off]       hold the distro open so the endpoint stays up
    bosun repair                   unstick a WSL install that stopped responding
    bosun kube [status|setup]      kubectl, and a managed-cluster CLI (opt-in)
    bosun config                   print the resolved settings and exit

Settings resolve from three layers, most-specific first::

    CLI flag  ->  env var (BOSUN_*)  ->  bosun.toml  ->  built-in default

Env vars: BOSUN_TOML, BOSUN_DISTRO, BOSUN_USER, BOSUN_ENGINE, BOSUN_EXPOSE,
BOSUN_HOST, BOSUN_PORT, BOSUN_TLS_PORT, BOSUN_CONTEXT, BOSUN_INSTALL_CLI, BOSUN_KEEPALIVE,
BOSUN_KUBERNETES, BOSUN_KUBE_PROVIDER, BOSUN_KUBE_CHANNEL, BOSUN_VHDX.
"""

from __future__ import annotations

import argparse
import json
import sys

from . import __version__, flows
from .config import ConfigError, resolve
from .engines import ENGINES, UnsupportedEngine
from .exec import BosunError, DryRunRunner, SubprocessRunner
from .providers import PROVIDERS, UnsupportedProvider


def _log(message: str) -> None:
    """Print a line, degrading rather than crashing on a legacy code page.

    Windows gives stdout a cp1252 encoding whenever output is piped or the
    console is not on a UTF-8 code page, and a character it cannot represent
    raises UnicodeEncodeError mid-command. Losing a dash is acceptable; losing
    the command is not.
    """
    try:
        print(message, flush=True)
    except UnicodeEncodeError:
        encoding = sys.stdout.encoding or "ascii"
        print(message.encode(encoding, "replace").decode(encoding), flush=True)


def _confirm(question: str) -> bool:
    try:
        return input(f"{question} [y/N]: ").strip().lower() in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        print()
        return False


def _overrides(args: argparse.Namespace) -> dict:
    """Turn the CLI flags into the top layer of the config stack."""
    out: dict = {"distro": {}, "engine": {}, "client": {}, "kubernetes": {}}
    if getattr(args, "distro", None):
        out["distro"]["name"] = args.distro
    if getattr(args, "user", None):
        out["distro"]["user"] = args.user
    if getattr(args, "engine", None):
        out["engine"]["name"] = args.engine
    if getattr(args, "expose", None):
        out["engine"]["expose"] = args.expose
    if getattr(args, "port", None):
        key = "tls_port" if getattr(args, "expose", None) == "tls" else "port"
        out["engine"][key] = args.port
    if getattr(args, "context", None):
        out["client"]["context"] = args.context
    if getattr(args, "provider", None):
        # Naming a provider is itself the opt-in; asking for one and being told
        # Kubernetes is disabled would be a pointless second step.
        out["kubernetes"] = {"provider": args.provider, "enabled": True}
    return {k: v for k, v in out.items() if v}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bosun",
        description="A small CLI that sets up WSL with a container engine on Windows.",
    )
    parser.add_argument("--version", action="version", version=f"bosun {__version__}")

    # Flags every subcommand accepts.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-c", "--config", help="path to bosun.toml")
    common.add_argument("--distro", help="WSL distro name to target")
    common.add_argument("--engine", choices=sorted(ENGINES), help="container engine")
    common.add_argument("-v", "--verbose", action="store_true", help="echo every command")
    common.add_argument(
        "-n", "--dry-run", action="store_true", help="show what would run, changing nothing"
    )

    sub = parser.add_subparsers(dest="command", required=True)

    p_up = sub.add_parser("up", parents=[common], help="provision the distro into a container host")
    p_up.add_argument("--user", help="Linux username to create or reuse")
    p_up.add_argument(
        "--expose", choices=("tcp", "tls", "unix"), help="how Windows reaches the engine"
    )
    p_up.add_argument("--port", type=int, help="port for the engine's TCP endpoint")
    p_up.add_argument("--context", help="name of the client context to create")
    p_up.add_argument(
        "--no-prompt",
        action="store_true",
        help="never prompt; fail if a username is not configured",
    )

    sub.add_parser("status", parents=[common], help="report what works and what does not")

    sub.add_parser("repair", parents=[common], help="unstick a WSL install that stopped responding")

    p_down = sub.add_parser("down", parents=[common], help="unregister the distro (destructive)")
    p_down.add_argument("--context", help="client context to remove alongside the distro")
    p_down.add_argument("-y", "--yes", action="store_true", help="skip the confirmation")

    p_shrink = sub.add_parser("shrink", parents=[common], help="reclaim disk space")
    p_shrink.add_argument("--vhdx", help="path to ext4.vhdx (default: discover it)")
    p_shrink.add_argument(
        "--clean-only", action="store_true", help="clean inside the distro; skip the compaction"
    )

    p_keep = sub.add_parser(
        "keepalive", parents=[common], help="hold the distro open so the endpoint stays up"
    )
    p_keep.add_argument(
        "action",
        nargs="?",
        default="status",
        choices=("status", "on", "off"),
        help="default: status",
    )
    # The logon entry's own mode. Hidden, because it is not something to run by
    # hand: it never returns, and there is no console to stop it from.
    p_keep.add_argument("--supervise", action="store_true", help=argparse.SUPPRESS)

    p_kube = sub.add_parser(
        "kube", parents=[common], help="kubectl, and a managed-cluster CLI (opt-in)"
    )
    p_kube.add_argument(
        "action", nargs="?", default="status", choices=("status", "setup"), help="default: status"
    )
    p_kube.add_argument(
        "--provider",
        choices=sorted(PROVIDERS),
        help="managed Kubernetes provider whose CLI to install",
    )

    sub.add_parser("config", parents=[common], help="print the resolved settings and exit")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        cfg = resolve(args.config, _overrides(args))
    except (ConfigError, UnsupportedEngine, UnsupportedProvider) as exc:
        print(f"bosun: {exc}", file=sys.stderr)
        return 2

    runner = SubprocessRunner(verbose=args.verbose)
    if args.dry_run:
        runner = DryRunRunner(runner)
        _log("dry run — reads execute, writes are only printed")

    try:
        if args.command == "up":
            return flows.up(runner, cfg, _log, prompt=not args.no_prompt)
        if args.command == "status":
            return flows.status(runner, cfg, _log)
        if args.command == "down":
            return flows.down(runner, cfg, _log, _confirm, assume_yes=args.yes)
        if args.command == "shrink":
            return flows.shrink(runner, cfg, _log, clean_only=args.clean_only, vhdx_path=args.vhdx)
        if args.command == "keepalive":
            action = "supervise" if args.supervise else args.action
            return flows.keepalive(runner, cfg, _log, action, distro_name=args.distro)
        if args.command == "repair":
            return flows.repair(runner, cfg, _log)
        if args.command == "kube":
            return flows.kube(runner, cfg, _log, args.action)
        if args.command == "config":
            print(
                json.dumps(
                    {
                        "distro": cfg.distro,
                        "engine": cfg.engine,
                        "client": cfg.client,
                        "keepalive": cfg.keepalive,
                        "kubernetes": cfg.kubernetes,
                        "tls": cfg.tls,
                        "apt": cfg.apt,
                        "vhdx": cfg.vhdx,
                        "resolved": {"endpoint": cfg.endpoint, "port": cfg.port},
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
    except (BosunError, UnsupportedEngine, UnsupportedProvider) as exc:
        print(f"\nbosun: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nbosun: interrupted", file=sys.stderr)
        return 130

    return 2


if __name__ == "__main__":
    sys.exit(main())
