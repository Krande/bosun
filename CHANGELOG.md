# CHANGELOG

## v0.2.1 (2026-09-18)


### Chore

* render the changelog so squashed PR bodies stay readable (#2) ([`325484e`](https://github.com/Krande/bosun/commit/325484e56180d656ec67040deaa45f34d0b53925))


### Fix

* stop `bosun up` hanging forever on a first distro install (#3) ([`e50e155`](https://github.com/Krande/bosun/commit/e50e15514a70aba9e061432d5bcb040fbc1471ee))

<details><summary>Details</summary>

On a fresh machine `bosun up` sat on `installing Ubuntu-24.04`
indefinitely and opened a WSL window nobody asked for. Closing the
window changed nothing. Two separate faults combined into one unbounded
wait.

##### wsl.exe does not stop at installing

Since WSL 2.0 it then launches the new distro so its first-boot wizard
can ask for a username and password, and `wsl --install` does not return
until that is answered. Nothing is watching it during a bosun run — that
window was the wizard.

`--no-launch` registers the image and stops there; bosun creates the
account itself a few steps later. `register()` already documented
avoiding that wizard as its whole reason for existing; it just never got
the chance to run, because the install ahead of it never returned.

The launching form is retried only when wsl.exe rejects the flag by
quoting it back (pre-2.0). Any other failure is a real one, and the
winget fallback handles it better than a second attempt that hangs.
Checked against WSL 2.7.3: a genuine error names the distro, not the
option, so the retry does not misfire.

##### The timeout could not expire

`subprocess.run` kills only the direct child on `TimeoutExpired`, then
re-reads the pipes **with no timeout at all**. Helper processes that
inherited those pipes keep them open, so the 1800s budget became a read
that never returned — the `trying winget` fallback was unreachable no
matter how long you waited.

`run()` now drives `Popen` directly, ends the whole process tree
(`taskkill /F /T`, falling back to `proc.kill()`), and bounds the output
collection that follows.

##### Launcher names

`register()` guessed from a hard-coded list. Recent WSL ships a distro
as a plain archive and installs no launcher executable at all, so there
may be nothing to call. wsl.exe is asked first, the launcher is the
fallback, and its name is derived from the distro (`Ubuntu-24.04` →
`ubuntu2404.exe`, `ubuntu.exe`) rather than enumerated. A run that finds
none now says so instead of returning silently.

##### Tests

8 new, 357 passing, ruff clean — the wizard is suppressed; the retry
fires only on a rejected flag; a genuine failure reaches winget without
a second attempt; register prefers wsl over a launcher and falls back
correctly; launcher-name derivation; a timeout ends the tree and keeps
what was written; a child outliving the kill still times out.

🤖 Generated with [Claude Code](https://claude.com/claude-code)

Co-authored-by: Claude Opus 5 (1M context) <noreply@anthropic.com>

</details>


## v0.2.0 (2026-09-13)


### Feature

* keep-alive, kube and repair commands, and seven real-machine fixes (#1) ([`2e1cce3`](https://github.com/Krande/bosun/commit/2e1cce3b3a7f37389f8da3f63e2de70321c0f459))


## v0.1.0 (2026-09-11)


### Feature

* initial bosun CLI for WSL container-host setup ([`032e7e1`](https://github.com/Krande/bosun/commit/032e7e12a38eac5dea9bf4f8ef15838bd466395e))

<details><summary>Details</summary>

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
contributors and CI resolve the same pytest and ruff.

</details>

