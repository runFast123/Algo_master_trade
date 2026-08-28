"""Market status and quotes, scoped to a single user's Choice session."""

import datetime
import logging
import time
from typing import Any, Dict, List

from engine.app import market_calendar
from engine.app.choice_gateway.client_manager import ChoiceSession, SessionMode
from engine.app.choice_gateway.errors import ChoiceNotConnected, ChoiceUpstreamError
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

# Cached briefly to stay well inside the Choice REST rate limits. The cache is
# held on the session and keyed by the exact tokens requested, so one user's
# quotes are never served to another and a request for indices never returns
# another request's stocks.
QUOTE_CACHE_TTL = 3.0

IST_OFFSET = datetime.timedelta(hours=5, minutes=30)
MARKET_OPEN_MINUTE = 9 * 60 + 15   # 09:15 IST
MARKET_CLOSE_MINUTE = 15 * 60 + 30  # 15:30 IST


def _ist_now(now: datetime.datetime = None) -> datetime.datetime:
    now_utc = now or datetime.datetime.now(datetime.timezone.utc)
    return now_utc.astimezone(datetime.timezone.utc).replace(tzinfo=None) + IST_OFFSET


def is_indian_market_hours(now: datetime.datetime = None) -> bool:
    """Session check in IST, including exchange trading holidays."""
    ist_now = _ist_now(now)
    if not market_calendar.is_trading_day(ist_now.date()):
        return False
    minutes = ist_now.hour * 60 + ist_now.minute
    return MARKET_OPEN_MINUTE <= minutes <= MARKET_CLOSE_MINUTE


def get_market_status(session: ChoiceSession) -> Dict[str, Any]:
    is_open = is_indian_market_hours()
    today = _ist_now().date()
    calendar = market_calendar.describe(today)

    # Say *why* it is shut. "Closed" alone reads identically whether it is
    # 4pm, a Sunday, or Republic Day, and those need different reactions.
    if is_open:
        reason = None
    elif calendar["reason"]:
        reason = calendar["reason"]
    else:
        reason = "Outside 09:15-15:30 IST"

    base = {
        "status": "SUCCESS",
        "is_open": is_open,
        "session": "REGULAR" if is_open else "CLOSED",
        "market_hours": "09:15 IST - 15:30 IST",
        "closed_reason": reason,
        "date": today.isoformat(),
        "calendar": calendar,
        "holiday_calendar": "loaded" if calendar["known"] else "not configured",
    }

    if session.is_demo:
        base["mode"] = "DEMO"
        return base

    client = session.require_client()
    base["mode"] = session.mode.value
    try:
        upstream = client.market.get_market_status()
    except ChoiceUpstreamError as exc:
        logger.warning("Choice market status unavailable: %s", exc.message)
        base["upstream_error"] = exc.message
        return base

    if is_failure(upstream):
        base["upstream_error"] = failure_reason(upstream)
        return base

    base["segments"] = _parse_segment_status(upstream)
    return base


def _parse_segment_status(payload: Any) -> Dict[str, Any]:
    """Flatten Choice's segment status map.

    It arrives as ``{"lstMktStatus": {"<segment>": {"<market type>":
    {"MktType": ..., "Status": ...}}}}`` - keyed twice, not a list - so it
    needs unpacking rather than unwrapping.
    """
    statuses: Dict[str, Any] = {}
    root = payload.get("Response") if isinstance(payload, dict) else None
    segment_map = root.get("lstMktStatus") if isinstance(root, dict) else None
    if not isinstance(segment_map, dict):
        return statuses

    for segment_id, markets in segment_map.items():
        if not isinstance(markets, dict):
            continue
        entries = []
        for market_type, detail in markets.items():
            if isinstance(detail, dict):
                entries.append({
                    "market_type": pick_str(detail, "MktType", default=str(market_type)),
                    "status": pick_str(detail, "Status"),
                })
        if entries:
            statuses[str(segment_id)] = entries
    return statuses


def _normalize_quote(item: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "symbol": pick_str(item, "Symbol", "symbol", "TradingSymbol", "ScripName"),
        "segment_id": pick_int(item, "SegID", "SegmentId", "SegId", "Segment",
                               "segment_id", default=0),
        "token": pick_str(item, "Token", "token", "ScripCode", "SecurityId"),
        # Scaled like every other Choice price. This value becomes the fill
        # price for simulated market orders, so an unscaled quote would book
        # paper trades a hundredfold away from the market.
        "ltp": scaled_price(item, "Ltp", "LTP", "ltp", "LastPrice",
                            "LastTradedPrice", "Close", "ClosePrice"),
        "chg": pick_float(item, "Chg", "chg", "Change", "NetChange",
                          "NetChangeFromPrevClose"),
        "chg_pct": pick_float(item, "PerChg", "PercentChange", "PerChange",
                              "ChangePercent", "chg_pct"),
    }


