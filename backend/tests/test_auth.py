"""Platform authentication and broker-session isolation."""

import jose.jwt as jwt


def test_root_endpoint(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "Algo Trading Platform Backend" in response.json()["message"]


def test_register_login_and_me(client, unique_email):
    email = unique_email("flow")
    register = client.post("/api/v1/auth/register", json={
        "email": email, "password": "Passw0rd!secure",
        "full_name": "Test Trader", "tenant_name": "Test Desk",
    })
    assert register.status_code == 201
    assert register.json()["role"] == "trader"

    login = client.post("/api/v1/auth/login",
                        json={"email": email, "password": "Passw0rd!secure"})
    assert login.status_code == 200
    token = login.json()["access_token"]

    me = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    assert me.json()["email"] == email


def test_short_password_rejected(client, unique_email):
    response = client.post("/api/v1/auth/register", json={
        "email": unique_email("short"), "password": "abc",
        "full_name": "T", "tenant_name": "Desk",
    })
    assert response.status_code == 422


def test_wrong_password_rejected(client, unique_email):
    email = unique_email("wrong")
    client.post("/api/v1/auth/register", json={
        "email": email, "password": "Passw0rd!secure",
        "full_name": "T", "tenant_name": "Desk",
    })
    response = client.post("/api/v1/auth/login",
                           json={"email": email, "password": "NotThePassword1"})
    assert response.status_code == 401


def test_token_signed_with_another_key_is_rejected(client):
    forged = jwt.encode(
        {"sub": "x", "tenant_id": "y", "role": "admin"}, "wrong-key", algorithm="HS256"
    )
    response = client.get("/api/v1/auth/me",
                          headers={"Authorization": f"Bearer {forged}"})
    assert response.status_code == 401


def test_choice_endpoints_require_a_session(client, registered):
    """A user who has not connected gets 409, never another user's data."""
    headers, _ = registered("nosession")
    for path in ("/api/v1/portfolio/funds", "/api/v1/portfolio/holdings",
                 "/api/v1/market/quotes"):
        response = client.get(path, headers=headers)
        assert response.status_code == 409, path


def test_broker_sessions_are_not_shared_between_users(client, connected, registered):
    """Connecting one account must not grant any other user access."""
    _, _ = connected("owner")
    other_headers, _ = registered("bystander")

    response = client.get("/api/v1/portfolio/funds", headers=other_headers)
    assert response.status_code == 409


def test_sandbox_session_reports_its_mode(client, connected):
    headers, _ = connected("sandbox")
    status = client.get("/api/v1/auth/choice/status", headers=headers)
    assert status.status_code == 200
    assert status.json()["mode"] == "DEMO"
    assert status.json()["connected"] is True


def test_quotes_endpoint_graceful_on_upstream_failure(client, connected, monkeypatch):
    """When Choice market touchline is down, /quotes returns 200 with partial/empty data instead of 502."""
    headers, _ = connected("quotes_sandbox")
    from app.api.v1 import market as market_api

    def mock_fail(session, seg_tokens):
        from engine.app.choice_gateway.errors import ChoiceUpstreamError
        raise ChoiceUpstreamError("Choice touchline unavailable", "Index was outside bounds")

    monkeypatch.setattr(market_api.market_gateway, "get_multiple_touchline", mock_fail)
    response = client.get("/api/v1/market/quotes", headers=headers)
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "PARTIAL"
    assert data["data"] == []
    assert "Choice touchline unavailable" in data["upstream_error"]


def test_disconnect_drops_the_session(client, connected):
    headers, _ = connected("disconnect")
    assert client.post("/api/v1/auth/choice/disconnect", headers=headers).status_code == 200
    assert client.get("/api/v1/portfolio/funds", headers=headers).status_code == 409


def test_oauth_callback_rejects_an_unissued_state(client):
    """The callback must not mint a session for an arbitrary caller."""
    response = client.get("/api/v1/auth/choice/oauth/callback", params={
        "state": "forged.0000", "cid": "ATTACKER", "sid": "ATTACKER_SID",
    })
    assert response.status_code == 401


def test_oauth_start_requires_authentication(client):
    response = client.post("/api/v1/auth/choice/oauth/start",
                           params={"redirect_url": "http://localhost/cb"})
    assert response.status_code == 401


def test_oauth_disabled_without_a_vendor_key(client, registered):
    """Without the AES key the flow is off, not falling back to plaintext."""
    headers, _ = registered("oauth")
    response = client.post("/api/v1/auth/choice/oauth/start",
                           params={"redirect_url": "http://localhost/cb"},
                           headers=headers)
    assert response.status_code == 200
    assert response.json()["enabled"] is False


# -- Choice credential errors ----------------------------------------------
#
# "Token Expired" and "VendorId Invalid" need different actions from the user.
# Telling someone to "sign in again" when the fix is a reissued key sends them
# round a loop that cannot succeed.

import pytest

from app.core.errors import _environment_hint
from engine.app.choice_gateway.errors import ChoiceAuthError


@pytest.mark.parametrize("upstream,expired", [
    ("Unauthorized, Token Expired", True),
    ("Unauthorized, TOKEN EXPIRED", True),
    ("Unauthorized, VendorId Invalid or doesn't exists", False),
])
def test_credential_errors_give_the_right_instruction(upstream, expired):
    hint = _environment_hint(ChoiceAuthError("Choice rejected the session.", upstream))

    # The wording of the unknown-credential branch depends on CHOICE_ENV, so
    # assert only what must hold in either environment.
    assert ("generate a fresh api key" in hint.lower()) is expired
    assert hint, "an unrecognised credential must still explain itself"


def test_an_unrelated_failure_gets_no_credential_hint():
    assert _environment_hint(ChoiceAuthError("Timed out", "read timeout")) == ""


def test_diagnostics_returns_the_shape_the_health_panel_reads(client, registered):
    """The Health panel renders session/upstream/hint by name. It once expected
    'checks', which this endpoint never returned, so the panel silently fell
    back to dumping raw JSON — the one screen meant to explain a failed
    connection hid the answer."""
    headers, _ = registered("diagshape")
    client.post("/api/v1/auth/choice/connect", headers=headers,
                json={"mode": "sandbox", "vendor_id": "DEMO",
                      "api_key": "DEMO", "mobile_no": ""})

    body = client.get("/api/v1/diagnostics/choice", headers=headers).json()

    assert {"session", "upstream", "normalized"} <= set(body)
    assert {"mode", "environment", "vendor_id", "base_url"} <= set(body["session"])
    assert isinstance(body["upstream"], list)


# -- the Choice environment is chosen per connection ------------------------
#
# It used to be one setting in a shared .env, so a user testing against UAT
# moved everyone on that install — and a production Client ID is simply
# rejected by the sandbox, which is the commonest setup failure there is.

def test_the_chosen_environment_reaches_the_broker_login(client, registered, monkeypatch):
    """Asserted at the call, not at the status code.

    A sandbox connect returns 200 whatever is sent — it never reaches
    `login_totp` at all — so checking the response proves nothing about
    whether the choice was honoured.
    """
    from engine.app.choice_gateway.client_manager import ChoiceSession, SessionMode

    seen = {}

    def fake_login(self, vendor_id, api_key, mobile_no, paper=False,
                   remember=False, environment=None):
        seen["environment"] = environment
        seen["paper"] = paper
        self.mode = SessionMode.PAPER
        self.session_id = "TEST"

    monkeypatch.setattr(ChoiceSession, "login_totp", fake_login)
    headers, _ = registered("env_pick")

    res = client.post("/api/v1/auth/choice/connect", headers=headers, json={
        "mode": "paper", "vendor_id": "M09984", "api_key": "realkey",
        "mobile_no": "9999999999", "environment": "PROD"})

    assert res.status_code == 200, res.text
    assert seen["environment"] == "PROD"


def test_an_unknown_environment_is_refused_rather_than_guessed(client, registered):
    headers, _ = registered("env_bad")

    res = client.post("/api/v1/auth/choice/connect", headers=headers, json={
        "mode": "sandbox", "vendor_id": "DEMO", "api_key": "DEMO",
        "mobile_no": "", "environment": "PRODUCTION"})

    assert res.status_code == 422


def test_omitting_the_environment_keeps_the_deployment_default(client, registered):
    """Existing clients send no environment field and must be unaffected."""
    headers, _ = registered("env_none")

    res = client.post("/api/v1/auth/choice/connect", headers=headers, json={
        "mode": "sandbox", "vendor_id": "DEMO", "api_key": "DEMO", "mobile_no": ""})

    assert res.status_code == 200
    assert res.json()["environment"] in {"UAT", "PROD"}
