# CHANGELOG



## v0.2.0 (2026-09-13)

### Feature

* feat: keep-alive, kube and repair commands, and seven real-machine fixes (#1) ([`2e1cce3`](https://github.com/Krande/bosun/commit/2e1cce3b3a7f37389f8da3f63e2de70321c0f459))


## v0.1.0 (2026-09-11)

### Feature

* feat: initial bosun CLI for WSL container-host setup

Extracts the WSL + container-engine setup from a personal admin script into
an installable CLI, with the machine-specific bits turned into configuration
so the repo can be public.

Commands: up (provision, idempotent), status (read-only report), shrink
(reclaim vhdx space), down (unregister), config (print resolved settings).

Structure follows deputy: pure decision logic (config, wslconf, daemonjson,
engines) separated from one injectable process adapter (exec), with flows
taking every side-effecting dependency as an argument. 163 tests run against
a fake runner on Linux with no WSL involved, using a virtual clock so the
real retry budgets stay meaningful without the suite waiting them out.

Changes from the original script:

- No sudoers drop-in and no sudo password prompt. Privileged steps run over
  `wsl -u root`, which needs no credentials. The old temporary NOPASSWD entry
  granted the same access but collected a secret and left the machine
  weakened if the process died before its cleanup.
- daemon.json is merged rather than overwritten, and only written when it
  actually differs, so re-running does not restart the engine.
- /etc/wsl.conf is parsed and edited, preserving unrelated sections.
- TLS binds to the configured host rather than 0.0.0.0.
- Certificate subject, username, distro, ports and context are configuration.
- Container engine is a table entry rather than an assumption; podman is
  registered but fails fast as not implemented.
- Status markers fall back to ASCII when the console cannot encode them,
  which on Windows is any time the output is piped.

CI, PR checks and releases are delegated to deputy, pinned to v0.5.6 — below
v0.5.2 a fresh install resolves a GitPython release that breaks tagging
silently. pixi.lock is committed and CI runs through pixi with `--locked`, so
contributors and CI resolve the same pytest and ruff. ([`032e7e1`](https://github.com/Krande/bosun/commit/032e7e12a38eac5dea9bf4f8ef15838bd466395e))
