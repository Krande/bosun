"""``bosun status`` — report what is and is not working, without changing anything.

Every check is read-only and independent: a failure earlier in the list never
stops a later check from running, because the useful output is the whole picture
at once rather than the first thing that went wrong.

The checks are ordered as a dependency chain — distro, systemd, engine
installed, service running, group membership, API reachable, endpoint listening,
host CLI, context — so the first ✗ in the list is normally the thing to fix, and
everything below it is a consequence rather than a separate problem.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass

from . import distro as distro_mod
from .config import Config
from .engines import EngineSpec
from .exec import Runner, Wsl, have


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str = ""
    # False for checks that are informational or not applicable in this config,
    # so they do not drag the overall verdict down.
    required: bool = True


def run_checks(runner: Runner, cfg: Config, spec: EngineSpec) -> list[Check]:
    """Run every check and return the results in dependency order."""
    checks: list[Check] = []

    name = distro_mod.find(runner, cfg)
    if name is None:
        registered = distro_mod.list_distros(runner)
        detail = f"registered: {', '.join(registered)}" if registered else "no distros registered"
        return [Check("distro registered", False, detail)]
    checks.append(Check("distro registered", True, name))

    launchable = distro_mod.is_launchable(runner, name)
    checks.append(Check("distro starts", launchable))
    if not launchable:
        return checks

    wsl = Wsl(runner, name)

    conf = wsl.read_file("/etc/wsl.conf")
    user = cfg.user or distro_mod.current_user(wsl) or ""
    checks.append(Check("default user", bool(user), user or "none configured"))

    systemd = distro_mod.systemd_active(wsl)
    checks.append(Check("systemd is PID 1", systemd, "" if systemd else "set [boot] systemd=true"))

    installed = wsl.sh(f"command -v {spec.name}", user="root", timeout=15, read_only=True)
    checks.append(Check(f"{spec.name} installed", installed.ok, installed.out))

    active = wsl.sh(f"systemctl is-active {spec.service}", user="root", timeout=20, read_only=True)
    checks.append(Check(f"{spec.service} service active", active.out == "active", active.out))

    if user:
        in_group = wsl.ok(
            f"id -nG {user} | tr ' ' '\\n' | grep -qx {spec.group}", user="root", timeout=15
        )
        checks.append(
            Check(
                f"{user} in the {spec.group} group",
                in_group,
                "" if in_group else "rootless access needs this; restart WSL after adding",
            )
        )

    api = wsl.ok(f"{spec.name} info", user="root", timeout=25)
    checks.append(Check(f"{spec.name} API responds", api))

    daemon_cfg = wsl.read_file(spec.daemon_json)
    checks.append(
        Check(
            f"{spec.daemon_json} present",
            bool(daemon_cfg.strip()),
            "",
            required=cfg.expose != "unix",
        )
    )

    if cfg.expose == "unix":
        checks.append(
            Check("endpoint exposed to Windows", True, "unix-only by config", required=False)
        )
    else:
        # Checked from inside the distro: WSL's loopback forwarding means a
        # listener bound there is what Windows actually reaches.
        listening = wsl.ok(
            f"timeout 5 bash -c '</dev/tcp/{cfg.engine['host']}/{cfg.port}'",
            user="root",
            timeout=20,
        )
        checks.append(Check(f"port {cfg.port} listening", listening, cfg.endpoint))

    cli = have(spec.host_cli)
    checks.append(Check(f"{spec.host_cli} on the Windows PATH", cli, required=False))

    if cli and cfg.expose != "unix":
        ctx = runner.run(
            [spec.host_cli, "context", "ls", "--format", "{{.Name}}"], timeout=60, read_only=True
        )
        names = {ln.strip() for ln in ctx.stdout.splitlines() if ln.strip()}
        checks.append(
            Check(f"context {cfg.context!r} exists", cfg.context in names, required=False)
        )
        reachable = runner.run(
            [spec.host_cli, "--context", cfg.context, "info"], timeout=60, read_only=True
        ).ok
        checks.append(Check("Windows client reaches the engine", reachable, required=False))

    # Surfaced last because it explains a failure above rather than being one.
    if conf and not distro_mod.systemd_active(wsl):
        checks.append(
            Check(
                "wsl.conf changes applied",
                False,
                "wsl.conf sets systemd but it is not active — run 'wsl --shutdown'",
                required=False,
            )
        )

    return checks


# Status markers, in a pretty form and a form that survives anywhere.
#
# The pretty glyphs are not encodable in cp1252, which is exactly what Python
# picks for stdout on Windows whenever output is piped or the console is not on
# a UTF-8 code page. Printing them there does not degrade — it raises
# UnicodeEncodeError and takes the whole command down. For a tool that is
# Windows-first and whose output people naturally pipe into a file or a pager,
# that is the difference between a diagnostic and a crash report.
GLYPH_MARKS = {"ok": "✓", "fail": "✗", "skip": "–"}
ASCII_MARKS = {"ok": "+", "fail": "x", "skip": "-"}


def pick_marks(stream: object | None = None) -> dict[str, str]:
    """Return the richest marker set the output stream can actually encode."""
    encoding = getattr(stream if stream is not None else sys.stdout, "encoding", None)
    if not encoding:
        return ASCII_MARKS
    try:
        "".join(GLYPH_MARKS.values()).encode(encoding)
    except (LookupError, UnicodeEncodeError):
        return ASCII_MARKS
    return GLYPH_MARKS


def render(checks: list[Check], *, width: int = 42, marks: dict[str, str] | None = None) -> str:
    """Format the results as an aligned table."""
    marks = marks if marks is not None else pick_marks()
    lines = []
    for check in checks:
        mark = marks["ok"] if check.ok else (marks["fail"] if check.required else marks["skip"])
        dots = "." * max(3, width - len(check.name))
        line = f"  {mark} {check.name} {dots} {'ok' if check.ok else 'FAIL'}"
        if check.detail:
            line += f"  ({check.detail})"
        lines.append(line)
    return "\n".join(lines)


def healthy(checks: list[Check]) -> bool:
    """True when every required check passed."""
    return all(c.ok for c in checks if c.required)
