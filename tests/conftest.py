import pathlib
import sys
import types

import pytest

# Make the package importable without an install, so a bare `pytest` from the
# repo root works before `pip install -e .`.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))


@pytest.fixture(autouse=True)
def virtual_clock(monkeypatch):
    """Replace real waiting with a clock that advances when something sleeps.

    bosun polls with real backoff — apt locks, the engine's API, WSL restarts —
    and those retry budgets are deliberately generous because the operations
    they guard genuinely take minutes. Left alone the suite spends those minutes
    asleep: it ran in three minutes before this fixture and in seconds after.

    Patching ``time.sleep`` alone is not enough. The loops bound themselves with
    ``time.monotonic``, so a no-op sleep turns a patient wait into a hot spin for
    exactly as long. Advancing a virtual ``monotonic`` from ``sleep`` keeps every
    timeout and attempt count behaving exactly as it does in production, just
    without the waiting — so a test asserting "gives up after 60s" still means it.

    Patched per-module rather than on the ``time`` module itself, so pytest's own
    timing stays honest.
    """
    state = {"now": 0.0}

    fake = types.SimpleNamespace(
        monotonic=lambda: state["now"],
        sleep=lambda seconds: state.__setitem__("now", state["now"] + seconds),
        time=lambda: state["now"],
    )

    for module in ("bosun.provision", "bosun.distro", "bosun.exec"):
        monkeypatch.setattr(f"{module}.time", fake, raising=False)


@pytest.fixture(autouse=True)
def endpoint_reachable(monkeypatch):
    """Pin the Windows-side socket probe so tests never touch the network.

    bosun checks the engine's endpoint with a real socket connect, because that
    is the only way to learn what Windows actually sees. Left alone the suite
    would dial 127.0.0.1:2375 on the developer's own machine — slow, and it
    would pass or fail depending on whether they happen to be running docker.

    Defaults to reachable. A test that cares about the failure overrides it:

        monkeypatch.setattr("bosun.client.socket.create_connection", boom)
    """

    class _Socket:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    monkeypatch.setattr(
        "bosun.client.socket.create_connection", lambda *_a, **_k: _Socket(), raising=False
    )
