"""Paper mode: real market data, simulated orders, no money moved.

The property that matters most is the negative one — a paper session must have
no path that reaches Choice's order endpoint.
"""

import pytest

from engine.app.choice_gateway import orders as orders_gateway
from engine.app.choice_gateway.client_manager import ChoiceSession, SessionMode

BASE_ORDER = {
    "segment_id": 1, "token": "2885", "symbol": "RELIANCE",
    "side": "BUY", "order_type": "RL_MKT", "quantity": 10, "price_in_paisa": 0,
}


def connect(client, headers, mode):
    return client.post("/api/v1/auth/choice/connect", headers=headers, json={
        "mode": mode, "vendor_id": "DEMO", "api_key": "DEMO", "mobile_no": "9999999999",
    })


# -- mode selection --------------------------------------------------------

def test_default_mode_is_paper_not_live(client, registered):
    """Omitting the mode must never produce a session that sends real orders."""
    from app.schemas.auth import ChoiceTotpRequest

    req = ChoiceTotpRequest(vendor_id="X", api_key="Y", mobile_no="9999999999")
    assert req.mode == "paper"


def test_unknown_mode_is_rejected(client, registered):
    headers, _ = registered("badmode")
    response = client.post("/api/v1/auth/choice/connect", headers=headers, json={
        "mode": "yolo", "vendor_id": "M09984", "api_key": "k", "mobile_no": "9999999999",
    })
    assert response.status_code == 422


def test_demo_credentials_force_sandbox_even_when_live_requested(client, registered):
    """DEMO must not become a live session because the mode said so."""
    headers, _ = registered("demoforce")
    response = client.post("/api/v1/auth/choice/connect", headers=headers, json={
        "mode": "live", "vendor_id": "DEMO", "api_key": "DEMO", "mobile_no": "9999999999",
    })
    assert response.status_code == 200
    assert response.json()["mode"] == "DEMO"
    assert response.json()["sends_real_orders"] is False


def test_status_reports_whether_orders_are_real(client, registered):
    headers, _ = registered("statusmode")
    connect(client, headers, "sandbox")
    status = client.get("/api/v1/auth/choice/status", headers=headers).json()
    assert status["connected"] is True
    assert status["sends_real_orders"] is False


# -- session semantics -----------------------------------------------------

def test_paper_session_simulates_orders_but_uses_broker_data():
    session = ChoiceSession("paper-user")
    session.mode = SessionMode.PAPER
    session.session_id = "SID"
    session.client = object()

    assert session.simulates_orders is True
    assert session.uses_broker_data is True
    assert session.mode.sends_real_orders is False
    assert session.is_connected is True


def test_live_session_does_not_simulate():
    session = ChoiceSession("live-user")
    session.mode = SessionMode.LIVE
    session.session_id = "SID"

    assert session.simulates_orders is False
    assert session.mode.sends_real_orders is True


def test_sandbox_session_has_no_broker_data():
    session = ChoiceSession("demo-user")
    session.start_demo("DEMO")

    assert session.simulates_orders is True
    assert session.uses_broker_data is False


def test_paper_order_never_calls_the_broker(monkeypatch):
    """The decisive test: no client call may be made from a paper order."""
    session = ChoiceSession("paper-user")
    session.mode = SessionMode.PAPER
    session.session_id = "SID"

    def explode(*args, **kwargs):
        raise AssertionError("A paper order must not reach the Choice client")

    class Guard:
        def __getattr__(self, name):
            return explode

    session.client = Guard()
    monkeypatch.setattr(session, "require_client", explode)

    result = orders_gateway.place_order(
        session=session, segment_id=1, token=2885, order_type="RL_LIMIT",
        side="BUY", quantity=10, price=2500.0, symbol="RELIANCE",
    )
    assert result["mode"] == "PAPER"
    assert "Nothing was sent to Choice" in result["message"]


# -- position and P&L tracking --------------------------------------------

def test_simulated_fills_track_position_and_realized_pnl():
    session = ChoiceSession("pnl-user")
    session.start_demo("DEMO")

    session.record_simulated_fill("RELIANCE", "BUY", 10, 2500.0)
    assert session.paper_positions["RELIANCE"]["quantity"] == 10
    assert session.paper_positions["RELIANCE"]["average_price"] == 2500.0

    # Averaging on a second buy.
    session.record_simulated_fill("RELIANCE", "BUY", 10, 2600.0)
    assert session.paper_positions["RELIANCE"]["average_price"] == 2550.0

    # Selling half realises half the gain.
    session.record_simulated_fill("RELIANCE", "SELL", 10, 2650.0)
    assert session.paper_realized_pnl == pytest.approx(1000.0)
    assert session.paper_positions["RELIANCE"]["quantity"] == 10

    # Closing out removes the position.
    session.record_simulated_fill("RELIANCE", "SELL", 10, 2650.0)
    assert "RELIANCE" not in session.paper_positions
    assert session.paper_realized_pnl == pytest.approx(2000.0)


