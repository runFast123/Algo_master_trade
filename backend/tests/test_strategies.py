"""Strategy CRUD, DSL validation, and backtest behaviour."""

import pytest

VALID_DSL = {
    "indicators": {"rsi_14": {"type": "RSI", "length": 14}},
    "entry_conditions": [{"field": "rsi_14", "operator": "<", "value": 35}],
    "exit_conditions": [{"field": "rsi_14", "operator": ">", "value": 65}],
    "actions": {"buy_qty": 10},
}


def create_strategy(client, headers, dsl=None, name="RSI mean reversion"):
    return client.post("/api/v1/strategies/", headers=headers, json={
        "name": name, "description": "test strategy",
        "dsl_definition": dsl or VALID_DSL,
    })


def test_strategy_crud(client, connected):
    headers, _ = connected("crud")

    created = create_strategy(client, headers)
    assert created.status_code == 201
    strategy_id = created.json()["id"]

    listed = client.get("/api/v1/strategies/", headers=headers)
    assert listed.status_code == 200
    assert any(s["id"] == strategy_id for s in listed.json())

    updated = client.put(f"/api/v1/strategies/{strategy_id}", headers=headers,
                         json={"name": "Renamed"})
    assert updated.status_code == 200
    assert updated.json()["name"] == "Renamed"

    assert client.delete(f"/api/v1/strategies/{strategy_id}",
                         headers=headers).status_code == 204
    assert client.get(f"/api/v1/strategies/{strategy_id}",
                      headers=headers).status_code == 404


def test_preview_describes_a_draft_without_saving(client, connected):
    """The builder's preview is the only thing standing between someone who
    does not read JSON and a strategy that does the opposite of what they
    meant, so it has to describe the definition the engine will actually run."""
    headers, _ = connected("prev")

    res = client.post("/api/v1/strategies/preview", headers=headers,
                      json={"dsl_definition": VALID_DSL, "symbol": "RELIANCE"})
    assert res.status_code == 200
    body = res.json()
    assert body["valid"] is True and body["error"] is None
    assert "RELIANCE" in body["explanation"]
    assert "RSI" in body["explanation"]

    assert client.get("/api/v1/strategies/", headers=headers).json() == []


def test_preview_reports_an_invalid_draft_instead_of_rejecting_it(client, connected):
    """A half-built strategy is the normal state of the builder. Answering with
    a 4xx would make the preview go blank exactly while it is most needed."""
    headers, _ = connected("prev_bad")

    res = client.post("/api/v1/strategies/preview", headers=headers, json={
        "dsl_definition": {"indicators": {}, "entry_conditions": [], "exit_conditions": []}})

    assert res.status_code == 200
    assert res.json()["valid"] is False
    assert "entry condition" in res.json()["error"]


def test_preview_refuses_an_unknown_field_exactly_as_saving_would(client, connected):
    """The preview is only worth trusting if it agrees with the save. A draft
    the preview calls valid must not then fail on create."""
    headers, _ = connected("prev_agree")
    broken = {**VALID_DSL, "entry_conditions": [{"field": "typo_14", "operator": "<", "value": 1}]}

    preview = client.post("/api/v1/strategies/preview", headers=headers,
                          json={"dsl_definition": broken}).json()
    saved = create_strategy(client, headers, dsl=broken)

    assert preview["valid"] is False
    assert saved.status_code >= 400


def test_preview_requires_a_signed_in_user(client):
    assert client.post("/api/v1/strategies/preview",
                       json={"dsl_definition": VALID_DSL}).status_code in (401, 403)


def test_strategies_are_tenant_scoped(client, connected, registered):
    owner_headers, _ = connected("owner_s")
    other_headers, _ = registered("other_s")

    strategy_id = create_strategy(client, owner_headers).json()["id"]
    response = client.get(f"/api/v1/strategies/{strategy_id}", headers=other_headers)
    assert response.status_code == 404


