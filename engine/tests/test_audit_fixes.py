"""Faults found by the August 2026 audit, each pinned so it cannot return.

Every test here corresponds to a defect that was live in the shipped binary.
The comments say what went wrong rather than what the code does, because the
code reads fine in all of these cases — that was the problem.
"""

import math
from unittest.mock import MagicMock, patch

import pytest

from engine.app.choice_gateway import funds as funds_gateway
from engine.app.choice_gateway import orders as orders_gateway
from engine.app.choice_gateway.client_manager import ChoiceSession, SessionMode
from engine.app.choice_gateway.errors import ChoiceUpstreamError, OrderRejected


def _live(owner: str) -> ChoiceSession:
    session = ChoiceSession(owner_key=owner)
    session.mode = SessionMode.LIVE
    session.session_id = "SESS-1"
    session.client = MagicMock()
    return session


# -- a POST must never be retried ------------------------------------------
#
# Choice's POSTs carry no idempotency key. A read timeout is "we do not know",
# not "it failed" — and the retry sent a second order and a second withdrawal.

def test_a_get_is_retried():
    from engine.app.choice_gateway.client_manager import TimeoutChoiceClient

    client = TimeoutChoiceClient.__new__(TimeoutChoiceClient)
    client.base_url = "https://x"
    client.get_headers = lambda include_auth=True: {}
    calls = []

    import requests
    with patch.object(requests, "request",
                      side_effect=lambda *a, **k: calls.append(1) or (_ for _ in ()).throw(
                          requests.exceptions.ConnectionError("boom"))):
        with pytest.raises(Exception):
            client.request("GET", "api/OpenAPI/Holdings")

    assert len(calls) > 1, "a GET should still be retried"


@pytest.mark.parametrize("endpoint", [
    "api/OpenAPI/V2/NewOrder",
    "api/OpenAPI/ProcessPayout",
    "api/OpenAPI/ModifyOrder",
])
def test_a_mutating_post_is_attempted_exactly_once(endpoint):
    """The one that mattered: a retried NewOrder is a second position, and a
    retried ProcessPayout is a second withdrawal."""
    from engine.app.choice_gateway.client_manager import TimeoutChoiceClient

    client = TimeoutChoiceClient.__new__(TimeoutChoiceClient)
    client.base_url = "https://x"
    client.get_headers = lambda include_auth=True: {}
    calls = []

    import requests
    with patch.object(requests, "request",
                      side_effect=lambda *a, **k: calls.append(1) or (_ for _ in ()).throw(
                          requests.exceptions.ReadTimeout("timeout"))):
        with pytest.raises(Exception):
            client.request("POST", endpoint, {})

    assert len(calls) == 1, f"{endpoint} was sent {len(calls)} times"


# -- an unknown order status is working, not finished ----------------------

@pytest.mark.parametrize("status", [
    "OPEN", "PENDING", "Trigger Pending", "PARTIALLY EXECUTED",
    "Some Status Choice Invented", "AMO Submitted", "Modified",
])
def test_an_unrecognised_status_is_treated_as_still_working(status):
    """This was an allowlist of open statuses, so any wording not on the list
    read as terminal and the kill switch skipped the order in silence.
    Attempting to cancel a finished order fails loudly; skipping a live one
    does not."""
    assert orders_gateway.is_open_status(status) is True


@pytest.mark.parametrize("status", [
    "EXECUTED", "Fully Executed", "COMPLETE", "CANCELLED", "REJECTED",
    "EXPIRED", "Lapsed", "",
])
def test_a_finished_order_is_not_working(status):
    assert orders_gateway.is_open_status(status) is False


# -- the kill switch must not mistake a refusal for an empty book ----------

def test_a_refused_order_book_raises_instead_of_reading_as_empty():
    """`unwrap_list` returns [] for a failure envelope exactly as it does for
    an empty book. The kill switch reported SUCCESS, cancelled 0, and left
    every order live at the exchange."""
    session = _live("book-fail")
    refusal = {"Status": "Fail", "Reason": "Session expired"}
    session.client.orders.get_order_book_v2.return_value = refusal
    session.client.orders.get_order_book.return_value = refusal

    with pytest.raises(ChoiceUpstreamError):
        orders_gateway.cancel_all_open(session)


