"""Persisted broker sessions, and the credential-expiry classification.

Both exist because a restart silently dropped the broker session while the
platform login survived, and because "sign in again" is the wrong instruction
for a key that has expired.
"""

import json

import pytest

from engine.app.choice_gateway import session_store
from engine.app.choice_gateway.errors import ChoiceAuthError, ChoiceCredentialExpired


@pytest.fixture(autouse=True)
def _isolated_store(tmp_path, monkeypatch):
    """Point the store at a temp directory and give it a key to derive from."""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setenv("SECRET_KEY", "k" * 48)
    yield


def test_a_saved_session_round_trips():
    session_store.save("u1", "SESS-1", "PROD", "PAPER",
                       vendor_id="M09984", base_url="https://x")
    data = session_store.load("u1")

    assert data["session_id"] == "SESS-1"
    assert data["mode"] == "PAPER"
    assert data["vendor_id"] == "M09984"


def test_the_session_id_is_not_readable_on_disk(tmp_path):
    """Obfuscation, not secrecy — but a plain-text session id in a file that
    backup software copies is a different class of mistake."""
    session_store.save("u1", "SUPER-SECRET-SESSION", "PROD", "PAPER")
    raw = (tmp_path / "ChoiceFinxTrader" / "sessions.json").read_text(encoding="utf-8")

    assert "SUPER-SECRET-SESSION" not in raw


def test_a_tampered_record_is_rejected_not_decrypted(tmp_path):
    """A modified record must not decrypt into a usable session id. Fernet
    signs the ciphertext, so tampering fails authentication rather than
    yielding plausible nonsense."""
    session_store.save("u1", "SESS-1", "PROD", "PAPER")
    path = tmp_path / "ChoiceFinxTrader" / "sessions.json"
    store = json.loads(path.read_text(encoding="utf-8"))
    store["u1"] = store["u1"][:-6] + "AAAAAA"
    path.write_text(json.dumps(store), encoding="utf-8")

    assert session_store.load("u1") is None


def test_a_record_written_under_a_different_key_is_discarded(tmp_path, monkeypatch):
    """Rotating SECRET_KEY must invalidate stored sessions rather than crash."""
    session_store.save("u1", "SESS-1", "PROD", "PAPER")
    monkeypatch.setenv("SECRET_KEY", "different" * 6)

    assert session_store.load("u1") is None


def test_a_stale_record_is_dropped(monkeypatch):
    session_store.save("u1", "SESS-1", "PROD", "PAPER")
    monkeypatch.setattr(session_store, "MAX_AGE_SECONDS", -1)

    assert session_store.load("u1") is None


def test_clear_forgets_only_that_user():
    session_store.save("keep", "S-KEEP", "PROD", "PAPER")
    session_store.save("drop", "S-DROP", "PROD", "PAPER")
    session_store.clear("drop")

    assert session_store.load("drop") is None
    assert session_store.load("keep")["session_id"] == "S-KEEP"


def test_it_still_works_without_a_signing_key(monkeypatch):
    """The desktop app sets no SECRET_KEY — the backend generates a random one
    per process — so requiring it made "remember me" quietly do nothing on the
    one deployment that has no other way to persist. A machine-local key file
    covers that case."""
    monkeypatch.delenv("SECRET_KEY", raising=False)
    session_store.save("u1", "SESS-1", "PROD", "PAPER")

    assert session_store.load("u1")["session_id"] == "SESS-1"


def test_the_local_key_survives_a_restart(monkeypatch, tmp_path):
    """The whole point: what one process wrote, the next one can read. A key
    regenerated per process would be indistinguishable from storing nothing."""
    monkeypatch.delenv("SECRET_KEY", raising=False)
    session_store.save("u1", "SESS-1", "PROD", "PAPER")
    first = (tmp_path / "ChoiceFinxTrader" / "session.key").read_bytes()

    assert session_store.load("u1")["session_id"] == "SESS-1"
    assert (tmp_path / "ChoiceFinxTrader" / "session.key").read_bytes() == first


def test_a_corrupt_key_file_does_not_break_the_login(monkeypatch, tmp_path):
    """Persisting must never raise into a login that already succeeded, so a
    damaged key file is replaced rather than propagated."""
    monkeypatch.delenv("SECRET_KEY", raising=False)
    session_store.save("u1", "SESS-1", "PROD", "PAPER")
    (tmp_path / "ChoiceFinxTrader" / "session.key").write_bytes(b"truncated")

    session_store.save("u1", "SESS-2", "PROD", "PAPER")     # must not raise
    assert session_store.load("u1")["session_id"] == "SESS-2"


def test_nothing_is_written_when_no_key_can_be_kept(monkeypatch):
    """Nowhere to keep a key means nothing can be persisted. Refuse rather than
    fall back to a constant, which would make the file portable between
    machines — worse than storing nothing."""
    monkeypatch.delenv("SECRET_KEY", raising=False)
    monkeypatch.setattr(session_store.Fernet, "generate_key",
                        staticmethod(lambda: (_ for _ in ()).throw(OSError("read-only"))))
    session_store.save("u1", "SESS-1", "PROD", "PAPER")

    assert session_store.load("u1") is None


