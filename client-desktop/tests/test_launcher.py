"""The launcher's log filtering.

A browser hanging up made the console print a stack trace labelled ERROR. In an
application where a real fault costs money, log noise that looks like a fault is
not cosmetic — it trains you to skim past the line that matters.

The filter is deliberately narrow, so what is tested here is mostly what it
still lets through.
"""

import importlib
import importlib.util
import logging
import sys
from pathlib import Path

import pytest

# The desktop client and the backend both provide a top-level ``app`` package —
# the reason the launcher spawns the backend as a separate process. In a single
# pytest session the backend's wins, so a plain ``from app.launcher import ...``
# collects fine on its own and fails as part of the suite. Load this package
# under a name of its own instead, so both can be tested in one run.
_DESKTOP = Path(__file__).resolve().parent.parent / "app"
if "desktop_app" not in sys.modules:
    _spec = importlib.util.spec_from_file_location(
        "desktop_app", _DESKTOP / "__init__.py",
        submodule_search_locations=[str(_DESKTOP)],
    )
    _module = importlib.util.module_from_spec(_spec)
    sys.modules["desktop_app"] = _module
    _spec.loader.exec_module(_module)

_launcher = importlib.import_module("desktop_app.launcher")
_QuietPeerDisconnect = _launcher._QuietPeerDisconnect
quieten_peer_disconnects = _launcher.quieten_peer_disconnects


def record(message: str, exc: BaseException | None) -> logging.LogRecord:
    return logging.LogRecord(
        name="asyncio", level=logging.ERROR, pathname=__file__, lineno=1,
        msg=message, args=(), exc_info=(type(exc), exc, None) if exc else None,
    )


def test_a_browser_hanging_up_is_not_reported_as_an_error():
    dropped = record(
        "Exception in callback _ProactorBasePipeTransport._call_connection_lost()",
        ConnectionResetError(10054, "An existing connection was forcibly closed"),
    )
    assert _QuietPeerDisconnect().filter(dropped) is False


@pytest.mark.parametrize("exc", [
    ConnectionAbortedError("aborted"),
    BrokenPipeError("broken pipe"),
])
def test_the_other_ways_a_peer_goes_away_are_also_quiet(exc):
    assert _QuietPeerDisconnect().filter(
        record("Exception in callback _ProactorBasePipeTransport._call_connection_lost()", exc)
    ) is False


def test_a_connection_reset_from_anywhere_else_still_surfaces():
    """The same exception raised outside connection teardown is a real event —
    the backend dropping mid-request, say — and must not be swallowed."""
    kept = record("Task exception was never retrieved",
                  ConnectionResetError(10054, "reset"))
    assert _QuietPeerDisconnect().filter(kept) is True


def test_a_different_failure_during_teardown_still_surfaces():
    """Narrow by exception type, not by where it happened. A bug in the
    teardown path is exactly the thing this must not hide."""
    kept = record(
        "Exception in callback _ProactorBasePipeTransport._call_connection_lost()",
        RuntimeError("something is actually wrong"),
    )
    assert _QuietPeerDisconnect().filter(kept) is True


def test_records_without_an_exception_are_untouched():
    assert _QuietPeerDisconnect().filter(record("just a message", None)) is True


def test_installing_twice_does_not_stack_filters():
    log = logging.getLogger("asyncio")
    existing = list(log.filters)
    try:
        log.filters = []
        quieten_peer_disconnects()
        quieten_peer_disconnects()
        assert sum(isinstance(f, _QuietPeerDisconnect) for f in log.filters) == 1
    finally:
        log.filters = existing
