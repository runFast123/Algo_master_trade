"""Portfolio analytics: per-holding day change and the portfolio summary."""

from engine.app.choice_gateway.analytics import day_change, enrich, summarise

# -- a holding that was never priced today ---------------------------------

def test_a_holding_with_no_last_price_is_not_reported_as_flat():
    """`portfolio._normalize` substitutes the close when Choice sends no LTP, so
    the holding still counts toward portfolio value. Comparing that substitute
    against itself gives exactly 0.00, which reads as "traded flat today" rather
    than "never priced" — and a false flat drags the whole day's figure toward
    zero while looking entirely plausible."""
    row = {"symbol": "ILLIQUID", "quantity": 100, "average_price": 100.0,
           "current_price": 100.0, "close_price": 100.0, "pnl": 0.0,
           "priced_from_close": True}

    assert day_change(row) == {"day_change": None, "day_change_pct": None, "day_pnl": None}


def test_a_holding_that_genuinely_did_not_move_is_still_reported():
    """Zero is a real answer when the price was actually observed."""
    row = {"symbol": "STEADY", "quantity": 100, "average_price": 100.0,
           "current_price": 100.0, "close_price": 100.0, "pnl": 0.0,
           "priced_from_close": False}

    assert day_change(row)["day_pnl"] == 0.0


def test_an_unpriced_holding_is_excluded_from_the_day_total():
    rows = enrich([
        {"symbol": "TRADED", "quantity": 100, "average_price": 100.0,
         "current_price": 110.0, "close_price": 105.0, "pnl": 1000.0,
         "priced_from_close": False},
        {"symbol": "ILLIQUID", "quantity": 100, "average_price": 100.0,
         "current_price": 100.0, "close_price": 100.0, "pnl": 0.0,
         "priced_from_close": True},
    ])
    summary = summarise(rows)

    assert summary["day_pnl"] == 500.0
    assert summary["day_priced"] == 1, "the unpriced holding must not be counted as priced"
    assert summary["holdings"] == 2
