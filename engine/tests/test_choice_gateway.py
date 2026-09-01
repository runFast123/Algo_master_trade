"""End-to-end Choice gateway behaviour with a mocked SDK client.

These cover the live PAPER/LIVE paths that backtests and the dashboard
actually call: scrip resolution, funds, holdings, order book, market
status, and touchline. Demo fixtures must never leak into a connected
account.
"""

from unittest.mock import MagicMock

import pytest

from engine.app.choice_gateway import funds as funds_gateway
from engine.app.choice_gateway import market as market_gateway
from engine.app.choice_gateway import orders as orders_gateway
from engine.app.choice_gateway import portfolio as portfolio_gateway
from engine.app.choice_gateway import scrip_master
from engine.app.choice_gateway.client_manager import ChoiceSession, SessionMode
from engine.app.choice_gateway.errors import (
    ChoiceNotConnected,
    ChoiceUpstreamError,
)


def _connected(owner: str, mode: SessionMode = SessionMode.PAPER) -> ChoiceSession:
    session = ChoiceSession(owner)
    session.mode = mode
    session.session_id = "SESS-1"
    session.access_token = "TOKEN"
    session.vendor_id = "M09984"
    session.client = MagicMock()
    session.client.scrip_master.is_loaded = True
    return session


def _demo(owner: str = "demo") -> ChoiceSession:
    session = ChoiceSession(owner)
    session.start_demo("DEMO")
    return session


# -- scrip master -----------------------------------------------------------


def test_demo_search_filters_sandbox_scrips():
    result = scrip_master.search_scrip(_demo(), "REL")
    assert result["mode"] == "DEMO"
    symbols = {row["symbol"] for row in result["data"]}
    assert "RELIANCE" in symbols
    assert "TCS" not in symbols


def test_live_search_calls_choice_scrip_master():
    session = _connected("search-live", SessionMode.LIVE)
    session.client.scrip_master.search.return_value = {
        "data": [{"symbol": "RELIANCE", "token": "2885"}]
    }
    result = scrip_master.search_scrip(session, "RELIANCE")
    assert result["status"] == "SUCCESS"
    session.client.scrip_master.search.assert_called_once()
    assert session.client.scrip_master.search.call_args.kwargs["name"] == "RELIANCE"


def test_get_token_picks_equity_from_choice_list():
    session = _connected("token-list")
    session.client.scrip_master.get_token.return_value = [
        {"Token": "144404", "Segment": "2", "Symbol": "RELIANCE",
         "SecDesc": "RELIANCE26SEP1380CE", "Series": "XX"},
        {"Token": "2885", "Segment": "1", "Symbol": "RELIANCE",
         "SecDesc": "RELIANCE INDUSTRIES LTD", "Series": "EQ"},
    ]
    assert scrip_master.get_token(session, "RELIANCE") == "2885"


def test_get_token_demo_returns_sandbox_token():
    assert scrip_master.get_token(_demo(), "RELIANCE") == "2885"
    assert scrip_master.get_token(_demo(), "UNKNOWNXYZ") is None


def test_resolve_known_sandbox_symbol_without_token():
    resolved = scrip_master.resolve_instrument(_demo(), "INFY", None, None)
    assert resolved["token"] == "1594"
    assert resolved["segment_id"] == 1


def test_unknown_instrument_fails_loudly_instead_of_using_symbol_as_token():
    session = _connected("unknown-scrip")
    session.client.scrip_master.get_token.return_value = None
    with pytest.raises(ChoiceUpstreamError, match="Unknown instrument"):
        scrip_master.resolve_instrument(session, "NOTAREALCO", None, None)


def test_explicit_token_is_honoured():
    session = _connected("explicit-token")
    resolved = scrip_master.resolve_instrument(session, "RELIANCE", 1, "2885")
    assert resolved["token"] == "2885"
    session.client.scrip_master.get_token.assert_not_called()


def test_connected_resolve_uses_choice_token():
    session = _connected("resolve-live")
    session.client.scrip_master.get_token.return_value = "11536"
    resolved = scrip_master.resolve_instrument(session, "TCS", 1, None)
    assert resolved["token"] == "11536"


def test_lot_size_demo_is_one_for_cash():
    assert scrip_master.get_lot_size(_demo(), "2885") == 1


def test_lot_size_live_reads_market_lot():
    session = _connected("lot-live", SessionMode.LIVE)
    session.client.scrip_master.get_details.return_value = {"MarketLot": 25}
    assert scrip_master.get_lot_size(session, "26000") == 25


# -- funds ------------------------------------------------------------------


