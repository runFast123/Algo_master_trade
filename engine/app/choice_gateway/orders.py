"""Order placement and books, scoped to a single user's Choice session.

Order flow is the one path that moves money, so nothing here is inferred from
connection state. Only a LIVE session submits to Choice; DEMO and PAPER are
both filled locally and have no code path to the broker. A failure is raised
rather than returned as a success-shaped payload.
"""

import itertools
import logging
import threading
import time
from typing import Any, Dict, List, Optional

from engine.app.choice_gateway.client_manager import ChoiceSession
from engine.app.choice_gateway.errors import (
    ChoiceGatewayError,
    ChoiceUpstreamError,
    OrderRejected,
)
from engine.app.choice_gateway.normalize import (
    failure_reason,
    is_failure,
    pick,
    pick_float,
    pick_int,
    pick_str,
    scaled_price,
    unwrap_dict,
    unwrap_list,
)
from engine.app.config import engine_settings
from engine.app.strategy_engine.risk_manager import risk_manager

logger = logging.getLogger("choice_gateway")

VALID_SIDES = {"BUY", "SELL"}
VALID_ORDER_TYPES = {"RL_MKT", "RL_LIMIT", "SL_MKT", "SL_LIMIT"}
VALID_PRODUCT_TYPES = {"CNC", "MIS", "NRML", "CO", "BO"}

# Choice buy/sell codes, per FINX Interactive Socket API Reference s6.1.
BUY_CODES = {"1", "B", "BUY"}
SELL_CODES = {"2", "S", "SELL"}

_demo_counter = itertools.count(1)

# Guards the client order number sequence; orders are placed from request
# threads and from strategy runner threads alike.
_order_no_lock = threading.Lock()
_order_no_last = 0


def _normalize_side(raw: Any) -> Optional[str]:
    """Map a Choice buy/sell code to BUY/SELL, or None when unrecognised.

    Guessing here would mislabel a trade's direction, so an unknown code is
    reported as unknown rather than defaulted.
    """
    token = str(raw).strip().upper()
    if token in BUY_CODES:
        return "BUY"
    if token in SELL_CODES:
        return "SELL"
    return None


def _normalize_order(item: Dict[str, Any]) -> Dict[str, Any]:
    side = _normalize_side(pick(item, "BuySell", "TransactionType", "side"))
    if side is None:
        logger.warning(
            "Unrecognised buy/sell code %r on order %s",
            pick(item, "BuySell", "TransactionType", "side"),
            pick(item, "ClientOrderNo", "OrderNo", "order_id"),
        )
    return {
        "order_id": pick_str(item, "ClientOrderNo", "OrderNo", "order_id"),
        "exchange_order_no": pick_str(item, "ExchangeOrderNo", "OrderNumber"),
        # Choice needs all three identifiers to find an order to amend, and the
        # order book is the only place they arrive together. Dropping the
        # gateway number here is what made amending impossible without it.
        "gateway_order_no": pick_str(item, "GatewayOrderNo", "GatewayOrderNumber"),
        "symbol": pick_str(item, "TradingSymbol", "Symbol", "symbol"),
        "side": side,
        "quantity": pick_int(item, "Qty", "OrderQty", "quantity", default=0),
        # Scaled if the record carries a divisor, untouched if it does not, so
        # an order book cannot show prices a hundredfold out.
        "price": scaled_price(item, "Price", "OrderPrice", "price"),
        "trigger_price": scaled_price(item, "TriggerPrice", "trigger_price"),
        "status": pick_str(item, "Status", "OrderStatus", "status", default="UNKNOWN"),
        "created_at": pick_str(item, "OrderTime", "Time", "created_at"),
        # Everything below is what an amendment has to send back unchanged.
        #
        # No defaults. These were `RL_LIMIT` and `CNC`, and the field names are
        # unverified — the live order book has only ever come back empty. If
        # Choice names the field differently, defaulting turned a working
        # SL_LIMIT stop into a plain limit on amendment: the amend dialog then
        # hid the trigger field and posted `trigger_price_in_paisa: 0`, so a
        # user changing the quantity silently removed their protective stop.
        # The `CNC` default did the same to an MIS order, converting intraday
        # into delivery. An empty value is honest, and the interface withholds
        # the Amend action rather than guessing.
        "segment_id": pick_int(item, "SegmentId", "segment_id", default=0),
        "token": pick_str(item, "Token", "token"),
        "order_type": pick_str(item, "OrderType", "order_type"),
        "product_type": pick_str(item, "ProductType", "product_type"),
    }