def test_an_empty_session_id_is_not_stored():
    session_store.save("u1", "", "PROD", "PAPER")
    assert session_store.load("u1") is None


# -- credential expiry -----------------------------------------------------

def test_expired_is_a_distinct_type_from_a_rejected_credential():
    """The remedy differs: reissue the key vs check the Client ID. Callers
    branch on the type, so the classification lives in one place."""
    assert issubclass(ChoiceCredentialExpired, ChoiceAuthError)
    assert not isinstance(ChoiceAuthError("x"), ChoiceCredentialExpired)


@pytest.mark.parametrize("body,expired", [
    ("Unauthorized, Token Expired", True),
    ("unauthorized, TOKEN EXPIRED", True),
    ("Unauthorized, VendorId Invalid or doesn't exists", False),
    ("Unauthorized", False),
])
def test_the_gateway_classifies_expiry_from_the_response_body(body, expired):
    from engine.app.choice_gateway.client_manager import EXPIRED_MARKERS

    hit = any(m in body.lower() for m in EXPIRED_MARKERS)
    assert hit is expired


def test_session_records_credential_state():
    from engine.app.choice_gateway.client_manager import ChoiceSession

    session = ChoiceSession("u1")
    assert session.credential_state == "UNKNOWN"

    session.record_failure(ChoiceCredentialExpired("expired", "Token Expired"))
    assert session.credential_state == "EXPIRED"

    session.record_failure(ChoiceAuthError("nope", "VendorId Invalid"))
    assert session.credential_state == "REJECTED"

    session.record_success()
    assert session.credential_state == "OK"
    assert session.last_error is None


# -- the environment is a property of the session, not the install ----------
#
# Each user picks UAT or PROD when they connect. Everything that reports,
# stores or logs an environment has to agree with the one the HTTP client was
# actually built against — a page saying UAT while orders go to PROD is the
# most expensive thing this codebase could get wrong.

from unittest.mock import patch

import engine.app.choice_gateway.client_manager as cm
from engine.app.config import CHOICE_BASE_URLS


def _connect(owner, environment=None):
    session = cm.ChoiceSession(owner_key=owner)
    with patch.object(cm, "TimeoutChoiceClient") as Client:
        Client.return_value.login.return_value = "SESS-123"
        session.login_totp("M09984", "key", "9999999999", paper=True,
                           environment=environment, remember=True)
        session._built_with = Client.call_args.kwargs.get("base_url")
    return session


@pytest.mark.parametrize("chosen", ["UAT", "PROD"])
def test_the_http_client_is_built_against_the_chosen_server(chosen):
    session = _connect(f"route-{chosen}", chosen)
    assert session._built_with == CHOICE_BASE_URLS[chosen]


@pytest.mark.parametrize("chosen", ["UAT", "PROD"])
def test_what_is_reported_matches_where_calls_go(chosen):
    session = _connect(f"report-{chosen}", chosen)
    described = session.describe()

    assert described["environment"] == chosen
    assert described["base_url"] == session._built_with


def test_omitting_a_choice_uses_the_deployment_default():
    """Existing behaviour for anyone who does not pick."""
    session = _connect("route-default", None)
    assert session._built_with == cm.engine_settings.choice_base_url


def test_the_stored_session_records_its_own_server_not_the_default():
    """Saved as the install default, a UAT session came back claiming PROD."""
    other = "UAT" if cm.engine_settings.CHOICE_ENV.upper() == "PROD" else "PROD"
    _connect("store-env", other)

    saved = session_store.load("store-env")
    assert saved["environment"] == other
    assert saved["base_url"] == CHOICE_BASE_URLS[other]


def test_a_restored_session_reports_the_server_it_was_saved_with():
    """Restore set base_url without setting the environment, so the app
    reported one server while calling the other."""
    other = "UAT" if cm.engine_settings.CHOICE_ENV.upper() == "PROD" else "PROD"
    _connect("restore-env", other)
    stored = session_store.load("restore-env")

    fresh = cm.ChoiceSession(owner_key="restore-env")
    with patch.object(cm, "TimeoutChoiceClient") as Client:
        Client.return_value.funds.get_funds_view_new.return_value = {"Status": "Success"}
        assert fresh.restore(stored) is True

    assert fresh.environment == other
    assert fresh.base_url == CHOICE_BASE_URLS[other]
    assert fresh.describe()["environment"] == other


def test_a_stored_session_with_an_unknown_server_is_discarded():
    """base_url and the reported environment must never disagree; when the
    stored environment cannot be resolved, the record is not usable."""
    session_store.save("bad-env", "SESS-1", "MARS", "PAPER",
                       vendor_id="M09984", base_url="https://example.invalid")
    stored = session_store.load("bad-env")

    fresh = cm.ChoiceSession(owner_key="bad-env")
    assert fresh.restore(stored) is False
    assert session_store.load("bad-env") is None
