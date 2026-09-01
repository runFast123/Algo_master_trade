"""Instrument lookup, scoped to a single user's Choice session."""

import logging
from typing import Any, Dict, List, Optional

from engine.app.choice_gateway.client_manager import ChoiceSession
from engine.app.choice_gateway.errors import ChoiceUpstreamError

logger = logging.getLogger("choice_gateway")

# Sandbox instrument list. Segment ids follow Annexure A of the Live Data Feed
# Specification: 1 = NSE-CASH, 2 = NSE-DERIVATIVES, 3 = BSE-CASH.
SANDBOX_SCRIPS: Dict[str, Dict[str, Any]] = {
    "RELIANCE": {"segment_id": 1, "token": "2885", "lot_size": 1, "tick_size": 0.05,
                 "company_name": "RELIANCE INDUSTRIES LTD"},
    "TCS": {"segment_id": 1, "token": "11536", "lot_size": 1, "tick_size": 0.05,
            "company_name": "TATA CONSULTANCY SERVICES LTD"},
    "INFY": {"segment_id": 1, "token": "1594", "lot_size": 1, "tick_size": 0.05,
             "company_name": "INFOSYS LIMITED"},
    "HDFCBANK": {"segment_id": 1, "token": "1333", "lot_size": 1, "tick_size": 0.05,
                 "company_name": "HDFC BANK LIMITED"},
    # Indices trade in the cash segment (1), not derivatives.
    "NIFTY 50": {"segment_id": 1, "token": "26000", "lot_size": 25, "tick_size": 0.05,
                 "company_name": "NIFTY 50 INDEX"},
    "BANKNIFTY": {"segment_id": 1, "token": "26009", "lot_size": 15, "tick_size": 0.05,
                  "company_name": "NIFTY BANK INDEX"},
    "FINNIFTY": {"segment_id": 1, "token": "26037", "lot_size": 25, "tick_size": 0.05,
                 "company_name": "NIFTY FINANCIAL SERVICES INDEX"},
}


def _ensure_loaded(session: ChoiceSession) -> bool:
    """Make sure the instrument master is in memory for this session.

    The SDK loads its scrip master CSV inside ``ChoiceClient.login()`` and
    nowhere else. Three of the four ways a session is established here —
    ``validate_totp``, partner OAuth, and ``restore`` on every restart with
    "remember me" — build the client directly and never call it. On those
    sessions the master stayed empty, so instrument search returned nothing and
    every lot size silently came back as 1.
    """
    client = session.require_client()
    master = getattr(client, "scrip_master", None)
    if master is None:
        return False
    if getattr(master, "is_loaded", False):
        return True
    try:
        master.fetch()
    except Exception as exc:
        logger.warning("Could not load the Choice instrument master: %s", exc)
        return False
    return bool(getattr(master, "is_loaded", False))


def search_scrip(session: ChoiceSession, query: str) -> Dict[str, Any]:
    query_upper = str(query).strip().upper()

    if session.is_demo:
        results = [
            {"symbol": symbol, **info}
            for symbol, info in SANDBOX_SCRIPS.items()
            if query_upper in symbol
        ]
        return {"status": "SUCCESS", "mode": "DEMO", "data": results}

    client = session.require_client()
    _ensure_loaded(session)
    try:
        raw = client.scrip_master.search(name=query_upper)
    except Exception as exc:
        raise ChoiceUpstreamError("Instrument search failed", str(exc)) from exc
    data = raw.get("data") if isinstance(raw, dict) else raw
    return {"status": "SUCCESS", "mode": "LIVE", "data": data or []}


def get_token(
    session: ChoiceSession, symbol: str, segment: Optional[str] = None
) -> Optional[str]:
    """Resolve a symbol to its instrument token, or None when unknown.

    Returns None rather than a placeholder token: silently substituting a
    default would place an order on the wrong instrument.
    """
    symbol_upper = str(symbol).strip().upper()

    if session.is_demo:
        info = SANDBOX_SCRIPS.get(symbol_upper)
        return info["token"] if info else None

    client = session.require_client()
    _ensure_loaded(session)
    try:
        found = client.scrip_master.get_token(
            symbol_or_desc=symbol_upper, segment=segment
        )
    except Exception as exc:
        raise ChoiceUpstreamError(
            f"Could not resolve token for {symbol_upper}", str(exc)
        ) from exc

    # The SDK returns a *list of every matching row* when no segment is given —
    # for RELIANCE that is the equity plus several hundred options and futures.
    # Stringifying it produced a "token" thousands of characters long, which is
    # what every symbol-resolved backtest was sending to Choice.
    if isinstance(found, list):
        return _pick_tradable(symbol_upper, found)
    return str(found) if found else None


