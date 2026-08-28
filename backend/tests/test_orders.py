"""Order validation, sandbox isolation, persistence and audit."""

import pytest

BASE_ORDER = {
    "segment_id": 1, "token": "2885", "symbol": "RELIANCE",
    "side": "BUY", "order_type": "RL_MKT", "quantity": 10, "price_in_paisa": 0,
}


def order(client, headers, **overrides):
    return client.post("/api/v1/orders/", headers=headers,
                       json={**BASE_ORDER, **overrides})


def test_order_requires_a_choice_session(client, registered):
    headers, _ = registered("ord_nosession")
    assert order(client, headers).status_code == 409


def test_sandbox_order_is_simulated_locally(client, connected):
    """A sandbox order must never reach Choice."""
    headers, _ = connected("ord_demo")
    response = order(client, headers)

    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "DEMO"
    assert "No order was sent to Choice" in body["message"]
    assert body["order"]["status"] == "SIMULATED"


@pytest.mark.parametrize("overrides", [
    {"quantity": 0},
    {"quantity": -500},
    {"quantity": 10 ** 9},
    {"side": "SIDEWAYS"},
    {"order_type": "NOT_A_TYPE"},
    {"product_type": "XYZ"},
    {"token": "not-a-token"},
    {"price_in_paisa": -100},
    {"order_type": "RL_LIMIT", "price_in_paisa": 0},
])
def test_invalid_orders_are_rejected(client, connected, overrides):
    headers, _ = connected("ord_invalid")
    response = order(client, headers, **overrides)
    assert response.status_code in (400, 422), response.text


def test_notional_limit_is_enforced(client, connected):
    """RiskManager caps order value even when the request itself is well formed."""
    headers, _ = connected("ord_risk")
    response = order(client, headers, order_type="RL_LIMIT",
                     quantity=50000, price_in_paisa=250450)
    assert response.status_code == 400
    assert "exceeds the per-order limit" in response.json()["detail"]


def test_orders_are_persisted_and_listed(client, connected):
    headers, _ = connected("ord_list")
    assert order(client, headers).status_code == 200

    listed = client.get("/api/v1/orders/", headers=headers)
    assert listed.status_code == 200
    rows = listed.json()
    assert len(rows) >= 1
    assert rows[0]["symbol"] == "RELIANCE"
    assert rows[0]["execution_mode"] == "DEMO"


def test_rejected_orders_are_persisted_too(client, connected):
    """An order book that hides failures cannot be reconciled."""
    headers, _ = connected("ord_rejected")
    order(client, headers, order_type="RL_LIMIT", quantity=50000,
          price_in_paisa=250450)

    rows = client.get("/api/v1/orders/", headers=headers, params={"status": "REJECTED"}).json()
    assert len(rows) >= 1
    assert rows[0]["status"] == "REJECTED"
    assert rows[0]["failure_reason"]


def test_orders_are_tenant_scoped(client, connected):
    owner_headers, _ = connected("ord_owner")
    other_headers, _ = connected("ord_other")

    order(client, owner_headers, symbol="RELIANCE")
    other_rows = client.get("/api/v1/orders/", headers=other_headers).json()
    assert all(row["symbol"] != "RELIANCE" for row in other_rows)


def test_order_activity_is_audited(client, connected):
    from app.database import SessionLocal
    from app.models.audit import AuditLog

    headers, data = connected("ord_audit")
    order(client, headers)

    db = SessionLocal()
    try:
        actions = {
            row.action for row in
            db.query(AuditLog).filter(AuditLog.tenant_id == data["tenant_id"]).all()
        }
    finally:
        db.close()

    assert "ORDER_PLACED" in actions
    assert "CHOICE_SANDBOX_CONNECTED" in actions


def test_price_is_carried_as_paisa(client, connected):
    headers, _ = connected("ord_price")
    response = order(client, headers, order_type="RL_LIMIT", quantity=1,
                     price_in_paisa=250450)
    assert response.status_code == 200
    assert response.json()["order"]["price_in_paisa"] == 250450
