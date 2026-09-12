"""Optional mutual TLS for the engine's TCP endpoint (``[engine].expose = "tls"``).

The default ``tcp`` mode exposes an unauthenticated API on loopback. Anything
running as the Windows user can reach it, and reaching a container engine's API
is equivalent to root on the machine — so on a shared or untrusted host, or when
the endpoint is bound anywhere other than 127.0.0.1, use this mode instead.

bosun acts as its own small CA: it generates a CA, a server certificate for the
daemon and a client certificate for Windows, all inside the distro, then exports
only the client half to ``%USERPROFILE%``. The CA private key never leaves the
distro. Every part of the certificate subject comes from ``[tls]`` in
bosun.toml, so the shipped default identifies nobody and no organisation.
"""

from __future__ import annotations

import pathlib
from collections.abc import Callable

from .config import Config
from .engines import EngineSpec
from .exec import BosunError, Wsl, is_dry_run

# Exported to Windows. The CA key and the server key are deliberately absent:
# the client needs to verify the daemon and prove itself, nothing more.
CLIENT_FILES = ("ca.pem", "cert.pem", "key.pem")


def openssl_config(cfg: Config) -> str:
    """The x509 extensions file for the server certificate.

    The SANs matter more than the CN here: modern TLS clients ignore the CN
    entirely, so a certificate without a matching SAN is rejected even though
    it looks correct in ``openssl x509 -text``.
    """
    lines = [
        "[req]",
        f"default_bits = {int(cfg.tls['key_bits'])}",
        "distinguished_name = dn",
        "x509_extensions = v3_req",
        "prompt = no",
        "",
        "[dn]",
        f"CN = {cfg.tls['server_cn']}",
        "",
        "[v3_req]",
        "subjectAltName = @alt_names",
        "basicConstraints = critical,CA:FALSE",
        "keyUsage = keyEncipherment,dataEncipherment,digitalSignature",
        "extendedKeyUsage = serverAuth,clientAuth",
        "",
        "[alt_names]",
    ]
    for i, dns in enumerate(cfg.tls.get("san_dns") or [], start=1):
        lines.append(f"DNS.{i} = {dns}")
    for i, ip in enumerate(cfg.tls.get("san_ip") or [], start=1):
        lines.append(f"IP.{i} = {ip}")
    return "\n".join(lines) + "\n"


def certs_exist(wsl: Wsl, cfg: Config) -> bool:
    tls_dir = cfg.tls["dir"].rstrip("/")
    files = " ".join(f"-f {tls_dir}/{n}" for n in ("ca.pem", "server-cert.pem", "cert.pem"))
    return wsl.ok(f"test {files.replace('-f', '-f ').strip()}", user="root", timeout=15)


