"""Normalisation against the payload shapes Choice actually returns.

Every fixture here was captured from a live production account via
``backend/scripts/diagnose_choice.py``. Values are illustrative; the shapes and
field names are real, and they are the reason the earlier guessed mappings
produced an empty dashboard for a funded account.
"""

import pytest

from engine.app.choice_gateway.funds import _normalize_funds
from engine.app.choice_gateway.market import _parse_segment_status
from engine.app.choice_gateway.normalize import (
    failure_reason,
    is_failure,
    unwrap_dict,
    unwrap_list,
)
from engine.app.choice_gateway.portfolio import _normalize

HOLDINGS_AVG_KEYS = ("AvgBuyPrice", "AvgPrice", "BuyPrice")


# -- funds -----------------------------------------------------------------

FUNDS_VIEW_NEW = {"Status": "Success", "Reason": "", "Response": {"FundsViewNew": {
    "LedgerBalance": 3120.55, "TodaysBalance": 2955.20, "TodaysPayIn": 0.0,
    "TodaysPayOut": 0.0, "DPCharges": 0.0, "FutureBillCrDr": 0.0,
    "FutureCrDr": 0.0, "BuyMargin": 0.0, "MarginAgainstAssets": 1200.0,
    "EarlyPayIn": 0.0, "PledgeValue": 800.0, "MarginUtilized": 165.35,
    "FundsAvailable": 2955.20, "OpenMargin": 0.0, "RealizedPnL": 12.5,
    "UnRealizedPnL": -40.0, "DpCFS": 0.0, "PoolCFS": 0.0, "SarCFS": 0.0,
    "OptionCFS": 0.0, "TodaysHoldingSellBenefit": 0.0, "DPBill": 0.0,
    "DPC": 0.0}}}

FUNDS_VIEW = {"Status": "Success", "Reason": "", "Response": {"FundsView": {
    "CashAvailable": 2955.20, "MarginAvailable": 2955.20, "MarginUsed": 0.0,
    "PayIn": 0.0, "RealisedProfit": 0.0, "UnRealisedProfit": 0.0,
    "Collateral": 0.0, "Deposit": 0.0, "LimitUtilization": 0.0,
    "Deposit_Adhoc": 0.0, "Withdrawn": 0.0, "ODLimit": 0.0}}}


def test_funds_view_new_is_mapped():
    funds = _normalize_funds(unwrap_dict(FUNDS_VIEW_NEW))

    assert funds["AvailableMargin"] == 2955.20      # FundsAvailable
    assert funds["used_margin"] == 165.35           # MarginUtilized
    assert funds["NetLimit"] == 3120.55             # available + used
    assert funds["total_collateral"] == 2000.00     # assets + pledged
    assert funds["ledger_balance"] == 3120.55
    assert funds["realized_pnl"] == 12.5
    assert funds["unrealized_pnl"] == -40.0


def test_funds_view_legacy_is_mapped():
    funds = _normalize_funds(unwrap_dict(FUNDS_VIEW))

    assert funds["AvailableMargin"] == 2955.20      # MarginAvailable
    assert funds["used_margin"] == 0.0              # a real zero, not missing


def test_a_genuine_zero_balance_is_reported_as_zero():
    payload = {"Status": "Success", "Response": {"FundsViewNew": {
        "FundsAvailable": 0.0, "MarginUtilized": 0.0}}}
    funds = _normalize_funds(unwrap_dict(payload))

    assert funds["AvailableMargin"] == 0.0
    assert funds["used_margin"] == 0.0


# -- holdings --------------------------------------------------------------

def _holding(**overrides):
    record = {
        "SegmentId": 1, "Token": 2885, "Symbol": "RELIANCE",
        "SecName": "RELIANCE INDUSTRIES LTD", "LTP": 250450,
        "ClosePrice": 247000, "AvgBuyPrice": 2450.00, "PriceDivisor": 100,
        "Qty": 50, "SellQty": 50, "MarketLot": 1, "TxnId": None,
        "AprQty": 0, "lExchangeScrip": None, "TotalSaarQty": 0,
    }
    record.update(overrides)
    return record


HOLDINGS = {"Status": "Success", "Reason": "", "Response": {
    "lDictStockViewHoldingData": {
        "INE002A01018": _holding(),
        "INE009A01021": _holding(Symbol="ABB", Token=13, LTP=512000,
                                 ClosePrice=510000, AvgBuyPrice=5000.0, Qty=2),
    }}}