def _requested_keys(requested: str) -> set:
    """The (segment_id, token) pairs the caller actually asked for."""
    keys = set()
    for pair in str(requested or "").split(","):
        pair = pair.strip()
        if "_" not in pair:
            continue
        seg, _, tok = pair.partition("_")
        if seg.strip().isdigit() and tok.strip():
            keys.add((int(seg), str(tok.strip())))
    return keys


def get_multiple_touchline(session: ChoiceSession, seg_tokens: str) -> Dict[str, Any]:
    """Level 1 quotes for ``segment_token`` pairs, e.g. ``"1_2885,1_1594"``."""
    cache_key = str(seg_tokens or "").strip()
    cached = session.quote_cache.get(cache_key)
    now = time.time()
    if cached and (now - cached["at"]) < QUOTE_CACHE_TTL:
        result = dict(cached["payload"])
        result["cached"] = True
        return result

    if session.is_demo:
        payload = {
            "status": "SUCCESS",
            "mode": "DEMO",
            "is_open": is_indian_market_hours(),
            "data": _demo_quotes(cache_key),
            "cached": False,
        }
        session.quote_cache[cache_key] = {"at": now, "payload": payload}
        return payload

    if session.mode is SessionMode.DISCONNECTED:
        raise ChoiceNotConnected(
            "Not signed in to Choice FINX. Connect your Choice account first."
        )

    raw = None
    touchline_err = None
    try:
        client = session.require_client()
        raw = client.market.get_multiple_touchline(cache_key)
    except Exception as exc:
        touchline_err = exc

    if touchline_err is not None or is_failure(raw):
        # In LIVE mode, fail loudly since real money execution requires live broker feed
        if session.is_live:
            session.market_data_ok = False
            raise ChoiceUpstreamError(
                "Choice could not return quotes",
                str(touchline_err) if touchline_err else failure_reason(raw),
            )

        # PAPER, signed in with real credentials. This used to fall back to the
        # sandbox price table "so simulation can run after-hours" — but those
        # are fixed numbers written months ago, and against a live account they
        # were 38% to 89% wrong: RELIANCE quoted at 2504.50 while Choice's own
        # holdings record said 1324.10. Paper fills went through at the fiction.
        #
        # A paper trade is meant to answer "what would this strategy have
        # done", and an invented price makes the answer worthless in a way that
        # looks exactly like a working system. Real prices or none.
        reason = str(touchline_err) if touchline_err else failure_reason(raw)
        quotes = _quotes_from_holdings(session, cache_key)
        if quotes:
            logger.info(
                "Touchline unavailable (%s); pricing %s from the holdings "
                "snapshot instead.", reason, cache_key,
            )
            payload = {
                "status": "SUCCESS",
                "mode": session.mode.value,
                "is_open": is_indian_market_hours(),
                "data": quotes,
                "cached": False,
                "source": "holdings_snapshot",
            }
            session.quote_cache[cache_key] = {"at": now, "payload": payload}
            return payload

        session.market_data_ok = False
        raise ChoiceUpstreamError(
            "Choice returned no price for this instrument, and it is not in "
            "your holdings to price from. Paper trading needs a real price - "
            "filling at a placeholder would make the result meaningless.",
            reason,
        )

    session.market_data_ok = True

    rows = unwrap_list(raw)
    if not rows and isinstance(raw, dict):
        logger.info(
            "Choice returned no quotes for %s; envelope keys were %s",
            cache_key, sorted(raw.keys())[:20],
        )

    # Keep only what was asked for. Choice has been observed returning rows for
    # instruments that were not requested, and `_market_fill_price` takes the
    # first quote carrying a price — so an unrequested row would fill an order
    # at the wrong instrument's price.
    wanted = _requested_keys(cache_key)
    quotes, seen = [], set()
    for row in rows:
        quote = _normalize_quote(row)
        key = (quote.get("segment_id"), str(quote.get("token") or ""))
        if wanted and key not in wanted:
            continue
        if key in seen:
            continue
        seen.add(key)
        quotes.append(quote)

    payload = {
        "status": "SUCCESS",
        "mode": session.mode.value,
        "is_open": is_indian_market_hours(),
        "data": quotes,
        "cached": False,
    }
    session.quote_cache[cache_key] = {"at": now, "payload": payload}
    return payload