def generate(wsl: Wsl, cfg: Config, log: Callable[[str], None], *, force: bool = False) -> None:
    """Create the CA, server and client certificates inside the distro.

    Each step is guarded by an existence test, so re-running ``bosun up`` reuses
    the existing CA rather than minting a new one — which would invalidate every
    client already configured against it.
    """
    tls_dir = cfg.tls["dir"].rstrip("/")
    bits = int(cfg.tls["key_bits"])
    ca_days = int(cfg.tls["ca_days"])
    cert_days = int(cfg.tls["cert_days"])

    if certs_exist(wsl, cfg) and not force:
        log("TLS certificates already present")
        return

    log("generating TLS certificates")
    res = wsl.sh("command -v openssl", user="root", timeout=15, read_only=True)
    if not res.ok:
        raise BosunError(
            "openssl is not installed in the distro; bosun installs it as part of "
            "provisioning, so this means an earlier step was skipped"
        )

    wsl.sh(f"install -d -m 0700 -o root -g root {tls_dir}", user="root", timeout=30)
    wsl.write_file(f"{tls_dir}/openssl.cnf", openssl_config(cfg), mode="0600")
    wsl.write_file(f"{tls_dir}/client_ext.cnf", "extendedKeyUsage = clientAuth\n", mode="0600")

    steps = [
        # Certificate authority.
        (f"[ -f {tls_dir}/ca-key.pem ] || openssl genrsa -out {tls_dir}/ca-key.pem {bits}", 120),
        (
            f"[ -f {tls_dir}/ca.pem ] || openssl req -x509 -new -nodes "
            f"-key {tls_dir}/ca-key.pem -sha256 -days {ca_days} "
            f"-subj '{cfg.subject(cfg.tls['ca_cn'])}' -out {tls_dir}/ca.pem",
            120,
        ),
        # Server certificate, signed by that CA.
        (
            f"[ -f {tls_dir}/server-key.pem ] || openssl genrsa "
            f"-out {tls_dir}/server-key.pem {bits}",
            120,
        ),
        (
            f"openssl req -new -key {tls_dir}/server-key.pem "
            f"-subj '{cfg.subject(cfg.tls['server_cn'])}' -out {tls_dir}/server.csr",
            60,
        ),
        (
            f"openssl x509 -req -in {tls_dir}/server.csr -CA {tls_dir}/ca.pem "
            f"-CAkey {tls_dir}/ca-key.pem -CAcreateserial -out {tls_dir}/server-cert.pem "
            f"-days {cert_days} -sha256 -extfile {tls_dir}/openssl.cnf -extensions v3_req",
            60,
        ),
        # Client certificate, same CA.
        (f"[ -f {tls_dir}/key.pem ] || openssl genrsa -out {tls_dir}/key.pem {bits}", 120),
        (
            f"openssl req -new -key {tls_dir}/key.pem "
            f"-subj '{cfg.subject(cfg.tls['client_cn'])}' -out {tls_dir}/client.csr",
            60,
        ),
        (
            f"openssl x509 -req -in {tls_dir}/client.csr -CA {tls_dir}/ca.pem "
            f"-CAkey {tls_dir}/ca-key.pem -CAcreateserial -out {tls_dir}/cert.pem "
            f"-days {cert_days} -sha256 -extfile {tls_dir}/client_ext.cnf",
            60,
        ),
        # Private keys readable only by root.
        (f"chmod 600 {tls_dir}/ca-key.pem {tls_dir}/server-key.pem {tls_dir}/key.pem", 15),
        (f"chown -R root:root {tls_dir}", 15),
    ]

    for script, timeout in steps:
        res = wsl.sh(script, user="root", timeout=timeout)
        if not res.ok:
            raise BosunError(f"TLS setup failed:\n  {script}\n{res.stderr.strip()}")

    wsl.sh(f"rm -f {tls_dir}/server.csr {tls_dir}/client.csr", user="root", timeout=15)


def windows_cert_dir(
    cfg: Config, spec: EngineSpec, home: pathlib.Path | None = None
) -> pathlib.Path:
    """Where the client certificates land on the Windows side."""
    configured = str(cfg.tls.get("windows_cert_dir") or "").strip()
    if configured:
        return pathlib.Path(configured).expanduser()
    return (home or pathlib.Path.home()) / spec.cert_dir


def export_to_windows(
    wsl: Wsl,
    cfg: Config,
    spec: EngineSpec,
    log: Callable[[str], None],
    home: pathlib.Path | None = None,
) -> pathlib.Path:
    """Copy the client certificate trio out to Windows and return the directory.

    Read back through ``wsl -u root`` because the client key is mode 0600; the
    content comes over stdout rather than through a shared filesystem path so it
    works regardless of where the distro is stored.
    """
    dest = windows_cert_dir(cfg, spec, home)

    # This is the one place bosun writes to the Windows filesystem rather than
    # through a subprocess, so DryRunRunner cannot intercept it and the check
    # has to be explicit.
    if is_dry_run(wsl.runner):
        log(f"  [dry-run] would export client certificates to {dest}")
        return dest

    dest.mkdir(parents=True, exist_ok=True)
    tls_dir = cfg.tls["dir"].rstrip("/")

    for name in CLIENT_FILES:
        content = wsl.read_file(f"{tls_dir}/{name}")
        if not content.strip():
            raise BosunError(f"could not read {tls_dir}/{name} out of the distro")
        (dest / name).write_text(content, encoding="utf-8")

    log(f"exported client certificates to {dest}")
    return dest