def test_a_genuinely_empty_book_is_still_empty():
    session = _live("book-empty")
    empty = {"Status": "Success", "Response": {"Orders": []}}
    session.client.orders.get_order_book_v2.return_value = empty
    session.client.orders.get_order_book.return_value = empty

    assert orders_gateway.cancel_all_open(session)["cancelled"] == 0


# -- a cancellation must never guess which way the order went --------------

@pytest.mark.parametrize("raw,expected", [
    ({"BS": 1}, 1), ({"BS": 2}, 2),
    ({"BuySell": "B"}, 1), ({"BuySell": "S"}, 2),
    ({"BuySell": "SELL"}, 2),
])
def test_the_side_code_reads_letters_as_well_as_numbers(raw, expected):
    """`pick_int` cannot parse "S", so every lettered SELL fell to the default
    of 1 and was cancelled as a BUY — which the broker refuses, making those
    orders uncancellable including by the kill switch."""
    assert orders_gateway._side_code(raw) == expected


def test_an_unreadable_side_is_refused_rather_than_defaulted():
    with pytest.raises(OrderRejected, match="which side"):
        orders_gateway._side_code({"BuySell": "?"})


# -- a rejected order must not be recorded as accepted ---------------------

@pytest.mark.parametrize("response", [
    {"status": "Fail", "Reason": "Insufficient margin"},   # lowercase key
    {"Status": "false", "Reason": "Blocked"},              # "false" status
    {"Status": "Failure", "Reason": "Rejected"},
])
def test_every_failure_envelope_rejects_the_order(response):
    """The check here was hand-rolled: an exact-cased key defaulting to
    success, over a set missing "false". A refused order was recorded as
    accepted and the trader believed they held a position they did not."""
    session = _live("place-fail")
    # Placement goes through `client.request` directly now, so that the client
    # order number is ours rather than the SDK's hardcoded 123456.
    session.client.request.return_value = response

    with pytest.raises(OrderRejected):
        orders_gateway.place_order(
            session, segment_id=1, token=2885, order_type="RL_LIMIT",
            side="BUY", quantity=1, price=100.0)


def test_every_order_gets_its_own_client_order_number():
    """The SDK hardcodes 123456 on every order. Cancellation finds an order by
    that field, so two live orders sharing it meant a Cancel on one could
    withdraw the other."""
    session = _live("place-unique")
    session.client.request.return_value = {"Status": "Success"}

    numbers = []
    for _ in range(5):
        orders_gateway.place_order(
            session, segment_id=1, token=2885, order_type="RL_LIMIT",
            side="BUY", quantity=1, price=100.0)
        numbers.append(session.client.request.call_args[0][2]["ClientOrderNo"])

    assert len(set(numbers)) == 5, numbers
    assert 123456 not in numbers


def test_the_order_id_is_read_from_inside_the_response_envelope():
    """`pick_str` on the raw payload returned "" because Choice nests the reply
    under `Response` — which made every live order look missing to
    reconciliation, permanently."""
    session = _live("place-envelope")
    session.client.request.return_value = {
        "Status": "Success", "Response": {"ClientOrderNo": 987654321}}

    result = orders_gateway.place_order(
        session, segment_id=1, token=2885, order_type="RL_LIMIT",
        side="BUY", quantity=1, price=100.0)

    assert result["order_id"] == "987654321"


# -- money amounts -----------------------------------------------------------

@pytest.mark.parametrize("amount", [float("nan"), float("inf"), float("-inf")])
def test_a_non_finite_amount_is_refused(amount):
    """`nan <= 0` and `inf <= 0` are both False, so both passed a bare
    positivity check. Python's JSON parser accepts the literals, so both could
    reach a live payout endpoint."""
    session = _live("amt")
    with pytest.raises(ChoiceUpstreamError):
        funds_gateway.withdraw(session, amount, "12345678")
    session.client.funds.process_payout.assert_not_called()


