"""Activation, and what happens when the licence service goes quiet.

The important case is the difference between "revoked" and "unreachable". A
network hiccup must not stop someone managing a live position; a withdrawn
licence must stop them within a bounded time. Getting these confused in either
direction is the failure mode worth testing.
"""

import importlib
import importlib.util
import json
import sys
import time
from pathlib import Path

import pytest

_DESKTOP = Path(__file__).resolve().parent.parent / "app"
if "desktop_app" not in sys.modules:
    _spec = importlib.util.spec_from_file_location(
        "desktop_app", _DESKTOP / "__init__.py",
        submodule_search_locations=[str(_DESKTOP)])
    _module = importlib.util.module_from_spec(_spec)
    sys.modules["desktop_app"] = _module
    _spec.loader.exec_module(_module)

licence = importlib.import_module("desktop_app.licence")
State = licence.LicenceState


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(licence, "_store_path", lambda: tmp_path / "licence.json")
    return tmp_path / "licence.json"


def write(store, **data):
    store.write_text(json.dumps(data), encoding="utf-8")


# -- identity ---------------------------------------------------------------

def test_the_install_id_is_stable(store):
    first = licence.install_id()
    assert licence.install_id() == first
    assert first.startswith("install-")


def test_the_install_id_says_nothing_about_the_machine(store):
    """Random, not derived. A hostname or MAC would identify the person, and
    counting seats does not require knowing who they are."""
    import socket
    generated = licence.install_id()
    assert socket.gethostname().lower() not in generated.lower()


# -- verdicts ---------------------------------------------------------------

def test_no_server_configured_means_licensing_is_off(store):
    """A build shipped before the service existed must not become a brick."""
    assert licence.check("") == State.DISABLED


def test_no_key_entered_is_unlicensed(store):
    write(store, install_id="install-x")
    assert licence.check("http://localhost:1") == State.UNLICENSED


def test_a_reachable_service_saying_revoked_stops_it(store, monkeypatch):
    write(store, key="CFX-1", install_id="install-x", last_verified=time.time())
    monkeypatch.setattr(licence, "_post", lambda *a, **k: {"status": "revoked"})

    assert licence.check("http://licences.example") == State.REVOKED


def test_a_deleted_licence_counts_as_revoked(store, monkeypatch):
    write(store, key="CFX-1", install_id="install-x", last_verified=time.time())
    monkeypatch.setattr(licence, "_post", lambda *a, **k: {"status": "unknown"})

    assert licence.check("http://licences.example") == State.REVOKED


def test_an_unreachable_service_keeps_working_inside_the_grace(store, monkeypatch):
    """The case that must not stop trading: a checked-in copy, briefly offline."""
    write(store, key="CFX-1", install_id="install-x",
          last_verified=time.time() - 2 * 86400, grace_days=7)

    def boom(*a, **k):
        raise OSError("network down")
    monkeypatch.setattr(licence, "_post", boom)

    assert licence.check("http://licences.example") == State.ACTIVE


def test_an_unreachable_service_stops_it_once_the_grace_runs_out(store, monkeypatch):
    write(store, key="CFX-1", install_id="install-x",
          last_verified=time.time() - 9 * 86400, grace_days=7)

    def boom(*a, **k):
        raise OSError("network down")
    monkeypatch.setattr(licence, "_post", boom)

    assert licence.check("http://licences.example") == State.UNREACHABLE


def test_a_copy_that_never_checked_in_gets_no_grace(store, monkeypatch):
    """Grace extends something already granted. Nothing was."""
    write(store, key="CFX-1", install_id="install-x")

    def boom(*a, **k):
        raise OSError("network down")
    monkeypatch.setattr(licence, "_post", boom)

    assert licence.check("http://licences.example") == State.UNLICENSED


def test_a_successful_check_refreshes_the_clock(store, monkeypatch):
    write(store, key="CFX-1", install_id="install-x",
          last_verified=time.time() - 5 * 86400, grace_days=7)
    monkeypatch.setattr(licence, "_post",
                        lambda *a, **k: {"status": "active", "grace_days": 7})

    assert licence.check("http://licences.example") == State.ACTIVE
    assert json.loads(store.read_text())["last_verified"] > time.time() - 5


# -- activation -------------------------------------------------------------

def test_activation_stores_what_the_service_returned(store, monkeypatch):
    monkeypatch.setattr(licence, "_post", lambda *a, **k: {
        "status": "active", "client_name": "Acme Capital", "grace_days": 7})

    licence.activate("http://licences.example", "CFX-ABC")
    saved = json.loads(store.read_text())

    assert saved["key"] == "CFX-ABC"
    assert saved["client_name"] == "Acme Capital"
    assert saved["last_verified"] > 0


def test_a_refused_activation_raises_rather_than_pretending(store, monkeypatch):
    import urllib.error

    def refuse(*a, **k):
        raise urllib.error.URLError("no route to host")
    monkeypatch.setattr(licence, "_post", refuse)

    with pytest.raises(RuntimeError, match="Could not reach"):
        licence.activate("http://licences.example", "CFX-ABC")


# -- what the user is told --------------------------------------------------

@pytest.mark.parametrize("state", [State.REVOKED, State.UNREACHABLE, State.UNLICENSED])
def test_every_stopping_state_explains_itself(state):
    message = licence.message_for(state)
    assert message and len(message) > 20


def test_a_working_state_says_nothing(store):
    assert licence.message_for(State.ACTIVE) is None
    assert licence.message_for(State.DISABLED) is None


def test_the_revocation_message_does_not_imply_positions_are_at_risk():
    """Positions live at the broker. Someone reading this at 3pm needs to know
    that immediately, not wonder."""
    assert "broker" in licence.message_for(State.REVOKED)