def test_market_order_fills_at_a_quote_not_zero(client, connected):
    """A market order submitted with price 0 must not fill at 0."""
    headers, _ = connected("fillprice")
    response = client.post("/api/v1/orders/", headers=headers, json=BASE_ORDER)
    assert response.status_code == 200

    filled = response.json()["order"]
    assert filled["executed_price"] > 0


def test_simulated_orders_are_recorded_as_simulated(client, connected):
    headers, _ = connected("simstatus")
    client.post("/api/v1/orders/", headers=headers, json=BASE_ORDER)

    rows = client.get("/api/v1/orders/", headers=headers).json()
    assert rows[0]["status"] == "SIMULATED"
    assert rows[0]["execution_mode"] in ("DEMO", "PAPER")


def test_order_book_shows_simulated_fills(client, connected):
    headers, _ = connected("simbook")
    client.post("/api/v1/orders/", headers=headers, json=BASE_ORDER)

    book = client.get("/api/v1/orders/book", headers=headers).json()
    assert book["mode"] in ("DEMO", "PAPER")
    assert len(book["data"]) >= 1
    assert "paper" in book


def test_paper_pnl_reported_on_status(client, connected):
    headers, _ = connected("pnlstatus")
    client.post("/api/v1/orders/", headers=headers, json=BASE_ORDER)

    status = client.get("/api/v1/auth/choice/status", headers=headers).json()
    assert status["paper"] is not None
    assert status["paper"]["simulated_orders"] >= 1


# -- shared configuration --------------------------------------------------

def test_backend_and_engine_tolerate_a_shared_env_file(tmp_path, monkeypatch):
    """Both settings classes read one .env, so neither may reject the other's keys.

    Refusing an unknown key here does not degrade gracefully: it raises at
    import time and the application never starts.
    """
    env_file = tmp_path / ".env"
    env_file.write_text(
        "APP_ENV=development\n"
        "SECRET_KEY=abcdefghijklmnopqrstuvwxyz0123456789\n"
        "CHOICE_ENV=PROD\n"
        "ORDER_RATE_LIMIT_PER_SEC=10\n"
        "MAX_ORDER_VALUE=500000\n",
        encoding="utf-8",
    )

    from app.config import Settings
    from engine.app.config import EngineSettings

    # A real environment variable outranks the file, so clear the ones the
    # test session sets before reading it back.
    for name in ("CHOICE_ENV", "APP_ENV", "SECRET_KEY"):
        monkeypatch.delenv(name, raising=False)

    backend = Settings(_env_file=str(env_file))
    engine = EngineSettings(_env_file=str(env_file))

    assert backend.APP_ENV == "development"
    assert engine.CHOICE_ENV == "PROD"
    assert engine.choice_base_url == "https://finxomne.choiceindia.com"
    assert engine.is_production is True


def test_choice_env_selects_the_endpoint():
    from engine.app.config import EngineSettings

    assert EngineSettings(CHOICE_ENV="UAT").choice_base_url == "https://uat.jiffy.in"
    assert (EngineSettings(CHOICE_ENV="PROD").choice_base_url
            == "https://finxomne.choiceindia.com")
    assert EngineSettings(CHOICE_ENV="UAT").is_production is False


def test_unknown_choice_env_is_rejected():
    from engine.app.config import EngineSettings

    with pytest.raises(ValueError, match="CHOICE_ENV must be one of"):
        EngineSettings(CHOICE_ENV="somewhere").choice_base_url


# -- choosing paper or live on a connected session --------------------------
#
# Both directions are available to a session that signed in with real
# credentials: someone using their own account decides whether their own orders
# are real. They are not symmetric — going to paper is immediate, going to live
# must be confirmed, and a sandbox session can never become live at all.

def _session_for(client, headers):
    from engine.app.choice_gateway.client_manager import choice_sessions
    me = client.get("/api/v1/auth/me", headers=headers).json()
    return choice_sessions.get(str(me["id"]))