def test_an_absurd_amount_is_refused():
    session = _live("amt-big")
    with pytest.raises(ChoiceUpstreamError, match="exceeds"):
        funds_gateway.withdraw(session, 1e300, "12345678")


def test_a_normal_amount_still_goes_through():
    session = _live("amt-ok")
    session.client.funds.process_payout.return_value = {"Status": "Success"}
    assert funds_gateway.withdraw(session, 5000, "12345678")["status"] == "SUCCESS"
    assert math.isclose(
        session.client.funds.process_payout.call_args.kwargs["amount"], 5000.0)


# -- cancelling must never resolve an ambiguous reference ------------------

def _book(rows):
    return {"Status": "Success", "Response": {"Orders": rows}}


def test_two_orders_sharing_a_reference_refuse_the_cancellation():
    """Orders placed before this platform issued its own numbers all carry the
    SDK's hardcoded 123456. Picking the first row is a coin toss over which
    position stays open, and the response reports success either way."""
    session = _live("cancel-ambiguous")
    rows = [
        {"ClientOrderNo": 123456, "Status": "OPEN", "TradingSymbol": "RELIANCE",
         "BS": 1, "Token": 2885, "SegmentId": 1},
        {"ClientOrderNo": 123456, "Status": "OPEN", "TradingSymbol": "INFY",
         "BS": 2, "Token": 1594, "SegmentId": 1},
    ]
    session.client.orders.get_order_book_v2.return_value = _book(rows)

    with pytest.raises(OrderRejected, match="share the reference"):
        orders_gateway.cancel_one(session, 123456)

    session.client.orders.cancel_order.assert_not_called()


def test_an_unambiguous_reference_still_cancels():
    session = _live("cancel-one-ok")
    session.client.orders.get_order_book_v2.return_value = _book([
        {"ClientOrderNo": 5551212, "Status": "OPEN", "TradingSymbol": "RELIANCE",
         "BS": 1, "Token": 2885, "SegmentId": 1},
    ])
    session.client.orders.cancel_order.return_value = {"Status": "Success"}

    assert orders_gateway.cancel_one(session, 5551212)["cancelled"] == 1


def test_an_order_can_be_cancelled_by_its_exchange_number():
    """The exchange number is broker-generated and unique, so it is the only
    reference that is trustworthy on legacy orders."""
    session = _live("cancel-by-exchange")
    session.client.orders.get_order_book_v2.return_value = _book([
        {"ClientOrderNo": 123456, "ExchangeOrderNo": "NSE-9988",
         "Status": "OPEN", "TradingSymbol": "RELIANCE", "BS": 1,
         "Token": 2885, "SegmentId": 1},
    ])
    session.client.orders.cancel_order.return_value = {"Status": "Success"}

    assert orders_gateway.cancel_one(session, "NSE-9988")["cancelled"] == 1


# -- the per-order value cap must apply to market orders too --------------

def test_a_market_order_is_valued_at_the_reference_price():
    """`quantity * price` with price 0 meant the value cap never applied to
    market orders — the one order type with no price ceiling of its own."""
    from engine.app.config import engine_settings

    cap = engine_settings.MAX_ORDER_VALUE
    over = int(cap / 1000) + 100          # 1000 rupees a share puts this over

    with pytest.raises(OrderRejected, match="exceeds"):
        orders_gateway.validate_order(
            segment_id=1, token=2885, order_type="RL_MKT", side="BUY",
            quantity=over, price=0.0, owner_key="cap-market",
            reference_price=1000.0)


def test_a_market_order_within_the_cap_still_passes():
    orders_gateway.validate_order(
        segment_id=1, token=2885, order_type="RL_MKT", side="BUY",
        quantity=1, price=0.0, owner_key="cap-ok", reference_price=1000.0)


def test_a_market_order_is_still_allowed_when_no_price_can_be_had():
    """A quote outage must not become an inability to trade; the quantity cap
    remains, and the gap is logged."""
    orders_gateway.validate_order(
        segment_id=1, token=2885, order_type="RL_MKT", side="BUY",
        quantity=1, price=0.0, owner_key="cap-noprice", reference_price=0.0)


