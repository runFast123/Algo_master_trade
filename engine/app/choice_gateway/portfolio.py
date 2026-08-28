"""Holdings and positions, scoped to a single user's Choice session."""

import logging
from typing import Any, Dict, List

from engine.app.choice_gateway.client_manager import ChoiceSession
from engine.app.choice_gateway.errors import ChoiceUpstreamError
from engine.app.choice_gateway.normalize import (
    failure_reason,
    is_failure,
    pick_float,
    pick_int,
    pick_str,
    scaled_price,
    unwrap_dict,
    unwrap_list,
)

logger = logging.getLogger("choice_gateway")

# Field names confirmed against a live Holdings response, whose records carry
# Symbol, SecName, Token, SegmentId, LTP, ClosePrice, AvgBuyPrice,
# PriceDivisor, Qty, SellQty and MarketLot.
SYMBOL_KEYS = (
    "TradingSymbol", "Symbol", "ScripName", "SecName", "SecDesc",
    "InstrumentName", "ScripSymbol", "symbol",
)
QUANTITY_KEYS = (
    "Qty", "NetQty", "Quantity", "HoldingsQty", "NetQuantity", "TotalQty",
    "quantity",
)
LAST_PRICE_KEYS = ("LTP", "Ltp", "LastPrice", "LastTradedPrice", "current_price")
CLOSE_PRICE_KEYS = ("ClosePrice", "PrevClose", "Close")
PNL_KEYS = (
    "PnL", "Pnl", "UnrealizedPnL", "UnrealisedPnL", "RealizedPnL", "MTM",
    "NetPnL", "ProfitLoss", "pnl",
)

def _normalize(item: Dict[str, Any], avg_price_keys: tuple) -> Dict[str, Any]:
    symbol = pick_str(item, *SYMBOL_KEYS)
    quantity = pick_float(item, *QUANTITY_KEYS, default=0.0)

    # AvgBuyPrice arrives as a decimal already in rupees. LTP and ClosePrice
    # are scaled integers sharing the record's PriceDivisor, so both are
    # divided; confirmed against live data where LIQUIDBEES and NITINFIRE
    # match their close exactly once scaled.
    avg_price = pick_float(item, *avg_price_keys)
    current_price = scaled_price(item, *LAST_PRICE_KEYS)
    close_price = scaled_price(item, *CLOSE_PRICE_KEYS)
    pnl = pick_float(item, *PNL_KEYS)

    # Report a scale that still looks wrong, but never substitute one price for
    # another: an earlier version swapped in the close on a mismatch, which
    # replaced a correctly-scaled price with an unscaled one and inflated the
    # whole portfolio a hundredfold. Surfacing a suspect number beats silently
    # publishing a wrong one.
    if current_price is not None and close_price:
        ratio = current_price / close_price
        if ratio > 10 or ratio < 0.1:
            logger.warning(
                "%s: last price %.4f and close %.4f differ by %.0fx after "
                "scaling — check PriceDivisor for this instrument.",
                symbol or "instrument", current_price, close_price,
                ratio if ratio > 1 else 1 / ratio,
            )
    # No last price: value the holding at its close rather than dropping it out
    # of the portfolio, but record that this is what happened. Without the flag
    # the substitution makes last equal close, and the day change comes out as
    # exactly 0.00 — a holding asserted to be flat when the truth is that it was
    # never priced. `day_change` refuses to guess when the *close* is missing;
    # this is the same ignorance from the other side and deserves the same
    # answer.
    priced_from_close = current_price is None and close_price is not None
    if current_price is None:
        current_price = close_price

    # Only derive P&L when Choice did not report one and we have both prices.
    if pnl is None and avg_price is not None and current_price is not None:
        pnl = (current_price - avg_price) * quantity

    return {
        "symbol": symbol,
        "isin": pick_str(item, "_record_key", "ISIN", "Isin") or None,
        "segment_id": pick_int(item, "SegmentId", "SegID", "segment_id"),
        "token": pick_str(item, "Token", "token") or None,
        "quantity": quantity,
        "sellable_quantity": pick_float(item, "SellQty", "SellableQty"),
        "average_price": round(avg_price, 2) if avg_price is not None else None,
        "current_price": round(current_price, 2) if current_price is not None else None,
        "close_price": round(close_price, 2) if close_price is not None else None,
        "priced_from_close": priced_from_close,
        "pnl": round(pnl, 2) if pnl is not None else None,
        # Needed to convert a position between products. A holding has no
        # product, so this stays empty there and the interface offers nothing
        # rather than an action Choice would refuse.
        "product_type": pick_str(item, "ProductType", "Product", "product_type") or None,
        "client_order_no": pick_str(item, "ClientOrderNo", "OrderNo",
                                    "client_order_no") or None,
    }


def _demo_rows(session: ChoiceSession) -> List[Dict[str, Any]]:
    rows = []
    for holding in session.demo_holdings:
        row = dict(holding)
        row["pnl"] = round(
            (row["current_price"] - row["average_price"]) * row["quantity"], 2
        )
        rows.append(row)
    return rows


