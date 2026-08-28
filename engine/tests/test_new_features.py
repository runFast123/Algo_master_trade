"""Costs, portfolio analytics, the risk budget and the trading calendar."""

import datetime

import pytest

from engine.app import market_calendar as cal
from engine.app.choice_gateway import analytics
from engine.app.choice_gateway.errors import OrderRejected
from engine.app.costs import CostModel, preview_order
from engine.app.strategy_engine.risk_manager import RiskManager


# -- transaction costs -----------------------------------------------------

def test_buy_preview_costs_more_than_the_turnover():
    """A buyer pays the turnover plus charges, never less."""
    p = preview_order("BUY", 10, 1314.90)

    assert p["turnover"] == pytest.approx(13149.0)
    assert p["charges"] > 0
    assert p["net_amount"] > p["turnover"]
    assert p["effective_price"] > p["price"]


def test_sell_preview_credits_less_than_the_turnover():
    p = preview_order("SELL", 10, 1314.90)

    assert p["net_amount"] < p["turnover"]
    assert p["effective_price"] < p["price"]


def test_breakeven_covers_both_legs():
    """The price to exit flat has to pay the exit charges too — estimating on
    the entry leg alone is the mistake this figure exists to prevent."""
    p = preview_order("BUY", 100, 1000.0)

    assert p["breakeven_price"] > p["effective_price"]


def test_stamp_duty_is_charged_on_the_buy_leg_only():
    buy = preview_order("BUY", 10, 1000.0)["charge_breakdown"]
    sell = preview_order("SELL", 10, 1000.0)["charge_breakdown"]

    assert buy["stamp_duty"] > 0
    assert sell["stamp_duty"] == 0


def test_brokerage_is_capped():
    """0.03% uncapped on a large order would be far more than the ₹20 cap."""
    p = preview_order("BUY", 10000, 1000.0)
    assert p["charge_breakdown"]["brokerage"] == pytest.approx(20.0)


def test_total_matches_the_itemised_breakdown():
    """A trader must not be able to add up the rows and get a different total."""
    p = preview_order("BUY", 37, 1234.56)
    assert sum(p["charge_breakdown"].values()) == pytest.approx(p["charges"], abs=0.05)


def test_charges_are_unrounded_for_backtests():
    """Rounding per component would drift an equity curve over many trades."""
    model = CostModel()
    exact = model.charges(13149.0, "BUY")
    rounded = sum(model.breakdown(13149.0, "BUY").values())
    assert exact != rounded or exact == pytest.approx(rounded, abs=0.01)
    assert isinstance(exact, float)


def test_a_zero_quantity_preview_does_not_divide_by_zero():
    """The form is priced while it is still being filled in."""
    p = preview_order("BUY", 0, 0.0)
    assert p["charges"] == 0
    assert p["effective_price"] == 0


# -- portfolio analytics ---------------------------------------------------

def _holding(**kw):
    row = {"symbol": "X", "quantity": 10.0, "average_price": 100.0,
           "current_price": 110.0, "close_price": 105.0, "pnl": 100.0}
    row.update(kw)
    return row


def test_day_change_uses_the_previous_close():
    row = analytics.enrich([_holding()])[0]

    assert row["day_change"] == pytest.approx(5.0)       # 110 - 105
    assert row["day_pnl"] == pytest.approx(50.0)         # × 10
    assert row["day_change_pct"] == pytest.approx(4.76, abs=0.01)


def test_day_change_is_unknown_without_a_close():
    """Unknown must not become zero: a holding silently counted as flat drags
    the whole day's figure toward zero and looks plausible."""
    row = analytics.enrich([_holding(close_price=None)])[0]

    assert row["day_pnl"] is None
    assert row["day_change"] is None


def test_summary_reports_partial_day_coverage():
    rows = analytics.enrich([_holding(), _holding(symbol="Y", close_price=None)])
    summary = analytics.summarise(rows)

    assert summary["holdings"] == 2
    assert summary["day_priced"] == 1        # so a partial figure is legible
    assert summary["day_pnl"] == pytest.approx(50.0)


def test_summary_separates_day_from_total_return():
    """A book up since purchase can still be down today."""
    rows = analytics.enrich([
        _holding(average_price=50.0, current_price=110.0, close_price=120.0, pnl=600.0),
    ])
    summary = analytics.summarise(rows)

    assert summary["pnl"] > 0                # up since purchase
    assert summary["day_pnl"] < 0            # down today


def test_concentration_is_reported():
    rows = analytics.enrich([
        _holding(symbol="BIG", quantity=100.0, current_price=1000.0),
        _holding(symbol="SMALL", quantity=1.0, current_price=100.0),
    ])
    summary = analytics.summarise(rows)

    assert summary["largest_position"] == "BIG"
    assert summary["largest_position_pct"] > 99
    assert summary["concentration_hhi"] > 0.9      # ~1.0 means a single name


