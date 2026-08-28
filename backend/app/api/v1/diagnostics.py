"""Diagnostics for the Choice connection.

When the platform shows no data for an account that clearly has some, the
question is always the same: what did Choice actually send? These endpoints
answer it.

By default only the *shape* is reported — field names and value types, never
values — so the output can be shared to get a mapping corrected without
disclosing balances or holdings. Values are included only when explicitly
requested by the account owner.
"""

import logging
from datetime import date, timedelta
from typing import Any, Dict

from fastapi import APIRouter, Depends, Query

from app.dependencies import get_choice_session, get_current_user
from app.models.user import User
from engine.app.choice_gateway import funds as funds_gateway
from engine.app.choice_gateway import market as market_gateway
from engine.app.choice_gateway import portfolio as portfolio_gateway
from engine.app.choice_gateway import sockets_pricefeed
from engine.app.choice_gateway.client_manager import ChoiceSession
from engine.app.choice_gateway.normalize import (
    describe_shape,
    failure_reason,
    is_failure,
    is_keyed_collection,
    unwrap_dict,
    unwrap_list,
)

router = APIRouter()
logger = logging.getLogger("diagnostics")


def _probe(label: str, fetch, include_values: bool) -> Dict[str, Any]:
    """Call one upstream endpoint and report what came back.

    "The server answered" is not "the call worked". Choice reports most
    failures as **HTTP 200 with a failure envelope** — `{"Status": "Fail",
    "Reason": ...}` — which the SDK returns without raising. An earlier version
    of this function marked any non-raising call `ok`, so the capability panel
    reported live quotes as working while every quote request was in fact being
    refused. The envelope is checked here as well as the exception.
    """
    try:
        raw = fetch()
    except Exception as exc:
        return {"endpoint": label, "ok": False, "error": str(exc)[:400]}

    if is_failure(raw):
        return {
            "endpoint": label,
            "ok": False,
            "error": failure_reason(raw) or "Choice reported a failure",
            "envelope_keys": sorted(raw.keys())[:40] if isinstance(raw, dict) else None,
        }

    records = unwrap_list(raw)
    merged = unwrap_dict(raw)

    # `merged` is the field record for most endpoints, but for Holdings it is
    # the ISIN-keyed map of positions — and listing those keys published the
    # account's entire holdings list, with values switched off, in a report the
    # response itself invited the reader to share. Report the fields of one
    # record instead; `record_fields` already covers the useful half.
    if is_keyed_collection(merged):
        flat_fields = [f"<{len(merged)} entries, keys withheld>"]
    else:
        flat_fields = sorted(merged.keys())[:60] if merged else None

    result: Dict[str, Any] = {
        "endpoint": label,
        "ok": True,
        "envelope_keys": sorted(raw.keys())[:40] if isinstance(raw, dict) else None,
        "record_count": len(records),
        "record_fields": sorted(records[0].keys())[:60] if records else None,
        "flat_fields": flat_fields,
        "shape": describe_shape(raw),
    }
    if include_values:
        result["raw"] = raw
    return result