def _pick_tradable(symbol: str, rows: List[Dict[str, Any]]) -> Optional[str]:
    """Choose the instrument a person means when they type a symbol.

    "RELIANCE" means the share, not `RELIANCE26SEP1380CE`. Preference is the
    cash-segment EQ series; a derivative is only returned when nothing else
    matches, and the choice is logged because silently picking one instrument
    out of hundreds is exactly the kind of guess that should be visible.
    """
    if not rows:
        return None

    def score(row: Dict[str, Any]) -> tuple:
        segment = str(row.get("Segment", "")).strip()
        series = str(row.get("Series", "")).strip().upper()
        desc = str(row.get("SecDesc", "")).strip().upper()
        return (
            0 if segment == "1" else 1,          # cash segment first
            0 if series == "EQ" else 1,          # then the equity series
            0 if desc == symbol else 1,          # then an exact description
            len(desc),                            # then the plainest name
        )

    best = sorted(rows, key=score)[0]
    token = str(best.get("Token") or "").strip()
    if not token:
        return None

    if len(rows) > 1:
        logger.info(
            "%s matched %d instruments; using %s (%s, segment %s, series %s).",
            symbol, len(rows), token, best.get("SecDesc"),
            best.get("Segment"), best.get("Series"),
        )
    return token


def get_details(session: ChoiceSession, token: str) -> Optional[Dict[str, Any]]:
    token_str = str(token).strip()

    if session.is_demo:
        for symbol, info in SANDBOX_SCRIPS.items():
            if info["token"] == token_str:
                return {"symbol": symbol, **info}
        return None

    client = session.require_client()
    _ensure_loaded(session)
    try:
        return client.scrip_master.get_details(token=token_str)
    except Exception as exc:
        raise ChoiceUpstreamError(
            f"Could not fetch details for token {token_str}", str(exc)
        ) from exc


def resolve_instrument(
    session: ChoiceSession, symbol: str, segment_id: Optional[int], token: Optional[str]
) -> Dict[str, Any]:
    """Resolve a backtest/order target to a concrete ``(segment_id, token)``."""
    if token:
        return {"symbol": symbol, "segment_id": int(segment_id or 1), "token": str(token)}

    symbol_upper = str(symbol).strip().upper()
    info = SANDBOX_SCRIPS.get(symbol_upper)

    # 1. Known standard scrip matching segment (or in demo / default segment)
    if info and (session.is_demo or segment_id is None or segment_id == info.get("segment_id")):
        return {
            "symbol": symbol_upper,
            "segment_id": info["segment_id"],
            "token": info["token"],
        }

    # 2. Try Choice scrip master if connected
    resolved = None
    if session.is_connected and not session.is_demo:
        try:
            resolved = get_token(session, symbol_upper, segment=str(segment_id) if segment_id else None)
        except Exception as exc:
            logger.debug("Choice get_token failed for %s: %s", symbol_upper, exc)

    if resolved:
        return {
            "symbol": symbol_upper,
            "segment_id": int(segment_id or 1),
            "token": resolved,
        }

    # 3. Fallback to known standard scrips if available
    if info:
        return {
            "symbol": symbol_upper,
            "segment_id": info["segment_id"],
            "token": info["token"],
        }

    raise ChoiceUpstreamError(
        f"Unknown instrument {symbol_upper!r}. Supply an explicit token."
    )


def get_lot_size(session: ChoiceSession, token: str) -> Optional[int]:
    """Contract size for an instrument, or None when it cannot be established.

    An F&O order must be a whole multiple of the lot and the exchange rejects
    anything else, so this decides whether a quantity is valid.

    None rather than 1 when the instrument master could not be loaded. The
    difference matters: 1 means "this instrument trades in single units", which
    disables the multiple-of-lot check entirely and silently. Saying "unknown"
    lets the interface warn instead of quietly waving a bad quantity through.
    """
    token_str = str(token).strip()

    if session.is_demo:
        for info in SANDBOX_SCRIPS.values():
            if info["token"] == token_str:
                return int(info.get("lot_size", 1))
        return 1

    if not _ensure_loaded(session):
        return None

    details = get_details(session, token_str) or {}
    if not details:
        return None
    for key in ("MarketLot", "LotSize", "lot_size", "BoardLotQty"):
        raw = details.get(key)
        if raw in (None, ""):
            continue
        try:
            lot = int(float(raw))
        except (TypeError, ValueError):
            continue
        if lot > 0:
            return lot
    # The master has this instrument but names no lot: a cash scrip trades in
    # single units, which is a real answer rather than an absent one.
    return 1
