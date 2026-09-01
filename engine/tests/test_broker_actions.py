"""The broker actions that used to require the Choice website.

Amending an order, converting a position, moving cash, reading the profile and
subscribing to the broadcast feed. Every one of these either moves money or
changes what happens to a position at the close, so the tests here are mostly
about what must be *refused*.
"""

from unittest.mock import MagicMock, patch

import pytest

from engine.app.choice_gateway import funds as funds_gateway
from engine.app.choice_gateway import market as market_gateway
from engine.app.choice_gateway import orders as orders_gateway
from engine.app.choice_gateway import portfolio as portfolio_gateway
from engine.app.choice_gateway import scrip_master, sockets_pricefeed
from engine.app.choice_gateway.client_manager import ChoiceSession, SessionMode
from engine.app.choice_gateway.errors import (
    ChoiceNotConnected,
    ChoiceUpstreamError,
    OrderRejected,
)


def _session(owner: str, mode: SessionMode = SessionMode.LIVE) -> ChoiceSession:
    session = ChoiceSession(owner_key=owner)
    session.mode = mode
    session.session_id = "SESS-1"
    session.access_token = "TOKEN"
    session.vendor_id = "M09984"
    session.client = MagicMock()
    return session


# -- modifying an order ----------------------------------------------------

def test_a_simulated_order_cannot_be_modified():
    """Simulated orders fill on placement, so none is ever working. Accepting
    the request and changing nothing would read as success."""
    session = _session("mod-paper", SessionMode.PAPER)

    with pytest.raises(OrderRejected, match="fill immediately"):
        orders_gateway.modify_order(
            session, client_order_no=1, exchange_order_no="E1",
            gateway_order_no="G1", segment_id=1, token=2885,
            order_type="RL_LIMIT", side="BUY", quantity=10, price=100.0,
        )

    session.client.orders.modify_order.assert_not_called()


def test_modifying_is_validated_as_strictly_as_placing():
    """Raising the quantity of a working order raises exposure exactly as a
    new order does, so the same limits apply."""
    session = _session("mod-limits")

    with pytest.raises(OrderRejected):
        orders_gateway.modify_order(
            session, client_order_no=1, exchange_order_no="E1",
            gateway_order_no="G1", segment_id=1, token=2885,
            order_type="RL_LIMIT", side="BUY",
            quantity=10_000_000, price=100_000.0,     # far past any budget
        )

    session.client.orders.modify_order.assert_not_called()


def test_a_halt_stops_a_modification_too():
    """A kill switch that stops new orders but lets a working one be repriced
    upward is not a kill switch."""
    from engine.app.strategy_engine.risk_manager import risk_manager

    session = _session("mod-halt")
    risk_manager.halt(session.owner_key, "Kill switch used")
    try:
        with pytest.raises(OrderRejected, match="halted"):
            orders_gateway.modify_order(
                session, client_order_no=1, exchange_order_no="E1",
                gateway_order_no="G1", segment_id=1, token=2885,
                order_type="RL_LIMIT", side="BUY", quantity=1, price=100.0,
            )
    finally:
        # Halts clear on the day roll, not by an API call, so the fixture
        # undoes it directly rather than leaking into every later test.
        risk_manager._halted.pop(session.owner_key, None)
        risk_manager._halt_scope.pop(session.owner_key, None)


def test_a_successful_modification_reaches_choice_with_the_side_code():
    session = _session("mod-ok")
    session.client.orders.modify_order.return_value = {"Status": "Success",
                                                       "ClientOrderNo": "77"}

    result = orders_gateway.modify_order(
        session, client_order_no=77, exchange_order_no="E1",
        gateway_order_no="G1", segment_id=1, token=2885,
        order_type="RL_LIMIT", side="SELL", quantity=5, price=2500.0,
    )

    assert result["status"] == "SUCCESS"
    assert session.client.orders.modify_order.call_args.kwargs["bs"] == 2
    assert session.client.orders.modify_order.call_args.kwargs["qty"] == 5


def test_a_rejected_modification_raises_rather_than_returning_success():
    session = _session("mod-reject")
    session.client.orders.modify_order.return_value = {
        "Status": "Fail", "Reason": "Order already traded"}

    with pytest.raises(OrderRejected, match="already traded"):
        orders_gateway.modify_order(
            session, client_order_no=1, exchange_order_no="E1",
            gateway_order_no="G1", segment_id=1, token=2885,
            order_type="RL_LIMIT", side="BUY", quantity=1, price=100.0,
        )


