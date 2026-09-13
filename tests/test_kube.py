"""kubectl and the managed-Kubernetes providers.

Opt-in, and provider-neutral: kubectl comes from the Kubernetes project's own
repository and talks to any cluster, while everything cloud-specific lives in a
provider table. The tests pin both halves — that nothing installs unless asked,
and that no cloud is named outside providers.py.
"""

from __future__ import annotations

import json

import pytest

from bosun import flows, kube, providers
from bosun.config import resolve
from bosun.exec import Result, Wsl
from bosun.kube import Kubectl, ProviderTools, channel_of, minor_of, skew, within_skew
from fakes import FakeRunner, healthy_machine


def cfg(**kubernetes):
    return resolve(overrides={"kubernetes": kubernetes} if kubernetes else None)


def kubectl(runner, **kw):
    return Kubectl(Wsl(runner, "Ubuntu-24.04"), cfg(**kw), lambda _: None)


# ── version arithmetic ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "version,expected",
    [("v1.31.2", (1, 31)), ("1.31", (1, 31)), ("v1.9.0", (1, 9)), (None, None), ("junk", None)],
)
def test_minor_parsing(version, expected):
    assert minor_of(version) == expected


def test_channel_names_the_minor():
    assert channel_of("v1.31.4") == "v1.31"
    assert channel_of(None) is None


def test_skew_is_measured_in_minors():
    assert skew("v1.31.0", "v1.31.9") == 0
    assert skew("v1.32.0", "v1.31.0") == 1
    assert skew("v1.29.0", "v1.31.0") == -2


def test_one_minor_either_side_is_supported():
    assert within_skew("v1.31.0", "v1.30.0")
    assert within_skew("v1.30.0", "v1.31.0")
    assert not within_skew("v1.33.0", "v1.31.0")


def test_unknown_versions_are_not_treated_as_a_skew_failure():
    """Refusing to work because a probe failed is worse than proceeding."""
    assert within_skew(None, "v1.31.0")
    assert within_skew("v1.31.0", None)


# ── the channel problem ────────────────────────────────────────────────────


def test_the_channel_comes_from_upstream_when_reachable():
    """pkgs.k8s.io has no 'latest': a channel must be chosen before the source
    file can even be written."""
    runner = FakeRunner().out("dl.k8s.io", "v1.31.4\n")
    assert kubectl(runner).latest_channel() == "v1.31"


def test_an_unreachable_upstream_falls_back_rather_than_failing():
    runner = FakeRunner().on("dl.k8s.io", Result(1, "", "could not resolve host"))
    logs: list[str] = []
    keeper = Kubectl(Wsl(runner, "Ubuntu-24.04"), cfg(), logs.append)

    assert keeper.latest_channel() == kube.KUBECTL_FALLBACK_CHANNEL
    assert any("realigned" in line for line in logs)


def test_a_configured_channel_wins_over_the_probe():
    runner = FakeRunner().out("dl.k8s.io", "v1.31.4\n").on("command -v kubectl", Result(1))
    kubectl(runner, channel="v1.28").ensure_installed(lambda *a: True, lambda *a: None)
    assert runner.ran("stable:/v1.28/")
    assert not runner.ran("stable:/v1.31/")


def test_the_current_channel_is_read_from_the_source_file():
    runner = FakeRunner().out(
        "cat /etc/apt/sources.list.d/kubernetes.list",
        "deb [signed-by=/etc/apt/keyrings/kubernetes-apt-keyring.gpg] "
        "https://pkgs.k8s.io/core:/stable:/v1.30/deb/ /\n",
    )
    assert kubectl(runner).current_channel() == "v1.30"


# ── skew alignment ─────────────────────────────────────────────────────────


def version_json(v):
    return json.dumps({"clientVersion": {"gitVersion": v}})


def test_a_client_within_skew_is_left_alone():
    runner = FakeRunner().out("kubectl version", version_json("v1.31.0"))
    assert kubectl(runner).align_to_server("v1.30.5", lambda *a: True, lambda *a: None) is False
    assert not runner.ran("stable:/v1.30/")


def test_a_client_out_of_skew_is_realigned_to_the_cluster():
    """Managed offerings trail upstream stable, so the newest kubectl is
    routinely too new for the cluster you actually have."""
    runner = FakeRunner().out("kubectl version", version_json("v1.34.0"))
    assert kubectl(runner).align_to_server("v1.30.5", lambda *a: True, lambda *a: None) is True
    assert runner.ran("stable:/v1.30/")


def test_alignment_needs_a_known_server_version():
    runner = FakeRunner().out("kubectl version", version_json("v1.34.0"))
    assert kubectl(runner).align_to_server(None, lambda *a: True, lambda *a: None) is False


def test_alignment_does_not_rewrite_the_channel_it_already_uses():
    runner = FakeRunner()
    runner.out("kubectl version", version_json("v1.34.0"))
    runner.out("cat /etc/apt/sources.list.d/kubernetes.list", "stable:/v1.30/deb/ /\n")
    assert kubectl(runner).align_to_server("v1.30.5", lambda *a: True, lambda *a: None) is False


# ── the provider seam ──────────────────────────────────────────────────────


def test_azure_is_implemented():
    assert providers.get("azure").cli == "az"


def test_an_unknown_provider_lists_what_is_known():
    with pytest.raises(providers.UnsupportedProvider, match="bosun knows"):
        providers.get("digitalocean")


@pytest.mark.parametrize("name", ["aws", "google"])
def test_a_registered_but_unimplemented_provider_fails_loudly(name):
    """Better to refuse than to install a CLI bosun cannot then use."""
    with pytest.raises(providers.UnsupportedProvider, match="not implemented"):
        providers.get(name)


