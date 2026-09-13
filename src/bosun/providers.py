"""Managed-Kubernetes providers — the seam that keeps the kube support generic.

``kubectl`` itself is provider-neutral: it comes from the Kubernetes project's
own apt repository and talks to any cluster. What is *not* neutral is getting a
kubeconfig for a hosted cluster — each cloud ships its own CLI, its own login
flow, and sometimes its own credential plugin.

So the cloud-specific parts live in a :class:`Provider` table and nothing in
:mod:`bosun.kube` names a cloud directly. Adding a second provider is a table
entry plus its access class, not an edit to the install logic.

Azure is implemented. The others are registered as known names that are not
wired up, so asking for one fails immediately rather than installing half a
toolchain — the same contract :mod:`bosun.engines` uses for podman.
"""

from __future__ import annotations

from dataclasses import dataclass, field

KEYRING_DIR = "/etc/apt/keyrings"


@dataclass(frozen=True)
class AptSource:
    """A third-party apt repository, and the key that signs it."""

    key_url: str
    keyring: str
    list_file: str
    # $ARCH and $CODENAME are expanded by the distro's own shell at call time.
    list_line: str
    packages: tuple[str, ...]


@dataclass(frozen=True)
class Provider:
    """Everything bosun needs to reach one cloud's managed Kubernetes."""

    name: str
    #: Human-readable, for messages and `bosun kube --help`.
    title: str
    #: The provider's own CLI, as invoked inside the distro.
    cli: str
    #: Where that CLI comes from. None when it is not installed via apt.
    apt_source: AptSource | None = None
    #: Extra binaries the provider needs that no apt repository ships.
    extra_tools: tuple[str, ...] = field(default_factory=tuple)
    #: Directories under the Linux user's home that hold this provider's
    #: credentials. They must be owned by that user, never by root.
    credential_dirs: tuple[str, ...] = field(default_factory=tuple)
    implemented: bool = True


AZURE = Provider(
    name="azure",
    title="Azure Kubernetes Service",
    cli="az",
    apt_source=AptSource(
        key_url="https://packages.microsoft.com/keys/microsoft.asc",
        keyring=f"{KEYRING_DIR}/microsoft.gpg",
        list_file="/etc/apt/sources.list.d/azure-cli.list",
        list_line=(
            "deb [arch=$ARCH signed-by={keyring}] "
            "https://packages.microsoft.com/repos/azure-cli/ $CODENAME main"
        ),
        packages=("azure-cli",),
    ),
    # kubelogin is not optional for an Entra-ID-integrated cluster, and no apt
    # repository carries it. `az aks get-credentials` writes an *exec*
    # kubeconfig naming this binary, because kubectl removed its own built-in
    # azure auth provider in 1.26 — without it every command fails with
    # "no Auth Provider found for name azure".
    extra_tools=("kubelogin",),
    credential_dirs=(".azure", ".kube"),
)

# Registered, not wired up. Each needs its own login flow and credential
# plugin, which is real work rather than a table entry — so bosun refuses
# rather than installing a CLI it cannot then use.
AWS = Provider(
    name="aws",
    title="Amazon Elastic Kubernetes Service",
    cli="aws",
    credential_dirs=(".aws", ".kube"),
    implemented=False,
)

GOOGLE = Provider(
    name="google",
    title="Google Kubernetes Engine",
    cli="gcloud",
    credential_dirs=(".config/gcloud", ".kube"),
    implemented=False,
)

PROVIDERS: dict[str, Provider] = {p.name: p for p in (AZURE, AWS, GOOGLE)}


class UnsupportedProvider(ValueError):
    """The requested provider is unknown, or known but not implemented yet."""


def implemented_names() -> list[str]:
    return sorted(p.name for p in PROVIDERS.values() if p.implemented)


def get(name: str) -> Provider:
    """Look up a provider by name, failing before any side effect."""
    provider = PROVIDERS.get(name.strip().lower())
    if provider is None:
        raise UnsupportedProvider(
            f"unknown Kubernetes provider {name!r} - bosun knows: {', '.join(sorted(PROVIDERS))}"
        )
    if not provider.implemented:
        raise UnsupportedProvider(
            f"provider {provider.name!r} ({provider.title}) is not implemented yet; "
            f"bosun currently supports {', '.join(implemented_names())}"
        )
    return provider