# -- converting a position -------------------------------------------------

def test_converting_to_the_same_product_is_refused():
    """A no-op reported as success would let someone believe an MIS position
    is now delivery, and find out at the close that it was squared off."""
    session = _session("conv-same")

    with pytest.raises(ChoiceUpstreamError, match="already"):
        portfolio_gateway.convert_position(
            session, segment_id=1, token=2885, client_order_no=1, side="BUY",
            quantity=10, product_type="MIS", source_product_type="MIS")


def test_a_simulated_session_has_no_position_to_convert():
    session = _session("conv-paper", SessionMode.PAPER)

    with pytest.raises(ChoiceUpstreamError, match="simulated"):
        portfolio_gateway.convert_position(
            session, segment_id=1, token=2885, client_order_no=1, side="BUY",
            quantity=10, product_type="CNC", source_product_type="MIS")


def test_conversion_sends_both_the_source_and_the_target():
    """Choice needs to be told what the position is now as well as what it
    should become; inferring the source is how a conversion goes backwards."""
    session = _session("conv-ok")
    session.client.portfolio.position_conversion.return_value = {"Status": "Success"}

    portfolio_gateway.convert_position(
        session, segment_id=1, token=2885, client_order_no=1, side="BUY",
        quantity=10, product_type="CNC", source_product_type="MIS")

    kwargs = session.client.portfolio.position_conversion.call_args.kwargs
    assert kwargs["source_product_type"] == "MIS"
    assert kwargs["product_type"] == "CNC"
    assert kwargs["buy_sell"] == 1


# -- moving money ----------------------------------------------------------

@pytest.mark.parametrize("mode", [SessionMode.PAPER, SessionMode.DEMO])
def test_a_simulated_session_cannot_withdraw(mode):
    """Paper trading exists so a mistake costs nothing. A real transfer from a
    paper session would break that promise completely."""
    session = _session("cash-sim", mode)

    with pytest.raises(ChoiceUpstreamError, match="simulated"):
        funds_gateway.withdraw(session, 1000, "123456")

    session.client.funds.process_payout.assert_not_called()


@pytest.mark.parametrize("mode", [SessionMode.PAPER, SessionMode.DEMO])
def test_a_simulated_session_cannot_deposit(mode):
    session = _session("cash-sim-add", mode)

    with pytest.raises(ChoiceUpstreamError, match="simulated"):
        funds_gateway.add_funds(session, 1000, "upi", "123456",
                                user_vpa="a@bank")


@pytest.mark.parametrize("amount", [0, -1, -0.01, "abc", None])
def test_a_withdrawal_must_be_a_positive_amount(amount):
    session = _session("cash-amount")

    with pytest.raises(ChoiceUpstreamError):
        funds_gateway.withdraw(session, amount, "123456")

    session.client.funds.process_payout.assert_not_called()


def test_a_withdrawal_needs_a_bank_account():
    session = _session("cash-nobank")

    with pytest.raises(ChoiceUpstreamError, match="bank account"):
        funds_gateway.withdraw(session, 500, "   ")


def test_a_withdrawal_reaches_choice_with_the_rounded_amount():
    session = _session("cash-ok")
    session.client.funds.process_payout.return_value = {"Status": "Success"}

    result = funds_gateway.withdraw(session, 1000.006, "123456")

    assert result["status"] == "SUCCESS"
    assert session.client.funds.process_payout.call_args.kwargs["amount"] == 1000.01


def test_a_rejected_payout_raises():
    session = _session("cash-reject")
    session.client.funds.process_payout.return_value = {
        "Status": "Fail", "Reason": "Insufficient withdrawable balance"}

    with pytest.raises(ChoiceUpstreamError) as raised:
        funds_gateway.withdraw(session, 500, "123456")

    # The reason is what tells a user why. `register_exception_handlers`
    # appends it to the message as "Choice said: ...", so losing it here would
    # leave the user with "Choice rejected the payout" and nothing else.
    assert "Insufficient withdrawable balance" in raised.value.details


def test_an_unknown_payment_method_is_refused_before_any_call():
    session = _session("cash-method")

    with pytest.raises(ChoiceUpstreamError, match="Unknown payment method"):
        funds_gateway.add_funds(session, 100, "bitcoin", "123456")


@pytest.mark.parametrize("vpa", ["notavpa", "", "   "])
def test_a_upi_deposit_needs_a_real_vpa(vpa):
    session = _session("cash-vpa")

    with pytest.raises(ChoiceUpstreamError, match="UPI id"):
        funds_gateway.add_funds(session, 100, "upi", "123456", user_vpa=vpa)