# Sandbox instruments. Static reference values, clearly labelled as sandbox
# data so they are never mistaken for a live quote.
_DEMO_INSTRUMENTS = {
    "1_26000": {"symbol": "NIFTY 50", "ltp": 24326.65, "chg": -43.78, "chg_pct": -0.18},
    "1_26009": {"symbol": "BANKNIFTY", "ltp": 52140.30, "chg": 218.00, "chg_pct": 0.42},
    "1_26037": {"symbol": "FINNIFTY", "ltp": 23410.80, "chg": 28.10, "chg_pct": 0.12},
    "1_26017": {"symbol": "INDIA VIX", "ltp": 14.25, "chg": -0.31, "chg_pct": -2.10},
    "1_2885": {"symbol": "RELIANCE", "ltp": 2504.50, "chg": 34.50, "chg_pct": 1.40},
    "1_1594": {"symbol": "INFY", "ltp": 1540.20, "chg": 12.20, "chg_pct": 0.80},
    "1_11536": {"symbol": "TCS", "ltp": 4120.00, "chg": -12.40, "chg_pct": -0.30},
    "1_1333": {"symbol": "HDFCBANK", "ltp": 1650.00, "chg": 18.00, "chg_pct": 1.10},
    "3_1": {"symbol": "SENSEX", "ltp": 79850.15, "chg": -119.77, "chg_pct": -0.15},
}


def _quotes_from_holdings(session: ChoiceSession, seg_tokens: str) -> List[Dict[str, Any]]:
    """Price the requested instruments from the holdings snapshot.

    Discovered from a live diagnostic: on an account where MultipleTouchline
    fails outright, Holdings still answers, and every row carries ``LTP``,
    ``ClosePrice`` and the ``PriceDivisor`` needed to scale them. That is a
    real, current price from Choice for anything the account actually holds —
    a far better answer than a fixture, and available today without any new
    entitlement.

    Limited to held instruments by nature. Returns an empty list for anything
    else so the caller refuses rather than inventing a number.
    """
    wanted = _requested_keys(seg_tokens)
    if not wanted:
        return []

    # Positions as well as holdings. Restricting this to demat holdings meant
    # every instrument the account does not own became un-paper-tradable, which
    # on an account with no working quote endpoint is most of the market.
    rows = []
    for source in ("get_holdings", "get_positions"):
        try:
            from engine.app.choice_gateway import portfolio

            rows.extend(getattr(portfolio, source)(session).get("data") or [])
        except Exception as exc:
            logger.info("Could not price from %s: %s", source, exc)
    if not rows:
        return []

    quotes = []
    for row in rows:
        key = (row.get("segment_id"), str(row.get("token") or ""))
        if key not in wanted:
            continue
        # `priced_from_close` means Choice reported no last price and the
        # close was substituted. Publishing that as a quote asserts the
        # instrument traded and finished exactly flat, which is the same false
        # flat the analytics layer already refuses to draw.
        if row.get("priced_from_close"):
            continue
        last = row.get("current_price")
        if not last:
            continue
        close = row.get("close_price")
        change = (last - close) if close else None
        quotes.append({
            "symbol": row.get("symbol"),
            "segment_id": row.get("segment_id"),
            "token": row.get("token"),
            "ltp": last,
            "chg": round(change, 2) if change is not None else None,
            "chg_pct": round(change / close * 100, 2) if close else None,
            # Named so nothing downstream can mistake a holdings-derived price
            # for a live tick. It is real, but it is a snapshot.
            "source": "holdings_snapshot",
        })
    return quotes


def _demo_quotes(seg_tokens: str) -> List[Dict[str, Any]]:
    """Return sandbox quotes for exactly the instruments requested."""
    quotes = []
    for pair in [p.strip() for p in seg_tokens.split(",") if p.strip()]:
        info = _DEMO_INSTRUMENTS.get(pair)
        segment, _, token = pair.partition("_")
        if info is None:
            continue
        quotes.append({
            "symbol": info["symbol"],
            "segment_id": int(segment) if segment.isdigit() else 0,
            "token": token,
            "ltp": info["ltp"],
            "chg": info["chg"],
            "chg_pct": info["chg_pct"],
        })
    return quotes


def get_user_profile(session: ChoiceSession) -> Dict[str, Any]:
    """Account profile: registered bank accounts, demat BOID, enabled segments.

    Needed before any deposit or withdrawal, because those calls take a bank
    account number and this is where a user finds theirs without opening the
    Choice website.
    """
    if session.is_demo:
        return {
            "status": "SUCCESS",
            "mode": "DEMO",
            "data": {"ClientName": "Sandbox User", "ClientId": "DEMO",
                     "Segments": ["NSE-CASH"], "BankAccounts": []},
        }

    client = session.require_client()
    try:
        raw = client.market.get_user_profile()
    except Exception as exc:
        raise ChoiceUpstreamError("Could not fetch the account profile",
                                  str(exc)) from exc
    if is_failure(raw):
        raise ChoiceUpstreamError("Choice rejected the profile request",
                                  failure_reason(raw))
    return {"status": "SUCCESS", "mode": session.mode.value,
            "data": unwrap_dict(raw)}
