# bosun

A small CLI that sets up WSL with a container engine on Windows, so you get a
working `docker` without Docker Desktop.

I needed this on my own machines and got tired of redoing the steps by hand.
Sharing it in case it saves someone else the afternoon. It does what I needed
and not much more.

The steps it automates: install a distro, create a user, turn on systemd,
install the engine, stop its unit file from fighting its own config file, expose
an endpoint, grant socket access, wire up a client context. None of it is
secret — it's all in various docs — it's just tedious, and a couple of the
failure modes are genuinely confusing if you haven't hit them before.

```console
$ bosun up
engine: docker    exposure: tcp
distro ready: Ubuntu-24.04
user dev exists
docker already installed
/etc/docker/daemon.json already correct
docker is responding

Done.
  distro:   Ubuntu-24.04
  engine:   docker (tcp)
  endpoint: tcp://127.0.0.1:2375
  context:  bosun
```

## Getting started with pixi

```console
# 1. install bosun
pixi global install bosun --git https://github.com/Krande/bosun

# 2. provision (asks for a Linux username the first time)
bosun up

# 3. check it worked
bosun status
docker ps
```

`bosun up` is safe to re-run, so if something drifts after a Windows update or a
WSL reset, run it again.

To pin a version:

```console
pixi global install bosun --git https://github.com/Krande/bosun --tag v0.1.0
```

To skip the prompt entirely:

```console
bosun up --user dev --no-prompt
```

## Commands

| Command | What it does |
| --- | --- |
| `bosun up` | Provision the distro into a working container host. Safe to re-run. |
| `bosun status` | Report what works and what does not. Changes nothing. |
| `bosun shrink` | Reclaim disk space from the distro's virtual disk. |
| `bosun keepalive` | Hold the distro open so the endpoint keeps answering. |
| `bosun down` | Unregister the distro. Destructive; asks first. |
| `bosun config` | Print the resolved settings and exit. |

All of them take `--dry-run` (reads execute, writes are only printed) and
`--verbose` (echo each command).

### `bosun status`

Checks run in dependency order, so the first failure is usually the thing to fix
and the ones below it are consequences:

```console
$ bosun status
bosun status - distro 'Ubuntu-24.04', engine 'docker'

  ✓ distro registered ......................... ok  (Ubuntu-24.04)
  ✓ distro starts ............................. ok
  ✓ default user .............................. ok  (dev)
  ✓ systemd is PID 1 .......................... ok
  ✓ docker installed .......................... ok  (/usr/bin/docker)
  ✗ docker service active ..................... FAIL  (inactive)
  ✗ dev in the docker group ................... FAIL  (rootless access needs this)
  ...

First failure: docker service active
Run 'bosun up' to repair, or 'bosun up --verbose' to see each step.
```

(It falls back to `+`/`x` markers when the console can't encode the glyphs,
which on Windows is any time you pipe the output.)

### `bosun keepalive`

The one that stops `docker` mysteriously dying.

WSL idles an instance out about a minute after its last Windows-side command,
and the localhost forward to the engine dies with it. So `docker` works right
after `bosun up` and then stops — and every command you run to investigate
wakes the distro back up and reports everything healthy, which is what makes it
so confusing.

`bosun up` turns this on by default. To inspect or change it:

```console
$ bosun keepalive
keep-alive: armed, supervised, re-armed at logon

$ bosun keepalive off     # release it; WSL idles the distro out in ~a minute
$ bosun keepalive on      # hold it again, now and after every logon
```

It holds the distro by running `dbus-launch` through `wsl.exe` — the one
command that both satisfies WSL's idle accounting and returns immediately.
Most of the obvious alternatives simply do not work:

| Attempt | Result |
| --- | --- |
| `.wslconfig` `[experimental] vmIdleTimeout` | unknown key |
| `.wslconfig` `[wsl2] vmIdleTimeout=-1` | no effect |
| a systemd unit running `sleep infinity` | no effect |
| `wsl.conf` `[boot] command=dbus-launch true` | no effect |
| `wsl.exe --exec dbus-launch true` from Windows | **works** |