def _fetch(session: ChoiceSession, call: str, label: str) -> List[Dict[str, Any]]:
    """Call Choice and unwrap the records, raising on an explicit failure.

    An empty list is returned only when Choice genuinely reported no records —
    never as a way of swallowing an error.
    """
    client = session.require_client()
    raw = getattr(client.portfolio, call)()

    if is_failure(raw):
        raise ChoiceUpstreamError(
            f"Choice could not return {label}", failure_reason(raw)
        )

    rows = unwrap_list(raw)
    if not rows and isinstance(raw, dict):
        # Log only when something looks *unread*, not when the book is simply
        # empty. Holding no intraday positions is the normal state for most
        # accounts on most days, and logging it every refresh trains people to
        # ignore the log — which is where a real mapping failure would appear.
        payload = raw.get("Response")
        looks_empty = payload in (None, "", [], {}) or (
            isinstance(payload, dict) and not any(payload.values())
        )
        if looks_empty:
            logger.debug("Choice reported no %s for this account.", label)
        else:
            logger.warning(
                "Choice returned %s data we could not read; envelope keys were "
                "%s. Run the diagnostics endpoint to see the shape.",
                label, sorted(raw.keys())[:20],
            )
    return rows


def get_holdings(session: ChoiceSession) -> Dict[str, Any]:
    if session.is_demo:
        return {"status": "SUCCESS", "mode": "DEMO", "data": _demo_rows(session)}

    rows = _fetch(session, "get_holdings", "holdings")
    return {
        "status": "SUCCESS",
        "mode": session.mode.value,
        "data": [_normalize(r, ("AvgBuyPrice", "AvgPrice", "BuyPrice", "BuyAvgPrice",
                                "AveragePrice", "average_price")) for r in rows],
    }


def get_positions(session: ChoiceSession) -> Dict[str, Any]:
    if session.is_demo:
        return {"status": "SUCCESS", "mode": "DEMO", "data": []}

    rows = _fetch(session, "get_net_position", "positions")
    return {
        "status": "SUCCESS",
        "mode": session.mode.value,
        "data": [_normalize(r, ("BuyAvgPrice", "AvgPrice", "NetAvgPrice",
                                "AveragePrice", "average_price")) for r in rows],
    }


def convert_position(
    session: ChoiceSession,
    segment_id: int,
    token: int,
    client_order_no: int,
    side: str,
    quantity: int,
    product_type: str,
    source_product_type: str,
    is_edis_req: bool = True,
) -> Dict[str, Any]:
    """Move an open position between product types, e.g. MIS intraday to CNC.

    This changes what happens at the close: an unconverted MIS position is
    squared off by the broker, a CNC one is carried. It moves no money and
    places no order, but getting the direction wrong is not recoverable after
    the cutoff, so both the source and the target are stated explicitly rather
    than inferred from the position.
    """
    if session.simulates_orders:
        raise ChoiceUpstreamError(
            "Position conversion applies to positions held at Choice. A "
            "simulated session has none."
        )

    side_clean = str(side or "").strip().upper()
    if side_clean not in {"BUY", "SELL"}:
        raise ChoiceUpstreamError(f"Side must be BUY or SELL, got {side!r}")

    source = str(source_product_type or "").strip().upper()
    target = str(product_type or "").strip().upper()
    if source == target:
        raise ChoiceUpstreamError(
            f"The position is already {target}; nothing to convert."
        )

    client = session.require_client()
    try:
        raw = client.portfolio.position_conversion(
            segment_id=int(segment_id),
            token=int(token),
            client_order_no=int(client_order_no),
            buy_sell=1 if side_clean == "BUY" else 2,
            quantity=int(quantity),
            product_type=target,
            source_product_type=source,
            is_edis_req=bool(is_edis_req),
        )
    except Exception as exc:
        raise ChoiceUpstreamError("Position conversion failed", str(exc)) from exc

    if is_failure(raw):
        raise ChoiceUpstreamError(
            "Choice rejected the position conversion", failure_reason(raw)
        )

    logger.info(
        "Converted %s %s of token %s from %s to %s for user %s",
        side_clean, quantity, token, source, target, session.owner_key,
    )
    return {
        "status": "SUCCESS",
        "mode": session.mode.value,
        "message": f"Converted {quantity} from {source} to {target}",
        "data": raw if isinstance(raw, dict) else {},
    }


def get_dis_status(session: ChoiceSession) -> Dict[str, Any]:
    """eDIS authorisation status for this account.

    Selling delivery holdings needs an eDIS authorisation. Without it the sell
    is accepted and then fails at the depository, which looks like a platform
    fault rather than a missing authorisation — so surface it before the order.
    """
    if session.simulates_orders:
        return {"status": "SUCCESS", "mode": session.mode.value,
                "data": {}, "note": "Simulated sessions do not need eDIS."}

    client = session.require_client()
    try:
        raw = client.portfolio.get_dis_status()
    except Exception as exc:
        raise ChoiceUpstreamError("Could not fetch eDIS status", str(exc)) from exc

    if is_failure(raw):
        raise ChoiceUpstreamError("Choice rejected the eDIS status request",
                                  failure_reason(raw))
    return {"status": "SUCCESS", "mode": session.mode.value,
            "data": unwrap_dict(raw)}