def test_an_even_book_has_low_concentration():
    rows = analytics.enrich([
        _holding(symbol=s, quantity=10.0, current_price=100.0)
        for s in ("A", "B", "C", "D")
    ])
    summary = analytics.summarise(rows)

    assert summary["largest_position_pct"] == pytest.approx(25.0)
    assert summary["concentration_hhi"] == pytest.approx(0.25)


def test_empty_portfolio_summarises_without_error():
    summary = analytics.summarise([])
    assert summary["holdings"] == 0
    assert summary["largest_position"] is None


# -- risk budget and the circuit breaker -----------------------------------

def test_budget_reports_headroom():
    rm = RiskManager(max_daily_loss=10000)
    rm.record_realized_pnl("u", -2500)

    budget = rm.budget("u")
    assert budget["daily_loss_used"] == 2500
    assert budget["daily_loss_remaining"] == 7500
    assert budget["halted"] is False


def test_breaching_the_daily_loss_trips_the_breaker():
    """Refusing only the current order lets a retrying strategy keep probing."""
    rm = RiskManager(max_daily_loss=1000)
    rm.record_realized_pnl("u", -1500)

    with pytest.raises(OrderRejected):
        rm.validate_order(1, 10.0, "BUY", "u")

    assert rm.is_halted("u") is not None

    # A second, smaller order is refused too — the account is stopped, not
    # just this one request.
    with pytest.raises(OrderRejected, match="halted"):
        rm.validate_order(1, 1.0, "BUY", "u")


def test_a_halt_is_scoped_to_one_account():
    rm = RiskManager()
    rm.halt("a", "Kill switch used")

    assert rm.is_halted("a")
    assert rm.is_halted("b") is None
    rm.validate_order(1, 10.0, "BUY", "b")      # must not raise


def test_orders_without_an_owner_still_pass_the_size_checks():
    """owner_key gates the daily cap; the notional checks are unconditional."""
    rm = RiskManager(max_order_value=1000)
    with pytest.raises(OrderRejected):
        rm.validate_order(100, 100.0, "BUY")


def test_the_broker_figure_replaces_rather_than_accumulates():
    """Adding on every funds refresh would book the same loss repeatedly."""
    rm = RiskManager()
    rm.set_realized_pnl("u", -500)
    rm.set_realized_pnl("u", -500)
    assert rm.realized_pnl("u") == -500


# -- trading calendar ------------------------------------------------------

def test_a_listed_holiday_is_not_a_trading_day():
    assert cal.is_trading_day(datetime.date(2026, 1, 26)) is False
    assert cal.holiday_name(datetime.date(2026, 1, 26)) == "Republic Day"


def test_weekends_are_not_trading_days():
    assert cal.is_trading_day(datetime.date(2026, 8, 15)) is False   # Saturday


def test_an_ordinary_weekday_trades():
    assert cal.is_trading_day(datetime.date(2026, 8, 12)) is True


def test_an_unknown_year_is_reported_as_unknown():
    """Treating a blank year as fully trading would look authoritative and be
    wrong; the weekday fallback is stated rather than hidden."""
    described = cal.describe(datetime.date(2035, 1, 1))

    assert described["known"] is False
    assert "No holiday calendar" in described["note"]


def test_next_trading_day_skips_the_holiday():
    assert cal.next_trading_day(datetime.date(2026, 1, 26)) == datetime.date(2026, 1, 27)


def test_market_hours_respect_the_calendar():
    """The gate that matters: no session on a holiday, whatever the clock says."""
    from engine.app.choice_gateway.market import is_indian_market_hours

    # 11:00 IST on Republic Day 2026 = 05:30 UTC.
    republic_day = datetime.datetime(2026, 1, 26, 5, 30, tzinfo=datetime.timezone.utc)
    assert is_indian_market_hours(republic_day) is False

    # Same time on an ordinary Wednesday.
    ordinary = datetime.datetime(2026, 1, 28, 5, 30, tzinfo=datetime.timezone.utc)
    assert is_indian_market_hours(ordinary) is True


# -- the kill switch on a live account -------------------------------------
#
# The DEMO path short-circuits before touching the broker, so a fault in the
# live branch survives every sandbox test. These drive it with a fake client.

class _FakeOrdersAPI:
    def __init__(self, book, fail_on=()):
        self._book = book
        self._fail_on = set(fail_on)
        self.cancelled = []

    def get_order_book_v2(self):
        return {"Status": "Success", "Response": {"Orders": self._book}}

    get_order_book = get_order_book_v2

    def cancel_order(self, **kwargs):
        no = kwargs["client_order_no"]
        self.cancelled.append(kwargs)
        if no in self._fail_on:
            return {"Status": "Fail", "Reason": "Order already executed"}
        return {"Status": "Success", "Reason": ""}