def validate_order(
    segment_id: int,
    token: int,
    order_type: str,
    side: str,
    quantity: int,
    price: float,
    product_type: str = "CNC",
    owner_key: str = None,
    simulated: bool = False,
    reference_price: float = 0.0,
) -> Dict[str, Any]:
    """Reject malformed or oversized orders before they leave the process.

    ``reference_price`` is what a market order is worth. A market order carries
    no price of its own, so ``quantity * price`` was always 0 and the per-order
    value cap — the single largest safety control here — never applied to the
    one order type with no price ceiling of its own.

    ``owner_key`` scopes the daily loss cap and the halt state to one account.
    It is required in practice: without it those two checks silently do
    nothing, which is exactly how they came to be unreachable before.

    ``simulated`` says which loss budget applies. A paper order is measured
    against paper losses; only real losses can stop real orders.
    """
    side = str(side).strip().upper()
    order_type = str(order_type).strip().upper()
    product_type = str(product_type).strip().upper()

    if side not in VALID_SIDES:
        raise OrderRejected(f"Order side must be BUY or SELL, got {side!r}")
    if order_type not in VALID_ORDER_TYPES:
        raise OrderRejected(
            f"Order type must be one of {sorted(VALID_ORDER_TYPES)}, got {order_type!r}"
        )
    if product_type not in VALID_PRODUCT_TYPES:
        raise OrderRejected(
            f"Product type must be one of {sorted(VALID_PRODUCT_TYPES)}, "
            f"got {product_type!r}"
        )
    if quantity <= 0:
        raise OrderRejected(f"Order quantity must be greater than zero, got {quantity}")
    if quantity > engine_settings.MAX_ORDER_QUANTITY:
        raise OrderRejected(
            f"Order quantity {quantity} exceeds the per-order limit of "
            f"{engine_settings.MAX_ORDER_QUANTITY}"
        )
    if price < 0:
        raise OrderRejected(f"Order price cannot be negative, got {price}")
    if order_type in {"RL_LIMIT", "SL_LIMIT"} and price <= 0:
        raise OrderRejected(f"{order_type} orders require a price above zero")
    if segment_id <= 0:
        raise OrderRejected(f"Invalid segment id {segment_id}")
    if token <= 0:
        raise OrderRejected(f"Invalid instrument token {token}")

    # Value a market order at the reference price. Refusing outright when none
    # is available would make the platform unusable whenever quotes are down;
    # the quantity cap still applies, and the caller logs the gap.
    sizing_price = price if price > 0 else float(reference_price or 0.0)
    if sizing_price <= 0:
        logger.warning(
            "No reference price for %s %s of token %s; the per-order value cap "
            "cannot be applied to this order.", side, quantity, token,
        )

    risk_manager.validate_order(
        quantity=quantity, price=sizing_price, side=side, owner_key=owner_key,
        simulated=simulated,
    )

    return {
        "side": side,
        "order_type": order_type,
        "product_type": product_type,
    }


def _reference_price(
    session: ChoiceSession, segment_id: int, token: int, price: float
) -> float:
    """A price to size a market order against, or 0 when none can be had.

    Best effort by design: this exists to make the notional cap apply to market
    orders, and a quote outage must not become an inability to trade. When it
    returns 0 the quantity cap is the only remaining bound, and that is logged.
    """
    if price > 0:
        return price
    try:
        from engine.app.choice_gateway.market import get_multiple_touchline

        for quote in get_multiple_touchline(
                session, f"{segment_id}_{token}").get("data", []):
            if quote.get("ltp"):
                return float(quote["ltp"])
    except Exception as exc:
        logger.info("No reference price for token %s: %s", token, exc)
    return 0.0