def test_isin_keyed_holdings_are_discovered():
    """Choice keys holdings by ISIN rather than returning a list."""
    rows = unwrap_list(HOLDINGS)
    assert len(rows) == 2
    assert {r["Symbol"] for r in rows} == {"RELIANCE", "ABB"}


def test_price_divisor_is_applied():
    """LTP arrives as a scaled integer; not dividing gives a 100x price."""
    row = _normalize(unwrap_list(HOLDINGS)[0], HOLDINGS_AVG_KEYS)

    assert row["current_price"] == 2504.50          # 250450 / 100
    assert row["close_price"] == 2470.00            # 247000 / 100
    assert row["average_price"] == 2450.00          # already in rupees
    assert row["pnl"] == pytest.approx((2504.50 - 2450.00) * 50)


def test_isin_is_carried_through():
    row = _normalize(unwrap_list(HOLDINGS)[0], HOLDINGS_AVG_KEYS)
    assert row["isin"] == "INE002A01018"
    assert row["token"] == "2885"
    assert row["segment_id"] == 1


def test_missing_divisor_leaves_the_price_alone():
    row = _normalize(_holding(LTP=2504, PriceDivisor=None), HOLDINGS_AVG_KEYS)
    assert row["current_price"] == 2504.0


def test_a_suspect_scale_is_reported_not_silently_replaced():
    """Replacing a suspect price with the other one is what inflated the
    portfolio a hundredfold; both are now reported as received."""
    row = _normalize(_holding(LTP=250450, ClosePrice=2470, PriceDivisor=1),
                     HOLDINGS_AVG_KEYS)
    assert row["current_price"] == 250450.0
    assert row["close_price"] == 2470.0


# -- empty collections -----------------------------------------------------

@pytest.mark.parametrize("payload,label", [
    ({"Status": "Success", "Response": {"NetPositions": []}}, "positions"),
    ({"Status": "Success", "Response": {"Orders": []}}, "orders"),
])
def test_genuinely_empty_collections(payload, label):
    assert unwrap_list(payload) == [], label


# -- failures --------------------------------------------------------------

def test_touchline_failure_reason_is_read_from_response():
    """MultipleTouchline reports its error as a string in Response."""
    payload = {"Status": "Fail", "Reason": "",
               "Response": "Market data subscription not active"}

    assert is_failure(payload) is True
    assert "subscription" in failure_reason(payload)


# -- market status ---------------------------------------------------------

MARKET_STATUS = {"Status": "Success", "Response": {"lstMktStatus": {
    "1": {"1": {"MktType": "Normal", "Status": "Open"},
          "8": {"MktType": "Auction", "Status": "Closed"}},
    "2": {"1": {"MktType": "Normal", "Status": "Open"}},
}, "MktStatusResp": "OK"}}


def test_segment_status_is_flattened():
    """Status arrives keyed by segment and then by market type."""
    statuses = _parse_segment_status(MARKET_STATUS)

    assert set(statuses) == {"1", "2"}
    assert {e["status"] for e in statuses["1"]} == {"Open", "Closed"}
    assert statuses["2"][0]["market_type"] == "Normal"


# -- price scaling, from live production data ------------------------------
#
# Captured 12 August 2026. A portfolio worth ~25 lakh was displayed as ~25
# crore because the guard replaced a correctly-scaled LTP with an unscaled
# ClosePrice. Both fields share the record's PriceDivisor.

LIVE_PRICES = [
    # symbol,       raw LTP,  raw close, divisor, real last, real close
    ("RELIANCE-EQ",   131490,   132390,      100,    1314.90,   1323.90),
    ("INFY-EQ",       116650,   119070,      100,    1166.50,   1190.70),
    ("LIQUIDBEES-EQ", 100000,   100000,      100,    1000.00,   1000.00),
    ("NITINFIRE-Z",      182,      182,      100,       1.82,      1.82),
    ("IDEA-EQ",         1318,     1289,      100,      13.18,     12.89),
    ("IRFC-EQ",         8767,     8791,      100,      87.67,     87.91),
    ("SBICARD-EQ",     65000,    65005,      100,     650.00,    650.05),
]