`vmIdleTimeout` cannot help by construction — it governs the virtual machine,
whose timer only starts once every instance has already terminated. WSL counts
only processes created by a Windows-side `wsl.exe` invocation; anything started
inside the instance is ignored ([microsoft/WSL#13416](https://github.com/microsoft/WSL/issues/13416),
fine on 2.5.10, broken from 2.6.1 on).

A small supervisor keeps watch and re-arms after a `wsl --shutdown`, registered
as an HKCU `Run` value — no administrator rights, and Task Manager lists it
under Startup apps, so bosun is not the only way to see or stop it. It logs to
`%LOCALAPPDATA%\bosun-keepalive.log`.

Turn it off with `[keepalive] enabled = false` if you would rather manage it
yourself.

### `bosun shrink`

A WSL virtual disk grows to fit what you put in it and never shrinks back.
Delete a hundred gigabytes of image layers and the filesystem inside reports the
space as free while `ext4.vhdx` stays exactly as large as it ever got.

`bosun shrink` frees space inside the distro first (`apt-get clean`, an image
prune, then `fstrim`) and only then compacts the image. Order matters:
compaction reclaims only blocks the filesystem has already released.

Compaction needs an **elevated prompt** — `diskpart` can't attach the disk
otherwise. `--clean-only` does just the in-distro half and needs no elevation.

## Configuration

bosun runs fine with no config file. If you want one, settings resolve from
three layers, most-specific first:

```
CLI flag  ->  env var (BOSUN_*)  ->  bosun.toml  ->  built-in default
```

Copy [`bosun.toml.example`](bosun.toml.example) to `bosun.toml` and keep only
the lines you change. A real config is usually short:

```toml
[distro]
user = "dev"

[engine]
expose = "tls"
```

`bosun config` prints what a given setup actually resolves to, which is the
quickest way to find out why something isn't taking effect.

Env vars: `BOSUN_TOML`, `BOSUN_DISTRO`, `BOSUN_USER`, `BOSUN_ENGINE`,
`BOSUN_EXPOSE`, `BOSUN_HOST`, `BOSUN_PORT`, `BOSUN_TLS_PORT`, `BOSUN_CONTEXT`,
`BOSUN_INSTALL_CLI`, `BOSUN_KEEPALIVE`, `BOSUN_VHDX`.

## Exposure modes

`[engine].expose` decides how Windows reaches the engine. Worth picking
deliberately, because reaching a container engine's API is equivalent to root on
the machine.

| Mode | Endpoint | Who can use it |
| --- | --- | --- |
| `tcp` (default) | `tcp://127.0.0.1:2375` | Any process running as you. No authentication. |
| `tls` | `tcp://127.0.0.1:2376` | Only holders of a client certificate bosun issues. |
| `unix` | none | Nothing outside the distro. Use `wsl -- docker ...`. |

`tcp` on loopback is the convenient default and is what a single-user dev
machine usually wants. Use `tls` on a shared or untrusted host, or any time you
move `host` off `127.0.0.1` — binding elsewhere without TLS publishes root on
your machine to that network.

In `tls` mode bosun acts as its own small CA: it generates the CA, a server
certificate and a client certificate inside the distro, then exports only the
client half to `%USERPROFILE%\.docker`. The CA key and server key never leave
the distro. Subjects come from `[tls]`, and the defaults name no person,
organisation or country.

## Privileges

bosun doesn't create a sudoers drop-in and doesn't ask for a Linux password to
provision.

`wsl.exe -u root` already gives an unauthenticated root shell inside a distro —
the Windows user owns the distro image, and WSL doesn't pretend otherwise. So
privileged steps just run as root. The alternative (which my original script
used) is to prompt for a sudo password, write a temporary `NOPASSWD` entry into
`/etc/sudoers.d`, and remove it in a `finally` — same access, but it collects a
secret to get there and leaves the machine weakened if the process dies at the
wrong moment.

The one remaining password prompt is for setting a *new* Linux user's password,
for your own later convenience. bosun never needs it and never stores it.

## Layout

```
src/bosun/
  cli.py          argparse surface
  flows.py        up / down / status / shrink
  config.py       the three-layer config stack          (pure)
  engines.py      per-engine profiles                   (pure)
  wslconf.py      /etc/wsl.conf editing                 (pure)
  daemonjson.py   daemon.json desired state             (pure)
  exec.py         the only module that imports subprocess
  distro.py       find / install / register / users
  provision.py    apt, engine install, daemon, service
  tls.py          certificate generation and export
  client.py       Windows CLI and context
  vhdx.py         disk discovery and compaction
  diagnose.py     the status checks
```

Decisions live in pure functions and the process layer is one adapter passed in
as an argument, which means the tests can run the whole `up` flow against a fake
runner on a Linux CI box with no WSL involved.

The engine is a table entry rather than an assumption. Docker is implemented.
Podman is registered as a known name that isn't wired up, so asking for it fails
immediately rather than half-provisioning a machine — its rootless model needs a
per-user socket unit instead of a system daemon with a `hosts` array, which is
more than a table entry's worth of work.

## Development

```console
git clone https://github.com/Krande/bosun
cd bosun
pixi run test      # pytest
pixi run lint      # ruff check + format --check
```

Or without pixi: `pip install -e ".[dev]" && pytest -q`.

`pixi.lock` is committed, so the dev environment resolves identically for
everyone and CI runs the same pytest and ruff you do. CI checks the lock with
`--locked`, which fails if it has drifted from `pyproject.toml` — so if you
change a dependency, commit the regenerated lock alongside it.

The suite uses a virtual clock — `sleep` advances a fake `monotonic` instead of
actually waiting — so the real retry budgets stay meaningful without the suite
sitting through them.

CI, PR checks and releases run on [deputy](https://github.com/Krande/deputy);
see `deputy.toml`.

PR titles must be conventional commits, and deputy accepts exactly three types:
`feat` (minor bump), `fix` (patch), `chore` (no bump), each with an optional
scope. Anything else fails the check — so `chore(ci):` rather than `ci:`, and
`chore(docs):` rather than `docs:`. A PR also needs exactly one `release-*`
label.

## License

MIT — see [LICENSE](LICENSE).