@pytest.mark.parametrize("dsl, reason", [
    ({"indicators": {"x": {"type": "NOPE", "length": 5}},
      "entry_conditions": [{"field": "x", "operator": "<", "value": 1}],
      "exit_conditions": [{"field": "x", "operator": ">", "value": 9}],
      "actions": {"buy_qty": 1}}, "unknown indicator type"),
    ({"indicators": {"rsi": {"type": "RSI", "length": 14}},
      "entry_conditions": [{"field": "typo", "operator": "<", "value": 30}],
      "exit_conditions": [{"field": "rsi", "operator": ">", "value": 70}],
      "actions": {"buy_qty": 1}}, "unknown field"),
    ({"indicators": {"rsi": {"type": "RSI", "length": 14}},
      "entry_conditions": [{"field": "rsi", "operator": "~~", "value": 30}],
      "exit_conditions": [{"field": "rsi", "operator": ">", "value": 70}],
      "actions": {"buy_qty": 1}}, "unknown operator"),
    ({"indicators": {"rsi": {"type": "RSI", "length": 14}},
      "entry_conditions": [{"field": "rsi", "operator": "<", "value": 30}],
      "exit_conditions": [],
      "actions": {"buy_qty": 1}}, "no exit condition"),
])
def test_invalid_strategies_are_rejected(client, connected, dsl, reason):
    """A broken strategy fails on save, not silently at run time."""
    headers, _ = connected("dsl")
    response = create_strategy(client, headers, dsl=dsl)
    assert response.status_code == 422, f"expected rejection for {reason}"


def test_backtest_requires_a_choice_session(client, registered):
    headers, _ = registered("bt_nosession")
    created = create_strategy(client, headers)
    assert created.status_code == 201
    response = client.post(
        f"/api/v1/strategies/{created.json()['id']}/backtest", headers=headers,
        json={"symbol": "RELIANCE", "segment_id": 1, "timeframe": "1d",
              "start_date": "2025-01-01", "end_date": "2025-03-31",
              "initial_capital": 100000},
    )
    assert response.status_code == 409


def test_backtest_returns_computed_metrics(client, connected):
    """Metrics must come from the run, not from a canned fallback."""
    headers, _ = connected("bt")
    strategy_id = create_strategy(client, headers).json()["id"]

    response = client.post(
        f"/api/v1/strategies/{strategy_id}/backtest", headers=headers,
        json={"symbol": "RELIANCE", "segment_id": 1, "timeframe": "1d",
              "start_date": "2025-01-01", "end_date": "2025-06-30",
              "initial_capital": 100000},
    )
    assert response.status_code == 200
    run = response.json()
    metrics = run["metrics"]

    assert run["status"] == "COMPLETED"
    # The old fallback always produced exactly these numbers.
    assert not (metrics["return_pct"] == 15.0 and metrics["win_rate"] == 66.7)
    assert metrics["initial_capital"] == 100000.0
    for key in ("max_drawdown_pct", "sharpe_ratio", "total_charges", "equity_curve"):
        assert key in metrics
    # Sandbox data must be labelled so it is never read as exchange data.
    assert run["data_source"] == "SANDBOX_SYNTHETIC"
    assert any("synthetic" in line.lower() for line in run["logs"])


def test_backtest_uses_the_requested_symbol(client, connected):
    """Different instruments must produce different results."""
    headers, _ = connected("bt_symbol")
    strategy_id = create_strategy(client, headers).json()["id"]

    def run(symbol):
        return client.post(
            f"/api/v1/strategies/{strategy_id}/backtest", headers=headers,
            json={"symbol": symbol, "segment_id": 1, "timeframe": "1d",
                  "start_date": "2025-01-01", "end_date": "2025-06-30",
                  "initial_capital": 100000},
        ).json()["metrics"]

    assert run("RELIANCE")["total_pnl"] != run("INFY")["total_pnl"]


def test_backtest_rejects_a_reversed_date_range(client, connected):
    headers, _ = connected("bt_dates")
    strategy_id = create_strategy(client, headers).json()["id"]
    response = client.post(
        f"/api/v1/strategies/{strategy_id}/backtest", headers=headers,
        json={"symbol": "RELIANCE", "segment_id": 1, "timeframe": "1d",
              "start_date": "2025-06-30", "end_date": "2025-01-01",
              "initial_capital": 100000},
    )
    assert response.status_code == 422


# -- paper runs ------------------------------------------------------------
#
# Starting a run checks that the instrument prices *now*. An earlier version
# gated on a cached per-session flag, which went stale in both directions: one
# transient quote failure blocked every later run, and the interface reported
# "no market-data entitlement" for something it had never established.

import pytest

from engine.app.choice_gateway.client_manager import SessionMode, choice_sessions
from engine.app.choice_gateway.errors import ChoiceUpstreamError