def test_demo_funds_never_call_choice():
    session = _demo("funds-demo")
    session.client = MagicMock()
    result = funds_gateway.get_funds(session)
    assert result["mode"] == "DEMO"
    assert result["data"]["AvailableMargin"] > 0
    session.client.funds.get_funds_view_new.assert_not_called()


def test_paper_funds_prefer_funds_view_new():
    session = _connected("funds-paper")
    session.client.funds.get_funds_view_new.return_value = {
        "Status": "Success",
        "Response": {"FundsViewNew": {
            "FundsAvailable": 10000.0, "MarginUtilized": 250.0,
            "LedgerBalance": 10250.0,
        }},
    }
    result = funds_gateway.get_funds(session)
    assert result["status"] == "SUCCESS"
    assert result["source"] == "get_funds_view_new"
    assert result["data"]["AvailableMargin"] == 10000.0
    assert result["data"]["used_margin"] == 250.0
    session.client.funds.get_funds_view.assert_not_called()


def test_funds_fall_back_to_legacy_view():
    session = _connected("funds-legacy")
    session.client.funds.get_funds_view_new.return_value = {
        "Status": "Fail", "Reason": "not available", "Response": "",
    }
    session.client.funds.get_funds_view.return_value = {
        "Status": "Success",
        "Response": {"FundsView": {
            "MarginAvailable": 500.0, "MarginUsed": 0.0,
        }},
    }
    result = funds_gateway.get_funds(session)
    assert result["source"] == "get_funds_view"
    assert result["data"]["AvailableMargin"] == 500.0


def test_funds_failure_is_raised():
    session = _connected("funds-fail")
    session.client.funds.get_funds_view_new.return_value = {
        "Status": "Fail", "Reason": "session expired",
    }
    session.client.funds.get_funds_view.return_value = {
        "Status": "Fail", "Reason": "session expired",
    }
    with pytest.raises(ChoiceUpstreamError):
        funds_gateway.get_funds(session)


# -- holdings / positions ---------------------------------------------------


def test_holdings_unwrap_isin_map():
    session = _connected("holdings")
    session.client.portfolio.get_holdings.return_value = {
        "Status": "Success",
        "Response": {"lDictStockViewHoldingData": {
            "INE002A01018": {
                "SegmentId": 1, "Token": 2885, "Symbol": "RELIANCE",
                "SecName": "RELIANCE INDUSTRIES LTD", "LTP": 250450,
                "ClosePrice": 247000, "AvgBuyPrice": 2450.00,
                "PriceDivisor": 100, "Qty": 50, "SellQty": 50,
            }
        }},
    }
    result = portfolio_gateway.get_holdings(session)
    assert result["mode"] == "PAPER"
    assert len(result["data"]) == 1
    row = result["data"][0]
    assert row["symbol"] == "RELIANCE"
    assert row["current_price"] == 2504.50
    assert row["token"] == "2885"


def test_holdings_failure_is_raised_not_empty_list():
    session = _connected("holdings-fail")
    session.client.portfolio.get_holdings.return_value = {
        "Status": "Fail", "Reason": "not entitled",
    }
    with pytest.raises(ChoiceUpstreamError, match="holdings"):
        portfolio_gateway.get_holdings(session)


def test_positions_empty_success_is_empty_list():
    session = _connected("pos-empty")
    session.client.portfolio.get_net_position.return_value = {
        "Status": "Success", "Response": {"NetPositions": []},
    }
    result = portfolio_gateway.get_positions(session)
    assert result["data"] == []


# -- order book -------------------------------------------------------------


def test_paper_order_book_is_local_not_broker():
    session = _connected("ob-paper", SessionMode.PAPER)
    session.simulated_orders = [{"order_id": "P1", "status": "FILLED"}]
    result = orders_gateway.get_order_book(session)
    assert result["mode"] == "PAPER"
    assert result["data"][0]["order_id"] == "P1"
    session.client.orders.get_order_book_v2.assert_not_called()


def test_live_order_book_prefers_v2():
    session = _connected("ob-live", SessionMode.LIVE)
    session.client.orders.get_order_book_v2.return_value = {
        "Status": "Success",
        "Response": {"Orders": [{
            "ClientOrderNo": "1001", "BuySell": "1", "Qty": 10,
            "Symbol": "RELIANCE", "Status": "OPEN", "Token": "2885",
            "SegmentId": 1, "OrderType": "RL_LIMIT", "ProductType": "CNC",
            "Price": 2500.0,
        }]},
    }
    result = orders_gateway.get_order_book(session)
    assert result["mode"] == "LIVE"
    assert result["data"][0]["side"] == "BUY"
    assert result["data"][0]["order_id"] == "1001"
    session.client.orders.get_order_book.assert_not_called()