def test_a_netbanking_deposit_needs_an_ifsc():
    session = _session("cash-ifsc")

    with pytest.raises(ChoiceUpstreamError, match="IFSC"):
        funds_gateway.add_funds(session, 100, "netbanking", "123456")


def test_a_upi_deposit_reaches_choice():
    session = _session("cash-upi-ok")
    session.client.funds.payment_via_hdfc_upi.return_value = {"Status": "Success"}

    result = funds_gateway.add_funds(session, 2500, "UPI", "123456",
                                     user_vpa="user@bank")

    assert result["method"] == "upi"
    assert session.client.funds.payment_via_hdfc_upi.call_args.kwargs["amount"] == 2500.0


# -- lot size --------------------------------------------------------------

@pytest.mark.parametrize("field", ["MarketLot", "LotSize", "lot_size"])
def test_the_lot_size_is_read_from_whichever_field_choice_used(field):
    session = _session("lot")
    session.client.scrip_master.get_details.return_value = {field: "50"}

    assert scrip_master.get_lot_size(session, "35000") == 50


def test_an_instrument_with_no_lot_field_is_treated_as_cash():
    """A cash instrument legitimately has no lot. Refusing to size an order
    because a field is absent would be worse than assuming the common case."""
    session = _session("lot-missing")
    session.client.scrip_master.get_details.return_value = {"SecDesc": "RELIANCE"}

    assert scrip_master.get_lot_size(session, "2885") == 1


def test_a_nonsense_lot_value_does_not_become_the_lot():
    session = _session("lot-junk")
    session.client.scrip_master.get_details.return_value = {"MarketLot": "N/A"}

    assert scrip_master.get_lot_size(session, "2885") == 1


# -- the broadcast price feed ----------------------------------------------

def test_paper_sessions_may_use_the_price_feed():
    """The bug this replaces: the feed gated on `is_live`, which means "sends
    real orders". Paper is the mode that most needs a quote source."""
    session = _session("feed-paper", SessionMode.PAPER)
    connection = sockets_pricefeed.PriceFeedConnection(session)

    with patch("choice_api.PriceFeedSocketClient", create=True) as Client:
        connection.start()

    assert connection.is_running
    Client.return_value.start_websocket.assert_called_once()


def test_a_sandbox_session_still_cannot_use_the_price_feed():
    """Widening the gate must not widen it to sessions with no broker at all."""
    session = ChoiceSession(owner_key="feed-demo")
    session.mode = SessionMode.DEMO

    with pytest.raises(ChoiceNotConnected):
        sockets_pricefeed.PriceFeedConnection(session).start()


def test_depth_and_touchline_are_separate_subscriptions():
    """Keyed identically, asking for depth on an instrument already subscribed
    for touchline would silently do nothing."""
    session = _session("feed-depth")
    connection = sockets_pricefeed.PriceFeedConnection(session)

    with patch("choice_api.PriceFeedSocketClient", create=True) as Client:
        connection.start()
        instrument = [{"segment_id": 1, "token": 2885}]
        assert connection.subscribe(instrument) == ["1_2885"]
        assert connection.subscribe(instrument, depth=True) == ["1_2885:depth"]
        assert connection.subscribe(instrument) == []          # already on

    assert Client.return_value.subscribe_touchline.call_count == 1
    assert Client.return_value.subscribe_best_five.call_count == 1


def test_the_feed_prefers_the_address_choice_supplied_at_logon():
    """The feed handler address is per-session, not a fixed URL. Using the
    vendor default when Choice named a host would connect to the wrong one."""
    session = _session("feed-addr")
    session.client.bcast_ip = "10.20.30.40"
    session.client.bcast_port = 4521

    endpoint = sockets_pricefeed.feed_endpoint(session)

    assert endpoint["host"] == "wss://10.20.30.40"
    assert endpoint["port"] == 4521
    assert endpoint["source"] == "logon_response"


def test_no_supplied_address_is_reported_as_a_fallback_not_a_fact():
    """`vendor_default` is the signal that this account may not be provisioned
    for the feed at all, so it must stay distinguishable."""
    session = _session("feed-noaddr")
    session.client.bcast_ip = None
    session.client.bcast_port = None

    assert sockets_pricefeed.feed_endpoint(session)["source"] == "vendor_default"


# -- profile ---------------------------------------------------------------

