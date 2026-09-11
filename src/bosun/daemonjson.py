"""Pure computation of the engine's ``daemon.json`` contents.

Two reasons this is its own module rather than inline in the provisioning flow:

1. It must be *idempotent*. ``bosun up`` is expected to be re-runnable, and
   rewriting daemon.json on every run means restarting the engine on every run,
   which kills running containers. So the flow asks "does the live file already
   say what we want?" and only writes when the answer is no.

2. It must *merge*, not replace. A machine may carry hand-added keys —
   ``registry-mirrors``, ``insecure-registries``, ``data-root``, proxy settings —
   and a tool that clobbers them to set two of its own is a tool you stop
   trusting. bosun owns only the keys it manages and passes the rest through.
"""

from __future__ import annotations

import json
from typing import Any

from .config import Config
from .engines import EngineSpec

# The keys bosun manages. Anything else found in a live daemon.json is preserved
# verbatim; these are the only ones it will overwrite or remove.
MANAGED_KEYS = ("hosts", "tlsverify", "tlscacert", "tlscert", "tlskey")


def desired(cfg: Config, spec: EngineSpec) -> dict[str, Any]:
    """The managed keys as they should appear for this configuration."""
    unix = f"unix://{spec.socket}"
    if cfg.expose == "unix":
        return {"hosts": [unix]}

    if cfg.expose == "tls":
        tls_dir = cfg.tls["dir"].rstrip("/")
        # Bound to the configured host like the plain-TCP path, rather than the
        # 0.0.0.0 the pre-bosun script used. TLS authenticates the caller but
        # does not limit who can reach the port; on a laptop that roams between
        # networks, a daemon listening on every interface is an exposure that
        # mutual TLS only partly offsets. Widen deliberately via [engine].host.
        return {
            "hosts": [unix, f"tcp://{cfg.engine['host']}:{cfg.port}"],
            "tlsverify": True,
            "tlscacert": f"{tls_dir}/ca.pem",
            "tlscert": f"{tls_dir}/server-cert.pem",
            "tlskey": f"{tls_dir}/server-key.pem",
        }

    return {"hosts": [unix, f"tcp://{cfg.engine['host']}:{cfg.port}"]}


def parse(raw: str) -> dict[str, Any]:
    """Parse a live daemon.json, treating malformed content as empty.

    A daemon.json that does not parse is already preventing the engine from
    starting, so bosun replaces it rather than refusing to proceed.
    """
    raw = (raw or "").strip()
    if not raw:
        return {}
    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def merge(current: dict[str, Any], want: dict[str, Any]) -> dict[str, Any]:
    """Apply the managed keys onto ``current``, preserving everything else.

    Managed keys absent from ``want`` are dropped — switching from TLS back to
    plain TCP has to remove the ``tls*`` keys, or the daemon keeps demanding
    certificates the client no longer sends.
    """
    out = {k: v for k, v in current.items() if k not in MANAGED_KEYS}
    out.update(want)
    return out


def needs_update(current_raw: str, cfg: Config, spec: EngineSpec) -> bool:
    """True when the live file does not already express the desired state."""
    current = parse(current_raw)
    return merge(current, desired(cfg, spec)) != current


def render(current_raw: str, cfg: Config, spec: EngineSpec) -> str:
    """The full JSON document to write."""
    merged = merge(parse(current_raw), desired(cfg, spec))
    return json.dumps(merged, indent=2, sort_keys=True) + "\n"


def systemd_override(spec: EngineSpec) -> str:
    """The drop-in that stops the packaged unit from fighting daemon.json.

    The shipped unit starts the daemon with ``-H fd://``. A ``hosts`` array in
    daemon.json is then a duplicate listener specification and the daemon exits
    with "unable to configure the Docker daemon with file ... hosts". Blanking
    ExecStart and restating it without the flag is the documented fix.
    """
    return f"[Service]\nExecStart=\nExecStart={spec.exec_start}\n"