@pytest.mark.parametrize("symbol,ltp,close,divisor,want_last,want_close", LIVE_PRICES)
def test_both_prices_are_scaled(symbol, ltp, close, divisor, want_last, want_close):
    """LTP and ClosePrice are both scaled integers sharing PriceDivisor."""
    row = _normalize({"Symbol": symbol, "LTP": ltp, "ClosePrice": close,
                      "PriceDivisor": divisor, "AvgBuyPrice": want_last,
                      "Qty": 1}, HOLDINGS_AVG_KEYS)
    assert row["current_price"] == pytest.approx(want_last)
    assert row["close_price"] == pytest.approx(want_close)


def test_scaled_prices_stay_within_a_days_move_of_each_other():
    """The two prices agree once scaled — the mismatch that tripped the old
    guard was the guard comparing a divided price with an undivided one."""
    for symbol, ltp, close, divisor, _, _ in LIVE_PRICES:
        row = _normalize({"Symbol": symbol, "LTP": ltp, "ClosePrice": close,
                          "PriceDivisor": divisor, "AvgBuyPrice": 1, "Qty": 1},
                         HOLDINGS_AVG_KEYS)
        ratio = row["current_price"] / row["close_price"]
        assert 0.8 < ratio < 1.25, f"{symbol}: {ratio}"


def test_a_scale_mismatch_never_substitutes_the_other_price():
    """Reporting a suspect price is right; silently swapping in an unscaled
    one is what inflated the portfolio a hundredfold."""
    row = _normalize({"Symbol": "ODD", "LTP": 100, "ClosePrice": 1000000,
                      "PriceDivisor": 1, "AvgBuyPrice": 100, "Qty": 1},
                     HOLDINGS_AVG_KEYS)
    assert row["current_price"] == 100.0        # the last price is preserved
    assert row["close_price"] == 1000000.0      # the close is reported as-is


def test_portfolio_value_is_not_inflated():
    """The end-to-end symptom: value must be lakhs, not crores."""
    holdings = [
        {"Symbol": "RELIANCE-EQ", "LTP": 131490, "ClosePrice": 132390,
         "PriceDivisor": 100, "AvgBuyPrice": 1200.0, "Qty": 100},
        {"Symbol": "INFY-EQ", "LTP": 116650, "ClosePrice": 119070,
         "PriceDivisor": 100, "AvgBuyPrice": 1100.0, "Qty": 200},
    ]
    rows = [_normalize(h, HOLDINGS_AVG_KEYS) for h in holdings]
    value = sum(r["current_price"] * r["quantity"] for r in rows)
    cost = sum(r["average_price"] * r["quantity"] for r in rows)

    assert 3_00_000 < value < 4_00_000, f"expected lakhs, got {value}"
    assert 0.5 < value / cost < 2.0, "return must be plausible, not +11785%"


# -- quotes ----------------------------------------------------------------
#
# A quote's LTP becomes the fill price for a simulated market order, so an
# unscaled quote books paper trades a hundredfold away from the market.

def test_quote_price_is_scaled():
    from engine.app.choice_gateway.market import _normalize_quote

    quote = _normalize_quote({"Symbol": "RELIANCE-EQ", "Token": 2885, "SegID": 1,
                              "LTP": 131490, "PriceDivisor": 100})
    assert quote["ltp"] == pytest.approx(1314.90)


def test_a_quote_without_a_divisor_is_left_alone():
    """Payloads that already report rupees must not be divided."""
    from engine.app.choice_gateway.market import _normalize_quote

    quote = _normalize_quote({"Symbol": "RELIANCE-EQ", "LTP": 1314.90})
    assert quote["ltp"] == pytest.approx(1314.90)


def test_every_choice_price_read_goes_through_the_scaling_helper():
    """Guard the rule itself: no module may read a price field directly."""
    import pathlib
    import re

    price_fields = re.compile(r'pick_float\(\s*\w+\s*,\s*"(LTP|Ltp|ClosePrice|LastPrice)"')
    gateway = pathlib.Path("engine/app/choice_gateway")
    offenders = [
        f.name for f in gateway.glob("*.py")
        if price_fields.search(f.read_text(encoding="utf-8"))
    ]
    assert not offenders, f"read a price without scaling: {offenders}"


# -- quotes are filtered to what was asked for -------------------------------
#
# Choice has been observed returning rows for instruments that were not
# requested. `_market_fill_price` takes the first quote carrying a price, so an
# unrequested row would fill an order at the wrong instrument's price.
#
# A workaround that padded single-instrument requests to two pairs was tried
# and removed: the two-pair form fails identically, so the request size was
# never the problem. The filtering below is kept because it is correct
# regardless of why a stray row appears.