def test_live_order_book_falls_back_to_v1():
    session = _connected("ob-v1", SessionMode.LIVE)
    session.client.orders.get_order_book_v2.return_value = {
        "Status": "Success", "Response": {"Orders": []},
    }
    session.client.orders.get_order_book.return_value = {
        "Status": "Success",
        "Response": {"Orders": [{
            "ClientOrderNo": "2002", "BuySell": "2", "Qty": 5,
            "Symbol": "INFY", "Status": "OPEN",
        }]},
    }
    result = orders_gateway.get_order_book(session)
    assert result["data"][0]["side"] == "SELL"


# -- market status / quotes -------------------------------------------------


def test_disconnected_touchline_is_rejected():
    session = ChoiceSession("offline")
    with pytest.raises(ChoiceNotConnected):
        market_gateway.get_multiple_touchline(session, "1_2885")


def test_live_touchline_failure_is_loud():
    session = _connected("tl-live", SessionMode.LIVE)
    session.client.market.get_multiple_touchline.side_effect = Exception(
        "subscription not active"
    )
    with pytest.raises(ChoiceUpstreamError, match="quotes"):
        market_gateway.get_multiple_touchline(session, "1_2885")


def test_paper_touchline_does_not_use_demo_quotes():
    """A connected paper account must never fill from the sandbox price table."""
    session = _connected("tl-paper", SessionMode.PAPER)
    session.client.market.get_multiple_touchline.side_effect = Exception("no feed")
    session.client.portfolio.get_holdings.return_value = {
        "Status": "Fail", "Reason": "empty",
    }
    session.client.portfolio.get_net_position.return_value = {
        "Status": "Fail", "Reason": "empty",
    }
    with pytest.raises(ChoiceUpstreamError, match="real price"):
        market_gateway.get_multiple_touchline(session, "1_2885")


def test_paper_touchline_can_price_from_holdings():
    session = _connected("tl-holdings", SessionMode.PAPER)
    session.client.market.get_multiple_touchline.side_effect = Exception("no feed")
    session.client.portfolio.get_holdings.return_value = {
        "Status": "Success",
        "Response": {"lDictStockViewHoldingData": {
            "INE002A01018": {
                "SegmentId": 1, "Token": 2885, "Symbol": "RELIANCE",
                "SecName": "RELIANCE INDUSTRIES LTD", "LTP": 132410,
                "ClosePrice": 130000, "AvgBuyPrice": 1200.00,
                "PriceDivisor": 100, "Qty": 10, "SellQty": 10,
            }
        }},
    }
    session.client.portfolio.get_net_position.return_value = {
        "Status": "Success", "Response": {"NetPositions": []},
    }
    result = market_gateway.get_multiple_touchline(session, "1_2885")
    assert result["source"] == "holdings_snapshot"
    assert result["data"][0]["ltp"] == 1324.10
    # Sandbox RELIANCE is 2504.50 — that fiction must not appear here.
    assert result["data"][0]["ltp"] != 2504.50


def test_live_touchline_success_filters_unrequested_rows():
    session = _connected("tl-ok", SessionMode.LIVE)
    session.client.market.get_multiple_touchline.return_value = {
        "Status": "Success",
        "Response": {"Touchline": [
            {"SegID": 1, "Token": "2885", "Symbol": "RELIANCE",
             "LTP": 250450, "PriceDivisor": 100},
            {"SegID": 1, "Token": "1594", "Symbol": "INFY",
             "LTP": 154020, "PriceDivisor": 100},
        ]},
    }
    result = market_gateway.get_multiple_touchline(session, "1_2885")
    tokens = {q["token"] for q in result["data"]}
    assert tokens == {"2885"}


def test_market_status_demo_skips_choice():
    session = _demo("mkt-demo")
    session.client = MagicMock()
    result = market_gateway.get_market_status(session)
    assert result["mode"] == "DEMO"
    session.client.market.get_market_status.assert_not_called()


def test_user_profile_live_unwraps_choice_payload():
    session = _connected("profile", SessionMode.LIVE)
    session.client.market.get_user_profile.return_value = {
        "Status": "Success",
        "Response": {"ClientName": "Test User", "ClientId": "C1"},
    }
    result = market_gateway.get_user_profile(session)
    assert result["status"] == "SUCCESS"
    assert result["data"]["ClientName"] == "Test User"


def test_user_profile_failure_is_raised():
    session = _connected("profile-fail", SessionMode.LIVE)
    session.client.market.get_user_profile.return_value = {
        "Status": "Fail", "Reason": "not logged in",
    }
    with pytest.raises(ChoiceUpstreamError, match="profile"):
        market_gateway.get_user_profile(session)