def _make_real(session, mode):
    """A session that genuinely signed in, in the given mode."""
    from engine.app.choice_gateway.client_manager import SessionMode
    session.mode = SessionMode.LIVE if mode == "LIVE" else SessionMode.PAPER
    session.session_id = "SESS-REAL"
    session.vendor_id = "M09984"
    session.api_key = "realkey"
    return session


def test_a_live_session_can_drop_to_paper_instantly(client, connected):
    """No confirmation and no credentials: reducing what a session may do
    proves nothing by being slow."""
    from engine.app.choice_gateway.client_manager import SessionMode

    headers, _ = connected("mode_down")
    _make_real(_session_for(client, headers), "LIVE")

    res = client.post("/api/v1/auth/choice/mode", headers=headers,
                      json={"mode": "paper"})

    assert res.status_code == 200
    assert res.json()["changed"] is True
    assert res.json()["sends_real_orders"] is False
    assert _session_for(client, headers).mode is SessionMode.PAPER


def test_a_paper_session_can_go_live_when_confirmed(client, connected):
    from engine.app.choice_gateway.client_manager import SessionMode

    headers, _ = connected("mode_up")
    _make_real(_session_for(client, headers), "PAPER")

    res = client.post("/api/v1/auth/choice/mode", headers=headers,
                      json={"mode": "live", "confirm": True})

    assert res.status_code == 200
    assert res.json()["sends_real_orders"] is True
    assert _session_for(client, headers).mode is SessionMode.LIVE


def test_going_live_without_confirming_is_refused(client, connected):
    """A mis-click must not turn simulated orders into real ones."""
    from engine.app.choice_gateway.client_manager import SessionMode

    headers, _ = connected("mode_noconfirm")
    _make_real(_session_for(client, headers), "PAPER")

    res = client.post("/api/v1/auth/choice/mode", headers=headers,
                      json={"mode": "live"})

    assert res.status_code == 400
    assert "real money" in res.json()["detail"]
    assert _session_for(client, headers).mode is SessionMode.PAPER


def test_dropping_to_paper_needs_no_confirmation(client, connected):
    """The asymmetry, stated as a test: the safe direction is never gated."""
    headers, _ = connected("mode_down_noconfirm")
    _make_real(_session_for(client, headers), "LIVE")

    assert client.post("/api/v1/auth/choice/mode", headers=headers,
                       json={"mode": "paper"}).status_code == 200


def test_a_sandbox_session_can_never_go_live(client, connected):
    """No Choice login behind it, so "live" would mean orders with nowhere to
    go — and a user who thinks they are trading for real when they are not."""
    from engine.app.choice_gateway.client_manager import SessionMode

    headers, _ = connected("mode_sandbox")          # the fixture connects DEMO
    assert _session_for(client, headers).mode is SessionMode.DEMO

    res = client.post("/api/v1/auth/choice/mode", headers=headers,
                      json={"mode": "live", "confirm": True})

    assert res.status_code == 409
    assert "sandbox" in res.json()["detail"].lower()
    assert _session_for(client, headers).mode is SessionMode.DEMO


def test_the_broker_session_survives_either_switch(client, connected):
    """Market data, holdings and the Choice session id are unaffected; only the
    permission to submit changes."""
    headers, _ = connected("mode_keep")
    session = _make_real(_session_for(client, headers), "LIVE")

    client.post("/api/v1/auth/choice/mode", headers=headers, json={"mode": "paper"})
    client.post("/api/v1/auth/choice/mode", headers=headers,
                json={"mode": "live", "confirm": True})

    assert session.session_id == "SESS-REAL"
    assert session.vendor_id == "M09984"
    assert session.uses_broker_data is True


def test_switching_to_the_mode_already_in_use_reports_no_change(client, connected):
    headers, _ = connected("mode_same")
    _make_real(_session_for(client, headers), "PAPER")

    res = client.post("/api/v1/auth/choice/mode", headers=headers,
                      json={"mode": "paper"})

    assert res.status_code == 200
    assert res.json()["changed"] is False


def test_an_unknown_mode_is_refused(client, connected):
    headers, _ = connected("mode_bad")
    _make_real(_session_for(client, headers), "PAPER")

    assert client.post("/api/v1/auth/choice/mode", headers=headers,
                       json={"mode": "sandbox"}).status_code == 422


def test_switching_without_a_session_is_refused(client, registered):
    headers, _ = registered("mode_none")
    assert client.post("/api/v1/auth/choice/mode", headers=headers,
                       json={"mode": "paper"}).status_code == 409