from engine.app.choice_gateway.market import _normalize_quote, _requested_keys


def test_an_unrequested_quote_never_reaches_the_caller():
    rows = [
        {"Symbol": "RELIANCE", "SegID": 1, "Token": "2885", "Ltp": 131490,
         "PriceDivisor": 100},
        {"Symbol": "INFY", "SegID": 1, "Token": "1594", "Ltp": 116650,
         "PriceDivisor": 100},
    ]
    wanted = _requested_keys("1_2885")
    quotes = [q for q in map(_normalize_quote, rows)
              if (q["segment_id"], str(q["token"])) in wanted]

    assert len(quotes) == 1
    assert quotes[0]["symbol"] == "RELIANCE"
    assert quotes[0]["ltp"] == pytest.approx(1314.90)


def test_requested_keys_ignores_malformed_pairs():
    assert _requested_keys("1_2885,junk,,2_") == {(1, "2885")}


# -- symbol resolution ------------------------------------------------------
#
# `ScripMaster.get_token` returns *every* matching row when no segment is
# given — for RELIANCE that is the share plus several hundred options and
# futures. Stringifying that list produced a "token" thousands of characters
# long, which every symbol-resolved backtest then sent to Choice. The failure
# read as "could not fetch historical data", which looked like a missing
# entitlement and was nothing of the sort.

from engine.app.choice_gateway.scrip_master import _pick_tradable

RELIANCE_ROWS = [
    {"Token": "144404", "Segment": "2", "Symbol": "RELIANCE",
     "SecDesc": "RELIANCE26SEP1380CE", "Series": "XX"},
    {"Token": "68777", "Segment": "2", "Symbol": "RELIANCE",
     "SecDesc": "RELIANCE26SEPFUT", "Series": "XX"},
    {"Token": "2885", "Segment": "1", "Symbol": "RELIANCE",
     "SecDesc": "RELIANCE INDUSTRIES LTD", "Series": "EQ"},
]


def test_a_symbol_resolves_to_the_share_not_an_option():
    """"RELIANCE" means the share. Picking `RELIANCE26SEP1380CE` out of the
    same match list would run a strategy on a call option."""
    assert _pick_tradable("RELIANCE", RELIANCE_ROWS) == "2885"


def test_order_within_the_list_does_not_matter():
    assert _pick_tradable("RELIANCE", list(reversed(RELIANCE_ROWS))) == "2885"


def test_a_derivative_is_returned_only_when_nothing_else_matches():
    """Refusing outright would block futures deliberately looked up by name."""
    assert _pick_tradable("RELIANCE", RELIANCE_ROWS[:2]) == "68777"


def test_no_match_returns_nothing_rather_than_a_guess():
    assert _pick_tradable("RELIANCE", []) is None
    assert _pick_tradable("RELIANCE", [{"Segment": "1", "Series": "EQ"}]) is None


def test_the_cash_segment_wins_over_a_closer_name():
    """Segment matters more than description length: a derivative with a short
    name must not beat the equity."""
    rows = [
        {"Token": "999", "Segment": "2", "Symbol": "X", "SecDesc": "X", "Series": "XX"},
        {"Token": "111", "Segment": "1", "Symbol": "X",
         "SecDesc": "X LIMITED", "Series": "EQ"},
    ]
    assert _pick_tradable("X", rows) == "111"


# -- a session reports its own environment ---------------------------------

def test_describe_reports_the_sessions_server_not_the_install_default():
    """Reporting the deployment default would tell a user they are on one
    server while their orders go to the other — the single most expensive
    thing this chip could get wrong."""
    from engine.app.choice_gateway.client_manager import ChoiceSession
    from engine.app.config import CHOICE_BASE_URLS, engine_settings

    session = ChoiceSession(owner_key="env-report")
    other = "PROD" if (engine_settings.CHOICE_ENV or "UAT").upper() == "UAT" else "UAT"
    session.environment = other
    session.base_url = CHOICE_BASE_URLS[other]

    described = session.describe()
    assert described["environment"] == other
    assert described["environment"] != (engine_settings.CHOICE_ENV or "UAT").upper()
    assert described["base_url"] == CHOICE_BASE_URLS[other]
