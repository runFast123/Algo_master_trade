import logging
from typing import Any, Dict

from fastapi import APIRouter, Depends, Query

from app.dependencies import get_choice_session
from engine.app.choice_gateway import market as market_gateway
from engine.app.choice_gateway import scrip_master
from engine.app.choice_gateway import sockets_pricefeed
from engine.app.choice_gateway.client_manager import ChoiceSession
from engine.app.choice_gateway.errors import ChoiceUpstreamError

logger = logging.getLogger("market_api")

router = APIRouter()

# NIFTY 50, BANK NIFTY, FIN NIFTY, INDIA VIX, and four liquid large caps.
DEFAULT_WATCHLIST = "1_26000,1_26009,1_26037,1_26017,1_2885,1_1594,1_11536,1_1333"


@router.get("/search")
def search_instruments(
    q: str = Query(..., min_length=1, max_length=64),
    session: ChoiceSession = Depends(get_choice_session),
) -> Dict[str, Any]:
    """Instrument search against this user's Choice session."""
    return scrip_master.search_scrip(session, q)


@router.get("/status")
def get_market_status(
    session: ChoiceSession = Depends(get_choice_session),
) -> Dict[str, Any]:
    return market_gateway.get_market_status(session)


@router.get("/quotes")
def get_market_quotes(
    seg_tokens: str = Query(DEFAULT_WATCHLIST, max_length=1024,
                            description="Comma-separated segment_token pairs"),
    session: ChoiceSession = Depends(get_choice_session),
) -> Dict[str, Any]:
    """Level 1 quotes. Cached per session for a few seconds, keyed by request."""
    try:
        return market_gateway.get_multiple_touchline(session, seg_tokens)
    except ChoiceUpstreamError as exc:
        logger.info("Market quotes upstream exception: %s | %s", exc.message, exc.details)
        return {
            "status": "PARTIAL",
            "mode": session.mode.value,
            "is_open": market_gateway.is_indian_market_hours(),
            "data": [],
            "cached": False,
            "upstream_error": f"{exc.message} ({exc.details})" if exc.details else exc.message,
        }


@router.get("/profile")
def get_profile(
    session: ChoiceSession = Depends(get_choice_session),
) -> Dict[str, Any]:
    """Account profile: name, client id, segments, registered bank accounts.

    This is what a user would otherwise open the Choice website to read, and
    the deposit and withdrawal forms need the bank account from it.
    """
    return market_gateway.get_user_profile(session)


@router.get("/instrument/{token}")
def get_instrument(
    token: str,
    session: ChoiceSession = Depends(get_choice_session),
) -> Dict[str, Any]:
    """Contract details for one instrument, including its lot size.

    The order ticket needs the lot before it can validate a derivatives
    quantity, and the exchange is the wrong place to discover it is wrong.
    """
    details = scrip_master.get_details(session, token)
    lot = scrip_master.get_lot_size(session, token)
    return {
        "status": "SUCCESS",
        "mode": session.mode.value,
        "data": details or {},
        "lot_size": lot,
        # A null lot is "we could not establish it", not "one". The interface
        # needs to tell those apart: the first warrants a warning, the second
        # means the quantity is free.
        "lot_known": lot is not None,
    }


@router.get("/feed")
def feed_status(
    session: ChoiceSession = Depends(get_choice_session),
) -> Dict[str, Any]:
    """Whether this account is provisioned for the broadcast price feed.

    Choice hands the feed address out in the logon response, so an address
    here is evidence of entitlement — answerable without opening a socket.
    A ``vendor_default`` source means the logon gave us nothing.
    """
    if session.is_demo:
        return {"status": "SUCCESS", "mode": "DEMO", "provisioned": False,
                "detail": "A sandbox session has no broker feed."}

    endpoint = sockets_pricefeed.feed_endpoint(session)
    provisioned = endpoint["source"] == "logon_response"
    connection = sockets_pricefeed.get_connection(session)
    return {
        "status": "SUCCESS",
        "mode": session.mode.value,
        "provisioned": provisioned,
        "endpoint": endpoint,
        "running": connection.is_running,
        "subscriptions": connection.subscriptions,
        "detail": (
            "Choice supplied a feed address for this session."
            if provisioned else
            "Choice supplied no feed address; the vendor default would be a "
            "guess. Treat the feed as unavailable until Choice confirms it."
        ),
    }