def test_no_cloud_is_named_outside_the_provider_table():
    """The whole point of the seam: kube.py must stay provider-neutral."""
    import pathlib

    source = pathlib.Path(kube.__file__).read_text(encoding="utf-8").lower()
    for cloud in ("azure", "aks", "kubelogin", "aws", "eks", "gcloud", "gke"):
        assert cloud not in source, f"kube.py names {cloud}; it belongs in providers.py"


def test_azure_declares_its_credential_plugin():
    """kubectl dropped its built-in azure auth provider in 1.26, so an
    Entra-integrated cluster needs kubelogin or every command fails."""
    assert "kubelogin" in providers.AZURE.extra_tools


def test_every_provider_declares_a_kube_credential_dir():
    """~/.kube must be user-owned, or the kubeconfig is invisible to them."""
    for provider in providers.PROVIDERS.values():
        assert ".kube" in provider.credential_dirs


# ── opt-in ─────────────────────────────────────────────────────────────────


def test_kubernetes_is_off_by_default():
    assert resolve().kubernetes["enabled"] is False
    assert resolve().kubernetes["provider"] == ""


def test_up_installs_nothing_when_disabled(monkeypatch):
    monkeypatch.setattr("bosun.client.have", lambda _n: True)
    runner = healthy_machine()
    flows.up(runner, resolve(), lambda _: None, prompt=False)
    assert not runner.ran("kubectl")
    assert not runner.ran("pkgs.k8s.io")


def test_up_installs_kubectl_when_enabled(monkeypatch):
    monkeypatch.setattr("bosun.client.have", lambda _n: True)
    runner = healthy_machine().on("command -v kubectl", Result(1))
    runner.out("dl.k8s.io", "v1.31.4\n")
    flows.up(runner, cfg(enabled=True), lambda _: None, prompt=False)
    assert runner.ran("pkgs.k8s.io")


def test_enabling_without_a_provider_installs_kubectl_only(monkeypatch):
    monkeypatch.setattr("bosun.client.have", lambda _n: True)
    runner = healthy_machine().on("command -v kubectl", Result(1))
    runner.out("dl.k8s.io", "v1.31.4\n")
    flows.up(runner, cfg(enabled=True), lambda _: None, prompt=False)
    assert not runner.ran("packages.microsoft.com")


def test_a_provider_adds_its_own_repository(monkeypatch):
    monkeypatch.setattr("bosun.client.have", lambda _n: True)
    runner = healthy_machine()
    runner.on("command -v kubectl", Result(1)).on("command -v az", Result(1))
    runner.out("dl.k8s.io", "v1.31.4\n")
    flows.up(runner, cfg(enabled=True, provider="azure"), lambda _: None, prompt=False)
    assert runner.ran("packages.microsoft.com")


def test_an_unimplemented_provider_is_rejected_by_config():
    """Fails before anything is installed, rather than after three repos."""
    with pytest.raises(providers.UnsupportedProvider, match="not implemented"):
        resolve(overrides={"kubernetes": {"provider": "aws"}})


def test_an_unknown_provider_is_rejected_by_config():
    with pytest.raises(providers.UnsupportedProvider, match="unknown"):
        resolve(overrides={"kubernetes": {"provider": "nonesuch"}})


def test_the_provider_can_be_set_by_env():
    resolved = resolve(env={"BOSUN_KUBERNETES": "1", "BOSUN_KUBE_PROVIDER": "azure"})
    assert resolved.kubernetes["enabled"] is True
    assert resolved.kubernetes["provider"] == "azure"


def test_the_channel_can_be_set_by_env():
    assert resolve(env={"BOSUN_KUBE_CHANNEL": "v1.29"}).kubernetes["channel"] == "v1.29"


# ── credential ownership ───────────────────────────────────────────────────


def test_credentials_are_handed_back_to_the_linux_user():
    """Installs run as root; a root-owned kubeconfig produces 'connection to
    the server localhost:8080 refused' from the user's own shell."""
    runner = FakeRunner()
    tools = ProviderTools(Wsl(runner, "Ubuntu-24.04"), cfg(), providers.AZURE, lambda _: None)
    tools.fix_credential_ownership("dev")

    assert runner.ran("chown -R dev:dev ~dev/.kube")
    assert runner.ran("chown -R dev:dev ~dev/.azure")


def test_missing_extra_tools_are_reported():
    runner = FakeRunner().on("command -v kubelogin", Result(1))
    tools = ProviderTools(Wsl(runner, "Ubuntu-24.04"), cfg(), providers.AZURE, lambda _: None)
    assert tools.missing_tools() == ["kubelogin"]


# ── the flow ───────────────────────────────────────────────────────────────


def test_kube_status_reports_without_installing():
    runner = healthy_machine().out("command -v kubectl", "/usr/bin/kubectl\n")
    logs: list[str] = []
    flows.kube(runner, cfg(), logs.append, "status")

    assert any("kubectl" in line for line in logs)
    assert not runner.ran("apt-get")


def test_kube_status_needs_a_distro():
    runner = FakeRunner().out("wsl.exe -l -q", "")
    assert flows.kube(runner, cfg(), lambda _: None, "status") == 1


def test_kube_status_says_when_no_provider_is_configured():
    runner = healthy_machine().out("command -v kubectl", "/usr/bin/kubectl\n")
    logs: list[str] = []
    flows.kube(runner, cfg(), logs.append, "status")
    assert any("none configured" in line for line in logs)
