"""The platform's own books must reflect what actually happened.

Three faults, one theme. An order amended or cancelled at the broker left the
local record showing its original values forever — so the interface, the CSV
export used for accounting, and reconciliation were all confidently wrong.
Money moving left no audit trail at all. And the real-money daily loss cap was
updated only by a dashboard poll, so trading through the API never tripped it.
"""

import uuid
from unittest.mock import MagicMock, patch

import pytest

from app.database import SessionLocal
from app.dependencies import get_choice_session
from app.main import app
from app.models.audit import AuditLog
from app.models.order import Order
from engine.app.choice_gateway.client_manager import ChoiceSession, SessionMode


@pytest.fixture
def live(client, registered):
    """A registered user whose broker session is a live mock we can assert on.

    LIVE rather than the sandbox fixture: every path under test either reaches
    the broker or is supposed to refuse a simulated session, and neither is
    exercised by DEMO.
    """
    headers, _ = registered("books")
    me = client.get("/api/v1/auth/me", headers=headers).json()

    session = ChoiceSession(owner_key=me["id"])
    session.mode = SessionMode.LIVE
    session.session_id = "SESS-1"
    session.client = MagicMock()

    app.dependency_overrides[get_choice_session] = lambda: session
    db = SessionLocal()
    yield client, headers, me, session, db
    db.close()
    app.dependency_overrides.pop(get_choice_session, None)