def _market_fill_price(
    session: ChoiceSession, segment_id: int, token: int, submitted_price: float
) -> tuple:
    """Fill price for a simulated market order, and where it came from.

    A paper session is genuinely signed in, so its fills use the traded price
    rather than whatever the form happened to contain — that is the point of
    paper trading. A sandbox session gets the same treatment against its
    fixture quotes, so a market order does not fill at zero.

    The provenance comes from the quote, never from the session. Deriving it
    from `uses_broker_data` stamped every credentialed fill `live_ltp`,
    including one priced from a holdings snapshot taken on a Sunday.
    """
    try:
        from engine.app.choice_gateway.market import get_multiple_touchline

        quotes = get_multiple_touchline(session, f"{segment_id}_{token}")
        for quote in quotes.get("data", []):
            ltp = quote.get("ltp")
            if ltp:
                source = quote.get("source") or (
                    "live_ltp" if session.uses_broker_data else "sandbox_quote")
                return float(ltp), source
    except ChoiceGatewayError:
        # Deliberately not swallowed. The quote layer refuses rather than
        # invent a price, and catching that here reinstated the invention by
        # another route: the caller's own submitted price became the fill, so
        # a market order posted with price_in_paisa=154020 filled at 1540.20
        # against a real 1118.50. A refusal upstream has to stay a refusal.
        raise
    except Exception as exc:
        logger.warning("Could not price simulated fill from a quote: %s", exc)

    # A market order has no price of its own, so there is nothing honest to
    # fall back to. Only a priced order may use what it was given.
    if not submitted_price:
        raise OrderRejected(
            "No price is available for this instrument, so a market order "
            "cannot be simulated. Use a limit order, or pick an instrument "
            "with a live price."
        )
    return submitted_price, "submitted"


