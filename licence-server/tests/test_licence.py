"""The licence service.

What matters here is not that the happy path works, but that the refusals do —
an unknown key, a revoked one, a seat limit, and an unauthenticated operator
call. A licence service that fails open is decoration.
"""

import importlib.util
import sys
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# Three packages in this repository are called `app` — the backend, the desktop
# client, and this service. In one pytest session the first one imported wins,
# so these tests collected fine alone and failed as part of the suite. Load the
# module from its path under a name of its own instead.
_MAIN = Path(__file__).resolve().parent.parent / "app" / "main.py"

OPERATOR = "test-operator-token"


def _load_service():
    spec = importlib.util.spec_from_file_location("licence_service_main", _MAIN)
    module = importlib.util.module_from_spec(spec)
    sys.modules["licence_service_main"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def client(monkeypatch):
    """A service with its own empty database per test."""
    db = Path(tempfile.mkdtemp()) / "licences.db"
    monkeypatch.setenv("LICENCE_DATABASE_URL", f"sqlite:///{db}")
    monkeypatch.setenv("LICENCE_ADMIN_TOKEN", OPERATOR)

    main = _load_service()
    return TestClient(main.app)


def admin(extra=None):
    headers = {"Authorization": f"Bearer {OPERATOR}"}
    return {**headers, **(extra or {})}


def issue(client, name="Acme Capital", **kwargs):
    res = client.post("/api/licences", headers=admin(),
                      json={"client_name": name, **kwargs})
    assert res.status_code == 200, res.text
    return res.json()["key"]


# -- the desktop app's two calls -------------------------------------------

def test_a_valid_key_activates_and_is_recorded(client):
    key = issue(client)
    res = client.post("/api/activate", json={
        "key": key, "install_id": "install-0001", "app_version": "1.1.0",
        "environment": "PROD", "label": "Rahul laptop"})

    assert res.status_code == 200
    assert res.json()["status"] == "active"
    assert res.json()["client_name"] == "Acme Capital"

    listed = client.get("/api/licences", headers=admin()).json()["licences"]
    installs = listed[0]["installs"]
    assert len(installs) == 1
    assert installs[0]["label"] == "Rahul laptop"
    assert installs[0]["environment"] == "PROD"


def test_an_unknown_key_cannot_activate(client):
    res = client.post("/api/activate", json={
        "key": "CFX-000000-000000-000000", "install_id": "install-0001"})
    assert res.status_code == 404


def test_a_revoked_key_cannot_activate(client):
    key = issue(client)
    client.post(f"/api/licences/{key}/revoke", headers=admin())

    res = client.post("/api/activate", json={"key": key, "install_id": "install-0001"})
    assert res.status_code == 403


def test_heartbeat_reports_revocation_rather_than_erroring(client):
    """The app must tell "the server said stop" apart from "the server was
    unreachable". An exception makes those identical, and the second must not
    stop someone managing a live position."""
    key = issue(client)
    client.post("/api/activate", json={"key": key, "install_id": "install-0001"})

    alive = client.post("/api/heartbeat", json={"key": key, "install_id": "install-0001"})
    assert alive.status_code == 200 and alive.json()["status"] == "active"

    client.post(f"/api/licences/{key}/revoke", headers=admin())
    after = client.post("/api/heartbeat", json={"key": key, "install_id": "install-0001"})
    assert after.status_code == 200
    assert after.json()["status"] == "revoked"


def test_heartbeat_on_an_unknown_key_says_unknown(client):
    res = client.post("/api/heartbeat", json={
        "key": "CFX-DEADBEEF-CAFE-0000", "install_id": "install-0001"})
    assert res.status_code == 200
    assert res.json()["status"] == "unknown"


def test_a_restored_licence_works_again(client):
    key = issue(client)
    client.post(f"/api/licences/{key}/revoke", headers=admin())
    client.post(f"/api/licences/{key}/restore", headers=admin())

    res = client.post("/api/activate", json={"key": key, "install_id": "install-0001"})
    assert res.status_code == 200


# -- seats -----------------------------------------------------------------

def test_a_seat_limit_is_enforced(client):
    key = issue(client, max_installs=2)
    for n in range(2):
        assert client.post("/api/activate", json={
            "key": key, "install_id": f"install-{n:04d}"}).status_code == 200

    third = client.post("/api/activate", json={"key": key, "install_id": "install-0002"})
    assert third.status_code == 409
    assert "all in use" in third.json()["detail"]


def test_an_existing_install_reactivating_does_not_consume_a_seat(client):
    """Reinstalling, or restarting after a wipe of the local token, must not
    lock a client out of their own licence."""
    key = issue(client, max_installs=1)
    client.post("/api/activate", json={"key": key, "install_id": "install-0000"})

    again = client.post("/api/activate", json={"key": key, "install_id": "install-0000"})
    assert again.status_code == 200


def test_no_limit_means_no_limit(client):
    key = issue(client)
    for n in range(5):
        assert client.post("/api/activate", json={
            "key": key, "install_id": f"install-{n:04d}"}).status_code == 200


# -- the operator surface --------------------------------------------------

def test_operator_endpoints_refuse_without_the_token(client):
    assert client.get("/api/licences").status_code == 401
    assert client.post("/api/licences",
                       json={"client_name": "X"}).status_code == 401


def test_operator_endpoints_refuse_a_wrong_token(client):
    res = client.get("/api/licences", headers={"Authorization": "Bearer wrong"})
    assert res.status_code == 401


def test_operator_endpoints_fail_closed_when_no_token_is_configured(client, monkeypatch):
    """An unset token must disable the registry, not leave it open."""
    monkeypatch.delenv("LICENCE_ADMIN_TOKEN", raising=False)
    res = client.get("/api/licences", headers=admin())
    assert res.status_code == 503


def test_the_registry_answers_who_and_how_many(client):
    """The question this service exists for."""
    a = issue(client, "Acme Capital")
    b = issue(client, "Beta Traders")
    client.post("/api/activate", json={"key": a, "install_id": "install-a001", "app_version": "1.1.0"})
    client.post("/api/activate", json={"key": a, "install_id": "install-a002", "app_version": "1.1.0"})
    client.post("/api/activate", json={"key": b, "install_id": "install-b001", "app_version": "1.0.0"})

    licences = client.get("/api/licences", headers=admin()).json()["licences"]
    by_name = {lic["client_name"]: lic for lic in licences}

    assert len(by_name["Acme Capital"]["installs"]) == 2
    assert len(by_name["Beta Traders"]["installs"]) == 1


def test_nothing_about_trading_is_stored(client):
    """Positions, P&L and strategies are none of the operator's business, and
    holding them would change what obligations they are under."""
    main = sys.modules["licence_service_main"]
    columns = set(main.Install.__table__.columns.keys())

    assert columns == {"install_id", "licence_key", "app_version", "environment",
                       "label", "first_seen", "last_seen"}
