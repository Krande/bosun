"""daemon.json state: merge rather than clobber, and only write when needed."""

from __future__ import annotations

import json

from bosun import daemonjson
from bosun.config import resolve
from bosun.engines import DOCKER


def cfg(**engine):
    return resolve(overrides={"engine": engine} if engine else None)


def test_tcp_mode_exposes_unix_and_loopback():
    want = daemonjson.desired(cfg(), DOCKER)
    assert want["hosts"] == ["unix:///var/run/docker.sock", "tcp://127.0.0.1:2375"]
    assert "tlsverify" not in want


def test_unix_mode_exposes_nothing_to_windows():
    want = daemonjson.desired(cfg(expose="unix"), DOCKER)
    assert want == {"hosts": ["unix:///var/run/docker.sock"]}


def test_tls_mode_uses_the_tls_port_and_cert_paths():
    want = daemonjson.desired(cfg(expose="tls"), DOCKER)
    assert "tcp://127.0.0.1:2376" in want["hosts"]
    assert want["tlsverify"] is True
    assert want["tlscacert"].endswith("/ca.pem")
    assert want["tlskey"].endswith("/server-key.pem")


def test_tls_binds_to_the_configured_host_not_everything():
    """A daemon on 0.0.0.0 is reachable from any network the laptop joins."""
    want = daemonjson.desired(cfg(expose="tls"), DOCKER)
    assert not any("0.0.0.0" in h for h in want["hosts"])


def test_merge_preserves_unmanaged_keys():
    current = {
        "registry-mirrors": ["https://mirror.example"],
        "data-root": "/mnt/big",
        "hosts": ["unix:///var/run/docker.sock"],
    }
    merged = daemonjson.merge(current, daemonjson.desired(cfg(), DOCKER))
    assert merged["registry-mirrors"] == ["https://mirror.example"]
    assert merged["data-root"] == "/mnt/big"
    assert "tcp://127.0.0.1:2375" in merged["hosts"]


def test_switching_away_from_tls_removes_the_tls_keys():
    """Leftover tls* keys make the daemon demand certs the client stopped sending."""
    tls_state = daemonjson.merge({}, daemonjson.desired(cfg(expose="tls"), DOCKER))
    back_to_tcp = daemonjson.merge(tls_state, daemonjson.desired(cfg(), DOCKER))
    assert "tlsverify" not in back_to_tcp
    assert "tlscacert" not in back_to_tcp


def test_no_update_needed_when_already_correct():
    """The guard that stops `bosun up` restarting the engine on every run."""
    rendered = daemonjson.render("", cfg(), DOCKER)
    assert not daemonjson.needs_update(rendered, cfg(), DOCKER)


def test_update_needed_when_port_changes():
    rendered = daemonjson.render("", cfg(), DOCKER)
    assert daemonjson.needs_update(rendered, cfg(port=2999), DOCKER)


def test_malformed_json_is_treated_as_empty():
    assert daemonjson.parse("{not json") == {}
    assert daemonjson.parse("") == {}
    assert daemonjson.parse("[1, 2]") == {}
    assert daemonjson.needs_update("{not json", cfg(), DOCKER)


def test_render_is_valid_json():
    parsed = json.loads(daemonjson.render('{"data-root": "/mnt/big"}', cfg(), DOCKER))
    assert parsed["data-root"] == "/mnt/big"
    assert parsed["hosts"]


def test_systemd_override_blanks_exec_start_first():
    """Without the empty ExecStart= line systemd appends rather than replaces."""
    override = daemonjson.systemd_override(DOCKER)
    assert "ExecStart=\nExecStart=/usr/bin/dockerd" in override
    assert "-H fd://" not in override