# -- the kill switch must reach a running paper strategy -------------------

def _runner(owner: str):
    from engine.app.strategy_engine.runner import LiveStrategyRunner, RunMode

    session = ChoiceSession(owner_key=owner)
    session.mode = SessionMode.PAPER
    session.session_id = "SESS-1"
    dsl = {
        "indicators": {},
        "entry_conditions": [{"field": "close", "operator": ">", "value": 0}],
        "exit_conditions": [{"field": "close", "operator": "<", "value": 0}],
        "actions": {"buy_qty": 1},
    }
    runner = LiveStrategyRunner(
        run_id="run-" + owner, session=session, dsl_def=dsl,
        params={"segment_id": 1, "token": "2885", "symbol": "RELIANCE"},
        mode=RunMode.PAPER)
    runner.start()
    return runner


def _clear_halt(owner: str):
    from engine.app.strategy_engine.risk_manager import risk_manager
    risk_manager._halted.pop(owner, None)
    risk_manager._halt_scope.pop(owner, None)
    risk_manager._simulated.pop(owner, None)


def test_the_kill_switch_stops_a_running_paper_strategy():
    """The PAPER branch built its fill dict directly, so it never reached the
    risk manager. `POST /orders/halt` stopped manual paper orders while a paper
    strategy carried on entering and exiting."""
    from engine.app.strategy_engine.risk_manager import risk_manager

    runner = _runner("halt-paper")
    try:
        risk_manager.halt(runner.session.owner_key, "Kill switch used", scope="all")
        assert runner._submit("BUY", 100.0) is None
        assert runner.position_qty == 0
        assert any("halt" in e.lower() for e in runner.errors), runner.errors
    finally:
        _clear_halt(runner.session.owner_key)


def test_a_loss_cap_halt_still_leaves_a_paper_strategy_running():
    """Scope matters: a real-money loss cap must not stop paper work."""
    from engine.app.strategy_engine.risk_manager import risk_manager

    runner = _runner("losscap-paper")
    try:
        risk_manager.halt(runner.session.owner_key, "Daily loss cap", scope="real")
        assert runner._submit("BUY", 100.0) is not None
        assert runner.position_qty == 1
    finally:
        _clear_halt(runner.session.owner_key)


def test_a_paper_strategy_books_its_losses_against_the_paper_ledger():
    """Nothing wrote the paper ledger from a run, so the paper loss cap could
    never trip from the strategy that caused the losses."""
    from engine.app.strategy_engine.risk_manager import risk_manager

    runner = _runner("ledger-paper")
    try:
        runner._submit("BUY", 100.0)
        runner._submit("SELL", 90.0)          # a 10 rupee loss on 1 share

        booked = risk_manager._simulated[runner.session.owner_key]
        # Worse than the raw 10, because the run now pays slippage and charges
        # on both legs like the backtest does — but recognisably that trade.
        assert -12.0 < booked < -10.0, booked
        assert runner.total_charges > 0
        # And the real ledger is untouched.
        assert risk_manager._realized.get(runner.session.owner_key, 0.0) == 0.0
    finally:
        _clear_halt(runner.session.owner_key)


# -- a mode change must survive a restart ----------------------------------

def test_switching_to_paper_is_written_to_the_session_store(tmp_path, monkeypatch):
    """Only login used to persist, so switching LIVE to PAPER left "LIVE" on
    disk. `restore()` is reached from /auth/choice/status, so the next restart
    put the user back into live without the confirmation that guards it."""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setenv("SECRET_KEY", "k" * 48)
    from engine.app.choice_gateway import session_store

    session = ChoiceSession(owner_key="mode-persist")
    session.mode = SessionMode.LIVE
    session.session_id = "SESS-9"
    session.environment = "PROD"
    session.remember = True
    session.persist()
    assert session_store.load("mode-persist")["mode"] == "LIVE"

    session.mode = SessionMode.PAPER
    session.persist()

    assert session_store.load("mode-persist")["mode"] == "PAPER"