def _simulate_order(
    session: ChoiceSession,
    segment_id: int,
    token: int,
    side: str,
    order_type: str,
    quantity: int,
    price: float,
    symbol: str,
) -> Dict[str, Any]:
    """Fill an order locally, in DEMO or PAPER mode. Nothing is sent to Choice."""
    is_market = order_type in {"RL_MKT", "SL_MKT"}
    if is_market:
        fill_price, source = _market_fill_price(session, segment_id, token, price)
    else:
        fill_price, source = price, "limit"

    # A market order with no obtainable price would otherwise fill at zero and
    # silently corrupt the paper book. Refuse instead.
    if fill_price <= 0:
        raise OrderRejected(
            f"Cannot price a {order_type} order for {symbol or token}: no quote is "
            "available for this instrument. Check that market data is enabled on "
            "the Choice account, or place a limit order with an explicit price.",
            f"price_source={source}",
        )

    label = "PAPER" if session.is_paper else "DEMO"
    order_id = f"{label}-{next(_demo_counter):06d}"
    instrument = symbol or str(token)

    record = {
        "order_id": order_id,
        "exchange_order_no": "",
        "symbol": instrument,
        "side": side,
        "quantity": quantity,
        "price": round(fill_price, 2),
        "price_source": source,
        "status": "SIMULATED",
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    session.simulated_orders.insert(0, record)

    position = session.record_simulated_fill(instrument, side, quantity, fill_price)
    if session.is_demo:
        session.demo_used_margin += quantity * fill_price

    logger.info(
        "%s order %s filled locally for user %s: %s %s %s @ %.2f",
        label, order_id, session.owner_key, side, quantity, instrument, fill_price,
    )

    if session.is_paper:
        # Name the price for what it is. A holdings snapshot can be Friday's
        # close; calling that "the live price" is the same class of untruth as
        # filling at a fixture.
        described = {
            "live_ltp": "the live price",
            "holdings_snapshot": "the last price Choice reported for your holding",
            "submitted": "the price you specified",
            "limit": "your limit price",
        }.get(source, "the price available")
        message = (
            f"Paper order filled at {described} ({side} {quantity} "
            f"{instrument} @ {fill_price:.2f}). Nothing was sent to Choice, "
            "and no real funds moved."
        )
    else:
        message = (
            f"Sandbox order simulated ({side} {quantity} @ {fill_price:.2f}). "
            "No order was sent to Choice."
        )

    return {
        "status": "SUCCESS",
        "mode": label,
        "order_id": order_id,
        "message": message,
        "data": record,
        "position": position,
        "paper": session.paper_pnl(),
    }


def place_order(
    session: ChoiceSession,
    segment_id: int,
    token: int,
    order_type: str,
    side: str,
    quantity: int,
    price: float = 0.0,
    trigger_price: float = 0.0,
    validity: int = 1,
    product_type: str = "CNC",
    disclosed_qty: int = 0,
    symbol: str = "",
) -> Dict[str, Any]:
    """Place an order for this session.

    Raises :class:`OrderRejected` / :class:`ChoiceGatewayError` on any failure;
    a returned dict always means the order was accepted.
    """
    clean = validate_order(
        segment_id=segment_id,
        token=token,
        order_type=order_type,
        side=side,
        quantity=quantity,
        price=price,
        product_type=product_type,
        owner_key=session.owner_key,
        # The session decides which budget this order spends. A paper fill has
        # not cost anyone anything, so it must not consume the allowance that
        # protects real funds.
        simulated=session.simulates_orders,
        reference_price=_reference_price(session, segment_id, token, price),
    )
    session.order_limiter.acquire()

    # DEMO and PAPER are both filled here and never reach Choice. The
    # difference is only where the fill price comes from.
    if session.simulates_orders:
        return _simulate_order(
            session=session,
            segment_id=segment_id,
            token=token,
            side=clean["side"],
            order_type=clean["order_type"],
            quantity=quantity,
            price=price,
            symbol=symbol,
        )

    client = session.require_client()
    bs_code = 1 if clean["side"] == "BUY" else 2

    logger.info(
        "Submitting %s order: user=%s env=%s segment=%s token=%s qty=%s price=%s",
        clean["side"], session.owner_key, session.environment,
        segment_id, token, quantity, price,
    )

    # Sent through `client.request` rather than `client.orders.place_order`,
    # purely so the client order number is ours. The SDK hardcodes 123456 on
    # every order, and cancellation matches on that field.
    client_order_no = next_client_order_no()
    response = client.request("POST", "api/OpenAPI/V2/NewOrder", {
        "SegmentId": int(segment_id),
        "Token": int(token),
        "OrderType": clean["order_type"],
        "BS": bs_code,
        "Qty": int(quantity),
        "DisclosedQty": int(disclosed_qty),
        "Price": float(price),
        "TriggerPrice": float(trigger_price),
        "Validity": int(validity),
        "ProductType": clean["product_type"],
        "IsEdisReq": False,
        "Remarks": "API",
        "ModeTyp": "WEBAPI",
        "Mode": 1,
        "DeviceId": "MAC",
        "ClientOrderNo": client_order_no,
    })

    # `is_failure`, not a hand-rolled check. The version here read
    # `response.get("Status", "Success")` — an exact-cased key defaulting to
    # success, over a failure set missing "false". A lowercase `status` key, or
    # a `"false"` status, meant a refused order was recorded as accepted and
    # the trader believed they held a position they did not have.
    if is_failure(response):
        raise OrderRejected(
            failure_reason(response) or "Choice rejected the order",
            str(response)[:500],
        )

    # `unwrap_dict` first: Choice nests the reply under `Response`, and reading
    # the top level returned "" — which made every live order look
    # `missing_at_broker` to reconciliation, permanently.
    payload = unwrap_dict(response) or (response if isinstance(response, dict) else {})
    order_id = (pick_str(payload, "ClientOrderNo", "OrderNo", "OrderNumber")
                or str(client_order_no))
    return {
        "status": "SUCCESS",
        "mode": "LIVE",
        "order_id": order_id,
        "message": f"Order accepted by Choice ({clean['side']} {quantity} @ {price:.2f})",
        "data": payload,
    }


def modify_order(
    session: ChoiceSession,
    client_order_no: int,
    exchange_order_no: str,
    gateway_order_no: str,
    segment_id: int,
    token: int,
    order_type: str,
    side: str,
    quantity: int,
    price: float = 0.0,
    trigger_price: float = 0.0,
    validity: int = 1,
    product_type: str = "CNC",
    disclosed_qty: int = 0,
) -> Dict[str, Any]:
    """Amend a working order in place.

    Amending is not a lesser act than placing: raising the quantity or the
    limit price increases exposure exactly as a new order would, so the same
    validation and the same risk budget apply. The alternative — cancel and
    replace — also loses queue priority at the exchange, which is the reason
    this exists at all.
    """
    clean = validate_order(
        segment_id=segment_id,
        token=token,
        order_type=order_type,
        side=side,
        quantity=quantity,
        price=price,
        product_type=product_type,
        owner_key=session.owner_key,
        simulated=session.simulates_orders,
    )

    # A simulated order is filled the moment it is placed, so no simulated
    # order is ever working. Saying so is better than accepting the request and
    # changing nothing, which would read as success.
    if session.simulates_orders:
        raise OrderRejected(
            "Simulated orders fill immediately, so there is nothing working to "
            "modify. Place a new order instead."
        )

    session.order_limiter.acquire()
    client = session.require_client()

    logger.info(
        "Modifying order %s: user=%s env=%s qty=%s price=%s trigger=%s",
        client_order_no, session.owner_key, session.environment,
        quantity, price, trigger_price,
    )

    response = client.orders.modify_order(
        client_order_no=int(client_order_no),
        exchange_order_no=str(exchange_order_no or ""),
        gateway_order_no=str(gateway_order_no or ""),
        segment_id=int(segment_id),
        token=int(token),
        order_type=clean["order_type"],
        bs=1 if clean["side"] == "BUY" else 2,
        qty=int(quantity),
        price=float(price),
        trigger_price=float(trigger_price),
        validity=int(validity),
        product_type=clean["product_type"],
        disclosed_qty=int(disclosed_qty),
    )

    if is_failure(response):
        raise OrderRejected(
            failure_reason(response) or "Choice rejected the modification",
            str(response)[:500],
        )

    payload = response if isinstance(response, dict) else {}
    return {
        "status": "SUCCESS",
        "mode": "LIVE",
        "order_id": pick_str(payload, "ClientOrderNo", "OrderNo", "OrderNumber")
                    or str(client_order_no),
        "message": f"Order {client_order_no} amended to {quantity} @ {price:.2f}",
        "data": payload,
    }


def get_order_book(session: ChoiceSession) -> Dict[str, Any]:
    # Simulated sessions show their own fills; the broker has no record of them.
    if session.simulates_orders:
        return {
            "status": "SUCCESS",
            "mode": "PAPER" if session.is_paper else "DEMO",
            "data": list(session.simulated_orders),
            "paper": session.paper_pnl(),
        }

    client = session.require_client()
    for call in ("get_order_book_v2", "get_order_book"):
        fetch = getattr(client.orders, call, None)
        if fetch is None:
            continue
        rows = unwrap_list(fetch())
        if rows:
            return {
                "status": "SUCCESS",
                "mode": "LIVE",
                "data": [_normalize_order(r) for r in rows],
            }
    return {"status": "SUCCESS", "mode": "LIVE", "data": []}


def get_trade_book(session: ChoiceSession) -> Dict[str, Any]:
    # Simulated fills are their own trade record; the broker has none.
    if session.simulates_orders:
        return {
            "status": "SUCCESS",
            "mode": "PAPER" if session.is_paper else "DEMO",
            "data": list(session.simulated_orders),
            "paper": session.paper_pnl(),
        }

    client = session.require_client()
    rows = unwrap_list(client.orders.get_trade_book())
    return {
        "status": "SUCCESS",
        "mode": "LIVE",
        "data": [_normalize_order(r) for r in rows],
    }


# Broker statuses that mean the order is still working and can be cancelled.
# Anything else is terminal, and asking Choice to cancel it just earns a
# rejection.
# Terminal, not open. Inverted deliberately: this was an allowlist of open
# statuses, so any wording Choice used that was not on the list read as
# terminal and the kill switch skipped the order in silence. Choice's exact
# strings are unverified — the live order book has only ever come back empty —
# so the safe default for an unknown status is "still working". Trying to
# cancel a finished order fails loudly; skipping a live one does not.
TERMINAL_STATUSES = (
    "EXECUTED", "FILLED", "COMPLETE", "TRADED",
    "CANCEL", "REJECT", "EXPIRED", "LAPSED",
)


def is_open_status(status: Any) -> bool:
    text = str(status or "").strip().upper()
    if not text:
        return False
    # "PARTIALLY EXECUTED" contains "EXECUTED" but the unfilled remainder is
    # still live at the exchange, so it is checked before the terminal words.
    if "PARTIAL" in text:
        return "CANCEL" not in text and "REJECT" not in text
    return not any(word in text for word in TERMINAL_STATUSES)


def next_client_order_no() -> int:
    """A client order number that is unique to this order.

    The SDK hardcodes ``"ClientOrderNo": 123456`` on every ``NewOrder``, so
    every order this platform placed shared one number. Cancellation and
    amendment find an order by that field, which meant a Cancel on one row
    could withdraw a different order entirely — and reconciliation collapsed
    the whole book into a single bucket.

    Milliseconds since the epoch, truncated to fit a 32-bit signed int, plus a
    per-process counter so two orders in the same millisecond still differ.
    """
    with _order_no_lock:
        base = int(time.time() * 1000) % 2_000_000_000
        global _order_no_last
        _order_no_last = max(base, _order_no_last + 1)
        return _order_no_last


def _side_code(row: Dict[str, Any]) -> int:
    """Choice's numeric side for an order-book row, or a refusal.

    Guessing the side of a cancellation is never safe: it describes a working
    SELL as a BUY, and the broker either refuses it or acts on a record that
    does not match.
    """
    side = _normalize_side(pick(row, "BS", "BuySell", "TransactionType"))
    if side is None:
        raise OrderRejected(
            "Choice did not report which side this order is, so it cannot be "
            "cancelled safely. Cancel it from your Choice account.",
            str(pick(row, "BS", "BuySell", "TransactionType")),
        )
    return 1 if side == "BUY" else 2


def _order_book_rows(client) -> List[Dict[str, Any]]:
    """The broker's working order book, or an exception.

    `unwrap_list` returns `[]` for a `{"Status":"Fail"}` envelope exactly as it
    does for a genuinely empty book, so reading it without checking turned a
    refused fetch into "no orders". The kill switch then reported SUCCESS,
    cancelled 0, and left every order live at the exchange — the one moment the
    platform must not be optimistic.
    """
    last_reason = ""
    for call in (client.orders.get_order_book_v2, client.orders.get_order_book):
        raw = call()
        if is_failure(raw):
            last_reason = failure_reason(raw) or "Choice refused the order book"
            continue
        rows = unwrap_list(raw)
        if rows:
            return rows
        last_reason = ""          # a real, empty book
    if last_reason:
        raise ChoiceUpstreamError("Could not read the order book", last_reason)
    return []


def _cancel_row(client, row: Dict[str, Any]):
    """Cancel one order, echoing the book's own record back to Choice.

    Choice wants the original order returned in full, so every field is read
    from the order book rather than reconstructed. Kept in one place because
    the kill switch and a single cancellation must not drift apart — a field
    that differs between them is a cancellation that works in one path only.
    """
    return client.orders.cancel_order(
        client_order_no=pick_int(row, "ClientOrderNo", "OrderNo", default=0),
        exchange_order_no=pick_str(row, "ExchangeOrderNo", "OrderNumber"),
        gateway_order_no=pick_str(row, "GatewayOrderNo"),
        segment_id=pick_int(row, "SegmentId", "SegID", default=0),
        token=pick_int(row, "Token", default=0),
        order_type=pick_str(row, "OrderType", default="RL_MKT"),
        # Not `pick_int(..., default=1)`. Choice sends "B"/"S" as well as 1/2,
        # and `pick_int` cannot parse a letter, so every lettered SELL fell to
        # the default and was cancelled as a BUY — which the broker refuses,
        # making every such order uncancellable including by the kill switch.
        bs=_side_code(row),
        qty=pick_int(row, "Qty", "OrderQty", default=0),
        price=pick_float(row, "Price", "OrderPrice", default=0.0),
        trigger_price=pick_float(row, "TriggerPrice", default=0.0),
        validity=pick_int(row, "Validity", default=1),
        product_type=pick_str(row, "ProductType", default="CNC"),
        exchange_order_time=pick_str(row, "ExchangeOrderTime"),
        disclosed_qty=pick_int(row, "DisclosedQty", default=0),
    )


def cancel_one(session: ChoiceSession, client_order_no: int) -> Dict[str, Any]:
    """Cancel a single working order by its client order number."""
    if session.simulates_orders:
        for order in session.simulated_orders:
            if str(order.get("order_id")) == str(client_order_no):
                if not is_open_status(order.get("status")):
                    raise OrderRejected(
                        f"Order {client_order_no} is {order.get('status')}, "
                        "so there is nothing to cancel.")
                order["status"] = "CANCELLED"
                return {"status": "SUCCESS", "mode": session.mode.value,
                        "cancelled": 1,
                        "message": f"Order {client_order_no} cancelled"}
        raise OrderRejected(f"No simulated order {client_order_no}.")

    client = session.require_client()
    raw = _order_book_rows(client)

    # Matched against the live book rather than trusting the caller's numbers:
    # a cancellation assembled from a stale page could name an order that has
    # since traded.
    #
    # Any of the three identifiers may be used. Orders placed before this
    # platform issued its own client order numbers all carry the SDK's
    # hardcoded 123456, so that field alone cannot identify one of them.
    reference = str(client_order_no).strip()
    matches = [
        row for row in raw
        if reference in {
            str(pick(row, "ClientOrderNo", "OrderNo") or ""),
            str(pick(row, "ExchangeOrderNo", "OrderNumber") or ""),
            str(pick(row, "GatewayOrderNo") or ""),
        }
    ]

    if not matches:
        raise OrderRejected(f"Order {client_order_no} is not in the order book.")

    # Ambiguity is refused, never resolved by picking the first row. Cancelling
    # one of two orders that share a number is a coin toss over which position
    # stays open, and the response would report success either way.
    if len(matches) > 1:
        symbols = ", ".join(
            sorted({pick_str(r, "TradingSymbol", "Symbol") or "?" for r in matches}))
        raise OrderRejected(
            f"{len(matches)} orders share the reference {client_order_no} "
            f"({symbols}), so cancelling by it could withdraw the wrong one. "
            "Use the exchange order number, or cancel from your Choice account.",
        )

    row = matches[0]
    status = pick(row, "Status", "OrderStatus")
    if not is_open_status(status):
        raise OrderRejected(
            f"Order {client_order_no} is {status}, so there is nothing to cancel.")

    resp = _cancel_row(client, row)
    if is_failure(resp):
        raise OrderRejected(
            failure_reason(resp) or "Choice refused the cancellation",
            str(resp)[:500])
    return {"status": "SUCCESS", "mode": "LIVE", "cancelled": 1,
            "message": f"Order {client_order_no} cancelled"}


def cancel_all_open(session: ChoiceSession) -> Dict[str, Any]:
    """Cancel every working order on the account. The kill switch.

    Each cancellation is attempted independently: one order Choice refuses to
    cancel must not stop the rest, because the whole point of a kill switch is
    that it works when things are already going wrong. Failures are reported,
    never swallowed.
    """
    if session.simulates_orders:
        cancelled = 0
        for order in session.simulated_orders:
            if is_open_status(order.get("status")):
                order["status"] = "CANCELLED"
                cancelled += 1
        return {
            "status": "SUCCESS",
            "mode": "PAPER" if session.is_paper else "DEMO",
            "cancelled": cancelled, "failed": 0, "failures": [],
        }

    client = session.require_client()
    raw = _order_book_rows(client)
    working = [r for r in raw if is_open_status(pick(r, "Status", "OrderStatus"))]

    cancelled, failures = 0, []
    for row in working:
        symbol = pick_str(row, "TradingSymbol", "Symbol") or "?"
        try:
            resp = _cancel_row(client, row)
            if is_failure(resp):
                failures.append({"symbol": symbol, "reason": failure_reason(resp)})
            else:
                cancelled += 1
        except Exception as exc:               # keep going; report at the end
            failures.append({"symbol": symbol, "reason": str(exc)})

    if failures:
        logger.warning("Kill switch: %d cancelled, %d failed", cancelled, len(failures))
    return {
        "status": "SUCCESS", "mode": "LIVE", "cancelled": cancelled,
        "failed": len(failures), "failures": failures,
        "considered": len(working),
    }


def reconcile(session: ChoiceSession, local_orders: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Compare what this platform recorded against what Choice holds.

    The interface shows the local order book. If the two ever diverge the
    interface would be confidently wrong, which is the failure mode worth
    catching early — a missed fill or a silent rejection shows up here first.
    """
    if session.simulates_orders:
        return {
            "status": "SUCCESS",
            "mode": "PAPER" if session.is_paper else "DEMO",
            "checked": len(local_orders), "matched": len(local_orders),
            "missing_at_broker": [], "unknown_locally": [], "status_mismatch": [],
            "note": "Simulated orders exist only here; there is nothing to reconcile.",
        }

    broker = {
        pick_str(r, "ClientOrderNo", "OrderNo"): r
        for r in unwrap_list(session.require_client().orders.get_order_book_v2())
    }

    # Only orders this platform believes reached the broker are comparable.
    submitted = [o for o in local_orders
                 if str(o.get("status", "")).upper() not in {"SIMULATED", "REJECTED"}]

    matched, missing, mismatched = 0, [], []
    for order in submitted:
        key = str(order.get("client_order_no") or "")
        row = broker.get(key)
        if row is None:
            missing.append({"client_order_no": key, "symbol": order.get("symbol"),
                            "status": order.get("status")})
            continue
        their_status = pick_str(row, "Status", "OrderStatus").upper()
        ours = str(order.get("status", "")).upper()
        # ACCEPTED locally is a submission receipt, not a fill, so it is
        # compatible with any working or completed broker status.
        if ours == "ACCEPTED" or their_status.startswith(ours[:6]):
            matched += 1
        else:
            mismatched.append({"client_order_no": key, "symbol": order.get("symbol"),
                               "ours": ours, "broker": their_status})

    local_keys = {str(o.get("client_order_no") or "") for o in submitted}
    unknown = [{"client_order_no": k,
                "symbol": pick_str(v, "TradingSymbol", "Symbol"),
                "status": pick_str(v, "Status", "OrderStatus")}
               for k, v in broker.items() if k and k not in local_keys]

    return {
        "status": "SUCCESS", "mode": "LIVE",
        "checked": len(submitted), "matched": matched,
        "missing_at_broker": missing,      # we think it went; Choice disagrees
        "unknown_locally": unknown,        # placed elsewhere, e.g. Choice's app
        "status_mismatch": mismatched,
        "in_sync": not missing and not mismatched,
    }