@router.get("/choice")
def choice_diagnostics(
    include_values: bool = Query(
        False,
        description="Include actual values, not just field names. Your own "
                    "account data - only enable when you intend to share it.",
    ),
    current_user: User = Depends(get_current_user),
    session: ChoiceSession = Depends(get_choice_session),
) -> Dict[str, Any]:
    """What Choice returns for this session, and what we made of it.

    Compare ``upstream.record_fields`` against ``normalized`` below: fields
    Choice sends that we do not read are exactly what a mapping fix needs.
    """
    client = None if session.is_demo else session.require_client()

    probes = []
    if client is not None:
        probes = [
            _probe("FundsViewNew", client.funds.get_funds_view_new, include_values),
            _probe("FundsView", client.funds.get_funds_view, include_values),
            _probe("Holdings", client.portfolio.get_holdings, include_values),
            _probe("NetPosition", client.portfolio.get_net_position, include_values),
            _probe("MarketStatus", client.market.get_market_status, include_values),
            _probe(
                "MultipleTouchline",
                lambda: client.market.get_multiple_touchline("1_2885,1_1594"),
                include_values,
            ),
            _probe("OrderBook", client.orders.get_order_book_v2, include_values),
            _probe("TradeBook", client.orders.get_trade_book, include_values),
            # Endpoints we do not use yet. Probed so "what can we build?" is
            # answered before the work starts rather than after.
            _probe("UserProfile", client.market.get_user_profile, include_values),
            _probe("DISStatus", client.portfolio.get_dis_status, include_values),
            # Historical bars are the decisive probe for whether a paper
            # strategy runner is possible on this account. The runner can be
            # driven by bars (`LiveStrategyRunner.on_bar`), so if this works
            # the runner does not need the live tick feed at all.
            _probe(
                "HistoricalData",
                # "D", not "1d": Choice's chart endpoint takes TradingView
                # intervals. Sending our own vocabulary made a parameter fault
                # indistinguishable from a missing entitlement.
                lambda: client.historical.get_historical_data(
                    segment_id=1, token=2885,
                    from_date=(date.today() - timedelta(days=10)).isoformat(),
                    to_date=date.today().isoformat(),
                    resolution="D",
                ),
                include_values,
            ),
        ]

    # What this account can actually do, stated rather than inferred. These
    # three answers decide which features are available and which are blocked.
    def _ok(endpoint: str) -> bool:
        return any(p["endpoint"] == endpoint and p.get("ok") for p in probes)

    # The broadcast price feed is a different service from the REST endpoints
    # above: its own host, its own protocol, and plausibly its own entitlement.
    # Choice hands out the address in the logon response, so an address here is
    # the evidence that this account is provisioned for it — checkable without
    # opening a socket or placing anything.
    feed = sockets_pricefeed.feed_endpoint(session) if client is not None else None

    capabilities = {
        "account_data": _ok("Holdings") or _ok("FundsViewNew") or _ok("FundsView"),
        "live_quotes": _ok("MultipleTouchline"),
        "historical_bars": _ok("HistoricalData"),
        "price_feed_provisioned": bool(feed and feed["source"] == "logon_response"),
        "user_profile": _ok("UserProfile"),
        "edis": _ok("DISStatus"),
    }
    capabilities["paper_runner_possible"] = (
        capabilities["live_quotes"]
        or capabilities["historical_bars"]
        or capabilities["price_feed_provisioned"]
    )
    capabilities["note"] = (
        "A paper strategy runner needs prices. Live quotes give tick-level "
        "decisions; historical bars give bar-level ones, which the runner also "
        "supports; the broadcast feed gives ticks over a socket instead of "
        "REST. Any one is enough."
    )
    capabilities["price_feed_note"] = (
        "The logon response carried a feed address, so this account is "
        "provisioned for the broadcast feed even if MultipleTouchline is not."
        if capabilities["price_feed_provisioned"] else
        "The logon response carried no OdinBcastIP/OdinBcastPort. The feed "
        "would fall back to the vendor default host, which is a guess rather "
        "than an entitlement — treat the feed as unavailable until Choice "
        "confirms otherwise."
    )

    normalized: Dict[str, Any] = {}
    for label, call in (
        ("funds", lambda: funds_gateway.get_funds(session)),
        ("holdings", lambda: portfolio_gateway.get_holdings(session)),
        ("positions", lambda: portfolio_gateway.get_positions(session)),
        ("quotes", lambda: market_gateway.get_multiple_touchline(
            session, "1_2885,1_1594")),
    ):
        try:
            result = call()
            # This block used to return values whatever `include_values` said,
            # while the response's own hint invited the reader to share the file
            # "with include_values off". Following that advice disclosed every
            # holding and the account balance. The probes above have always
            # honoured the flag; this one now does too.
            normalized[label] = result if include_values else describe_shape(result)
        except Exception as exc:
            normalized[label] = {"error": str(exc)[:400]}

    return {
        "session": {
            "mode": session.mode.value,
            "vendor_id": session.vendor_id,
            "has_session_id": bool(session.session_id),
            "has_access_token": bool(session.access_token),
            # The session's server, not the install default: diagnostics
            # exist to answer "what is actually happening", not "what is set".
            "environment": session.environment,
            "base_url": session.base_url,
            "feed_ip": getattr(client, "bcast_ip", None) if client else None,
            "feed_port": getattr(client, "bcast_port", None) if client else None,
        },
        "capabilities": capabilities,
        "upstream": probes,
        "normalized": normalized,
        "hint": "Fields listed under upstream.record_fields that do not appear "
                "in the normalized output are unmapped. With include_values "
                "off this response carries field names and types only - no "
                "balances, no holdings - and is safe to share. With it on it "
                "contains your account data; treat it as private.",
        "includes_values": include_values,
    }
