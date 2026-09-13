"""bosun — sets up WSL with a container engine on Windows.

The steps it automates are all documented somewhere; they are just tedious to
repeat by hand. Install a distro, create a user, turn on systemd, install the
engine, stop its unit file from fighting its own config file, expose an
endpoint, grant socket access, wire up a client context. A couple of the failure
modes are confusing the first time you meet them — a ``hosts`` array in
daemon.json and the packaged unit's ``-H fd://`` both specify listeners, and the
daemon's response is to refuse to start while mentioning neither.

Commands: ``up`` (provision), ``status`` (report, changing nothing), ``shrink``
(reclaim disk space from the virtual disk), ``down`` (unregister the distro).

Layout: pure decision logic with no I/O (:mod:`bosun.config`,
:mod:`bosun.wslconf`, :mod:`bosun.daemonjson`, :mod:`bosun.engines`), plus one
injectable process adapter (:mod:`bosun.exec`). :mod:`bosun.flows` wires them
together and takes every side-effecting dependency as an argument, so the tests
run on a Linux CI box with no WSL involved.

The container engine is a table entry rather than an assumption — see
:mod:`bosun.engines`. Docker is implemented; the seam is there for the rest.
"""

__version__ = "0.2.0"

__all__ = ["__version__"]