class _FakeSession:
    simulates_orders = False
    is_paper = False
    owner_key = "live-user"

    def __init__(self, book, fail_on=()):
        self.orders_api = _FakeOrdersAPI(book, fail_on)
        self.client = type("C", (), {"orders": self.orders_api})()

    def require_client(self):
        return self.client


def _order(no, status="Open", symbol="RELIANCE"):
    return {"ClientOrderNo": no, "ExchangeOrderNo": f"X{no}", "GatewayOrderNo": f"G{no}",
            "SegmentId": 1, "Token": 2885, "OrderType": "RL_LIMIT", "BS": 1,
            "Qty": 10, "Price": 1300.0, "TriggerPrice": 0.0, "Validity": 1,
            "ProductType": "CNC", "Status": status, "TradingSymbol": symbol,
            "DisclosedQty": 0, "ExchangeOrderTime": "2026-08-12 10:00:00"}


def test_kill_switch_cancels_only_working_orders():
    from engine.app.choice_gateway.orders import cancel_all_open

    session = _FakeSession([
        _order(1, "Open"), _order(2, "Executed"), _order(3, "Pending"),
        _order(4, "Cancelled"),
    ])
    result = cancel_all_open(session)

    assert result["cancelled"] == 2          # the Open and Pending ones
    assert result["failed"] == 0
    assert {c["client_order_no"] for c in session.orders_api.cancelled} == {1, 3}


def test_kill_switch_echoes_the_original_order_back():
    """Choice requires the full original order on a cancel, so every field has
    to come from the order book rather than be reconstructed."""
    from engine.app.choice_gateway.orders import cancel_all_open

    session = _FakeSession([_order(7)])
    cancel_all_open(session)
    sent = session.orders_api.cancelled[0]

    assert sent["exchange_order_no"] == "X7"
    assert sent["gateway_order_no"] == "G7"
    assert sent["price"] == 1300.0
    assert sent["product_type"] == "CNC"
    assert sent["qty"] == 10


def test_one_refused_cancellation_does_not_stop_the_rest():
    """The point of a kill switch is that it works when things are going wrong."""
    from engine.app.choice_gateway.orders import cancel_all_open

    session = _FakeSession([_order(1), _order(2), _order(3)], fail_on={2})
    result = cancel_all_open(session)

    assert result["cancelled"] == 2
    assert result["failed"] == 1
    assert "already executed" in result["failures"][0]["reason"].lower()
    assert len(session.orders_api.cancelled) == 3      # all three attempted


def test_an_exception_on_one_order_is_reported_not_raised():
    from engine.app.choice_gateway.orders import cancel_all_open

    session = _FakeSession([_order(1), _order(2)])
    original = session.orders_api.cancel_order

    def explode(**kwargs):
        if kwargs["client_order_no"] == 1:
            raise RuntimeError("connection reset")
        return original(**kwargs)

    session.orders_api.cancel_order = explode
    result = cancel_all_open(session)

    assert result["cancelled"] == 1
    assert result["failed"] == 1
    assert "connection reset" in result["failures"][0]["reason"]


def test_reconcile_flags_an_order_the_broker_never_saw():
    from engine.app.choice_gateway.orders import reconcile

    session = _FakeSession([_order(1)])
    result = reconcile(session, [
        {"client_order_no": "1", "symbol": "RELIANCE", "status": "ACCEPTED"},
        {"client_order_no": "99", "symbol": "INFY", "status": "ACCEPTED"},
    ])

    assert result["in_sync"] is False
    assert [m["client_order_no"] for m in result["missing_at_broker"]] == ["99"]


def test_reconcile_reports_orders_placed_elsewhere():
    """An order from Choice's own app is not a fault, and is listed separately."""
    from engine.app.choice_gateway.orders import reconcile

    session = _FakeSession([_order(1), _order(2, symbol="TCS")])
    result = reconcile(session, [
        {"client_order_no": "1", "symbol": "RELIANCE", "status": "ACCEPTED"},
    ])

    assert [u["client_order_no"] for u in result["unknown_locally"]] == ["2"]
    assert result["missing_at_broker"] == []


def test_reconcile_ignores_simulated_and_rejected_orders():
    """Neither ever reached the broker, so neither is a divergence."""
    from engine.app.choice_gateway.orders import reconcile

    session = _FakeSession([])
    result = reconcile(session, [
        {"client_order_no": "DEMO-1", "symbol": "X", "status": "SIMULATED"},
        {"client_order_no": "R-1", "symbol": "Y", "status": "REJECTED"},
    ])

    assert result["checked"] == 0
    assert result["in_sync"] is True