RSI_DSL = {
    "indicators": {"r": {"type": "RSI", "length": 14}},
    "entry_conditions": [{"field": "r", "operator": "<", "value": 30}],
    "exit_conditions": [{"field": "r", "operator": ">", "value": 70}],
    "actions": {"buy_qty": 1},
}


@pytest.fixture
def paper_user(client, registered):
    """A connected session forced into PAPER, so run guards are reachable."""
    headers, data = registered("paperrun")
    client.post("/api/v1/auth/choice/connect", headers=headers,
                json={"mode": "sandbox", "vendor_id": "DEMO",
                      "api_key": "DEMO", "mobile_no": ""})
    session = choice_sessions.get(str(data["user_id"]))
    session.mode = SessionMode.PAPER
    strategy_id = client.post("/api/v1/strategies/", headers=headers,
                              json={"name": "Run me",
                                    "dsl_definition": RSI_DSL}).json()["id"]
    return headers, session, strategy_id


def test_a_stale_refusal_does_not_block_a_run_that_can_be_priced(
    client, paper_user, monkeypatch
):
    """The flag says the last quote failed; the instrument prices fine now.
    The run must start."""
    import app.services.paper_run_service as prs

    headers, session, strategy_id = paper_user
    session.market_data_ok = False                    # stale, from earlier
    monkeypatch.setattr(prs.market_gateway, "get_multiple_touchline",
                        lambda s, t: {"data": [{"ltp": 1314.90}]})
    monkeypatch.setattr(prs, "resolve_instrument",
                        lambda s, sym, seg, tok: {"segment_id": 1, "token": "2885"})
    monkeypatch.setattr(prs.paper_scheduler, "start", lambda *a, **k: None)

    response = client.post(f"/api/v1/strategies/{strategy_id}/start",
                           headers=headers, json={"symbol": "RELIANCE"})

    assert response.status_code == 200, response.text
    assert response.json()["state"] == "RUNNING"


def test_a_refusal_quotes_choice_rather_than_asserting_no_entitlement(
    client, paper_user, monkeypatch
):
    import app.services.paper_run_service as prs

    headers, session, strategy_id = paper_user
    monkeypatch.setattr(prs, "resolve_instrument",
                        lambda s, sym, seg, tok: {"segment_id": 1, "token": "2885"})

    def refuses(_session, _tokens):
        raise ChoiceUpstreamError("Choice could not return quotes",
                                  "Market data subscription not active")

    monkeypatch.setattr(prs.market_gateway, "get_multiple_touchline", refuses)

    response = client.post(f"/api/v1/strategies/{strategy_id}/start",
                           headers=headers, json={"symbol": "RELIANCE"})

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert "subscription not active" in detail      # Choice's own words
    assert "entitlement" not in detail.lower()      # not our assumption


def test_a_priceless_quote_is_refused_with_a_next_step(
    client, paper_user, monkeypatch
):
    import app.services.paper_run_service as prs

    headers, session, strategy_id = paper_user
    monkeypatch.setattr(prs, "resolve_instrument",
                        lambda s, sym, seg, tok: {"segment_id": 1, "token": "2885"})
    monkeypatch.setattr(prs.market_gateway, "get_multiple_touchline",
                        lambda s, t: {"data": [{"ltp": None}]})

    response = client.post(f"/api/v1/strategies/{strategy_id}/start",
                           headers=headers, json={"symbol": "RELIANCE"})

    assert response.status_code == 409
    assert "diagnostics" in response.json()["detail"].lower()


def test_a_sandbox_session_still_cannot_run(client, registered):
    """Sandbox prices are fixtures. A strategy run on them would look real."""
    headers, _ = registered("sandboxrun")
    client.post("/api/v1/auth/choice/connect", headers=headers,
                json={"mode": "sandbox", "vendor_id": "DEMO",
                      "api_key": "DEMO", "mobile_no": ""})
    strategy_id = client.post("/api/v1/strategies/", headers=headers,
                              json={"name": "S", "dsl_definition": RSI_DSL}).json()["id"]

    response = client.post(f"/api/v1/strategies/{strategy_id}/start",
                           headers=headers, json={"symbol": "RELIANCE"})

    assert response.status_code == 409
    assert "sandbox" in response.json()["detail"].lower()