def _order(db, me, reference="555001", tenant=None):
    record = Order(
        tenant_id=tenant or me["tenant_id"], user_id=me["id"],
        client_order_no=reference, symbol="RELIANCE", segment_id=1, token="2885",
        side="BUY", order_type="RL_LIMIT", product_type="CNC",
        quantity=10, price_in_paisa=240000, status="ACCEPTED",
        execution_mode="LIVE", source="MANUAL",
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return record


def _actions(db, tenant_id):
    return [a for (a,) in db.query(AuditLog.action)
            .filter(AuditLog.tenant_id == tenant_id).all()]


def _amend(client, headers, **overrides):
    body = {
        "client_order_no": 555001, "exchange_order_no": "E1",
        "gateway_order_no": "G1", "segment_id": 1, "token": "2885",
        "symbol": "RELIANCE", "side": "BUY", "order_type": "RL_LIMIT",
        "product_type": "CNC", "quantity": 200, "price_in_paisa": 250000,
    }
    body.update(overrides)
    return client.post("/api/v1/orders/modify", headers=headers, json=body)


# -- amending -----------------------------------------------------------------

def test_amending_updates_the_local_order_record(live):
    """It reported 10 @ 2400 forever, whatever the order actually became."""
    client, headers, me, session, db = live
    record = _order(db, me)
    session.client.orders.modify_order.return_value = {"Status": "Success"}

    assert _amend(client, headers).status_code == 200

    db.expire_all()
    updated = db.get(Order, record.id)
    assert updated.quantity == 200
    assert updated.price_in_paisa == 250000
    assert updated.status == "MODIFIED"
    assert "ORDER_MODIFIED" in _actions(db, me["tenant_id"])


# -- cancelling ---------------------------------------------------------------

def _book(reference=555001, status="OPEN"):
    return {"Status": "Success", "Response": {"Orders": [{
        "ClientOrderNo": reference, "Status": status,
        "TradingSymbol": "RELIANCE", "BS": 1, "Token": 2885, "SegmentId": 1}]}}


def test_cancelling_marks_the_local_record_cancelled(live):
    client, headers, me, session, db = live
    record = _order(db, me)
    session.client.orders.get_order_book_v2.return_value = _book()
    session.client.orders.cancel_order.return_value = {"Status": "Success"}

    response = client.post("/api/v1/orders/555001/cancel", headers=headers)

    assert response.status_code == 200, response.text
    db.expire_all()
    assert db.get(Order, record.id).status == "CANCELLED"
    assert "ORDER_CANCELLED" in _actions(db, me["tenant_id"])


def test_an_order_this_platform_never_placed_still_cancels(live):
    """An order placed from the Choice website has no row here. Acting on it
    must still work — no local record is a real answer, not an error."""
    client, headers, me, session, db = live
    session.client.orders.get_order_book_v2.return_value = _book(999999)
    session.client.orders.cancel_order.return_value = {"Status": "Success"}

    response = client.post("/api/v1/orders/999999/cancel", headers=headers)

    assert response.status_code == 200, response.text


def test_one_tenant_cannot_rewrite_another_tenants_record(live):
    """The lookup is tenant-scoped, so an order number that happens to collide
    cannot be used to rewrite someone else's book."""
    client, headers, me, session, db = live
    theirs = _order(db, me, reference="777001", tenant=str(uuid.uuid4()))
    session.client.orders.get_order_book_v2.return_value = _book(777001)
    session.client.orders.cancel_order.return_value = {"Status": "Success"}

    client.post("/api/v1/orders/777001/cancel", headers=headers)

    db.expire_all()
    assert db.get(Order, theirs.id).status == "ACCEPTED"


# -- money movement -----------------------------------------------------------

def test_a_withdrawal_is_audited(live):
    """A disputed payout had no record naming the actor, the amount or the
    destination — only a log line written before the call was even made."""
    client, headers, me, session, db = live
    session.client.funds.process_payout.return_value = {"Status": "Success"}

    response = client.post("/api/v1/portfolio/funds/withdraw", headers=headers,
                           json={"amount": 5000, "bank_acc_no": "12345678",
                                 "confirm": True})

    assert response.status_code == 200, response.text
    entry = (db.query(AuditLog)
             .filter(AuditLog.tenant_id == me["tenant_id"],
                     AuditLog.action == "FUNDS_WITHDRAWN").one())
    assert entry.details["amount"] == 5000
    assert entry.details["bank_acc_no"] == "12345678"
    assert entry.actor_id == me["id"]


def test_a_refused_withdrawal_is_audited_too(live):
    """A run of refusals is what a compromised token looks like."""
    client, headers, me, session, db = live
    session.client.funds.process_payout.return_value = {
        "Status": "Fail", "Reason": "Insufficient withdrawable balance"}

    client.post("/api/v1/portfolio/funds/withdraw", headers=headers,
                json={"amount": 5000, "bank_acc_no": "12345678", "confirm": True})

    assert "FUNDS_WITHDRAWAL_REFUSED" in _actions(db, me["tenant_id"])


def test_an_unconfirmed_withdrawal_never_reaches_the_broker(live):
    client, headers, me, session, db = live

    response = client.post("/api/v1/portfolio/funds/withdraw", headers=headers,
                           json={"amount": 5000, "bank_acc_no": "12345678"})

    assert response.status_code == 400
    session.client.funds.process_payout.assert_not_called()
    assert "FUNDS_WITHDRAWN" not in _actions(db, me["tenant_id"])


def test_a_deposit_is_audited(live):
    client, headers, me, session, db = live
    session.client.funds.payment_via_hdfc_upi.return_value = {"Status": "Success"}

    response = client.post("/api/v1/portfolio/funds/add", headers=headers,
                           json={"amount": 2500, "method": "upi",
                                 "bank_acc_no": "12345678",
                                 "user_vpa": "someone@bank"})

    assert response.status_code == 200, response.text
    assert "FUNDS_DEPOSIT_STARTED" in _actions(db, me["tenant_id"])


def test_a_position_conversion_is_audited(live):
    client, headers, me, session, db = live
    session.client.portfolio.position_conversion.return_value = {"Status": "Success"}

    response = client.post("/api/v1/portfolio/convert", headers=headers, json={
        "segment_id": 1, "token": 2885, "client_order_no": 1, "side": "BUY",
        "quantity": 10, "product_type": "CNC", "source_product_type": "MIS"})

    assert response.status_code == 200, response.text
    entry = (db.query(AuditLog)
             .filter(AuditLog.tenant_id == me["tenant_id"],
                     AuditLog.action == "POSITION_CONVERTED").one())
    assert entry.details["from"] == "MIS"
    assert entry.details["to"] == "CNC"


# -- the daily loss cap -------------------------------------------------------

def _session(owner, mode):
    session = ChoiceSession(owner_key=owner)
    session.mode = mode
    session.session_id = "SESS-1"
    session.client = MagicMock()
    return session


def test_a_live_order_refreshes_the_realised_pnl_ledger():
    """The cap was updated only by `GET /portfolio/funds`, which only the
    dashboard calls. Trading through the API left it at zero for ever, so the
    circuit breaker depended on a display poll."""
    from app.services.order_service import refresh_realized_pnl

    session = _session("cap-refresh", SessionMode.LIVE)
    with patch("engine.app.choice_gateway.funds.get_funds") as get_funds:
        refresh_realized_pnl(session)

    get_funds.assert_called_once_with(session)


def test_a_paper_order_does_not_touch_the_real_ledger():
    from app.services.order_service import refresh_realized_pnl

    session = _session("cap-paper", SessionMode.PAPER)
    with patch("engine.app.choice_gateway.funds.get_funds") as get_funds:
        refresh_realized_pnl(session)

    get_funds.assert_not_called()


def test_a_funds_failure_does_not_break_an_order_that_already_went_through():
    """This runs after the order is placed and recorded, so a funds call that
    fails must not turn a successful order into an error response."""
    from app.services.order_service import refresh_realized_pnl

    session = _session("cap-fail", SessionMode.LIVE)
    with patch("engine.app.choice_gateway.funds.get_funds",
               side_effect=Exception("Choice is down")):
        refresh_realized_pnl(session)          # must not raise
