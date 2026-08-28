"""Test fixtures.

Every test run gets its own database file. The previous suite wrote to the real
``algo.db`` with fixed e-mail addresses, so a second run failed on duplicate
users and left rows behind.
"""

import os
import sys
import tempfile
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "backend"))

# Must be set before app.config is imported anywhere.
_TEST_DB = Path(tempfile.gettempdir()) / f"algo_test_{uuid.uuid4().hex}.db"
os.environ["DATABASE_URL"] = f"sqlite:///{_TEST_DB.as_posix()}"
os.environ["SECRET_KEY"] = "test-only-secret-key-at-least-32-characters-long"
os.environ["APP_ENV"] = "development"
os.environ["CHOICE_ENV"] = "UAT"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(scope="session", autouse=True)
def _cleanup_database():
    yield
    try:
        _TEST_DB.unlink(missing_ok=True)
    except OSError:
        pass


@pytest.fixture
def unique_email():
    """A fresh address per test, so tests do not collide or depend on order."""
    return lambda prefix="user": f"{prefix}_{uuid.uuid4().hex[:10]}@example.com"


@pytest.fixture
def registered(client, unique_email):
    """Register a user and return (auth headers, registration payload)."""

    def _register(prefix="user", tenant="Test Desk"):
        response = client.post(
            "/api/v1/auth/register",
            json={
                "email": unique_email(prefix),
                "password": "Passw0rd!secure",
                "full_name": "Test Trader",
                "tenant_name": tenant,
            },
        )
        assert response.status_code == 201, response.text
        data = response.json()
        return {"Authorization": f"Bearer {data['access_token']}"}, data

    return _register


@pytest.fixture
def connected(client, registered):
    """A user with a sandbox Choice session attached."""

    def _connect(prefix="user", tenant="Test Desk"):
        headers, data = registered(prefix, tenant)
        response = client.post(
            "/api/v1/auth/choice/connect",
            headers=headers,
            json={"vendor_id": "DEMO", "api_key": "DEMO", "mobile_no": "9999999999"},
        )
        assert response.status_code == 200, response.text
        return headers, data

    return _connect