def test_the_profile_is_unwrapped_from_the_choice_envelope():
    session = _session("profile")
    session.client.market.get_user_profile.return_value = {
        "Status": "Success", "Response": {"ClientId": "M09984", "ClientName": "A"}}

    result = market_gateway.get_user_profile(session)

    assert result["data"]["ClientId"] == "M09984"


def test_a_rejected_profile_request_raises():
    session = _session("profile-fail")
    session.client.market.get_user_profile.return_value = {
        "Status": "Fail", "Reason": "Not authorised"}

    with pytest.raises(ChoiceUpstreamError) as raised:
        market_gateway.get_user_profile(session)

    assert "Not authorised" in raised.value.details


# -- a paper price must be a real price ------------------------------------
#
# Found by running the diagnostic against a live PAPER session: MultipleTouchline
# failed, and quotes silently fell back to a fixture table written months
# earlier. RELIANCE was quoted at 2504.50 while Choice's own holdings record
# said 1324.10, and paper orders filled at the fiction. A paper trade exists to
# answer "what would this strategy have done"; an invented fill price makes the
# answer worthless while looking exactly like a working system.

def _paper_with_dead_touchline(owner: str):
    session = _session(owner, SessionMode.PAPER)
    session.client.market.get_multiple_touchline.side_effect = Exception(
        "Index was outside the bounds of the array.")
    return session


HELD = {"status": "SUCCESS", "data": [
    {"symbol": "RELIANCE-EQ", "segment_id": 1, "token": "2885",
     "current_price": 1324.10, "close_price": 1316.00},
]}


def test_a_paper_quote_never_falls_back_to_the_fixture_table():
    session = _paper_with_dead_touchline("quote-fixture")

    with patch("engine.app.choice_gateway.portfolio.get_holdings",
               return_value={"status": "SUCCESS", "data": []}):
        with pytest.raises(ChoiceUpstreamError):
            market_gateway.get_multiple_touchline(session, "1_2885")


def test_a_held_instrument_is_priced_from_the_holdings_snapshot():
    """Holdings answers on an account where touchline does not, and carries a
    real scaled LTP. That is a true price, and it is available today."""
    session = _paper_with_dead_touchline("quote-holdings")

    with patch("engine.app.choice_gateway.portfolio.get_holdings",
               return_value=HELD):
        result = market_gateway.get_multiple_touchline(session, "1_2885")

    assert result["source"] == "holdings_snapshot"
    assert result["data"][0]["ltp"] == 1324.10
    # Emphatically not the fixture value.
    assert result["data"][0]["ltp"] != 2504.50
    # Health used to stay on "Not checked yet" because this path returned
    # without flipping market_data_ok.
    assert session.market_data_ok is True


def test_the_holdings_price_carries_the_day_change():
    session = _paper_with_dead_touchline("quote-change")

    with patch("engine.app.choice_gateway.portfolio.get_holdings",
               return_value=HELD):
        quote = market_gateway.get_multiple_touchline(session, "1_2885")["data"][0]

    assert quote["chg"] == 8.10
    assert quote["chg_pct"] == 0.62


def test_an_instrument_that_is_not_held_is_refused_rather_than_invented():
    """The holdings snapshot only covers what the account holds. Everything
    else has no real price, and no price is the correct answer."""
    session = _paper_with_dead_touchline("quote-unheld")

    with patch("engine.app.choice_gateway.portfolio.get_holdings",
               return_value=HELD):
        with pytest.raises(ChoiceUpstreamError, match="not in"):
            market_gateway.get_multiple_touchline(session, "1_1594")


def test_a_failed_quote_marks_market_data_unhealthy():
    """The health panel has to be able to say why paper runs stopped."""
    session = _paper_with_dead_touchline("quote-health")

    with patch("engine.app.choice_gateway.portfolio.get_holdings",
               return_value={"status": "SUCCESS", "data": []}):
        with pytest.raises(ChoiceUpstreamError):
            market_gateway.get_multiple_touchline(session, "1_2885")

    assert session.market_data_ok is False


def test_a_sandbox_session_still_uses_its_fixtures():
    """The fixture table is correct for DEMO, which has no broker at all. Only
    a session holding real credentials must never see it."""
    session = ChoiceSession(owner_key="quote-demo")
    session.mode = SessionMode.DEMO

    result = market_gateway.get_multiple_touchline(session, "1_2885")

    assert result["mode"] == "DEMO"
    assert result["data"][0]["ltp"] == 2504.50
    assert session.market_data_ok is True
