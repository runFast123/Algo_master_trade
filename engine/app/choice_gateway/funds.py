"""Funds and margin, scoped to a single user's Choice session."""

import logging
import math
from typing import Any, Dict

from engine.app.choice_gateway.client_manager import ChoiceSession
from engine.app.choice_gateway.errors import ChoiceUpstreamError
from engine.app.choice_gateway.normalize import (
    failure_reason,
    is_failure,
    pick_float,
    unwrap_dict,
)

logger = logging.getLogger("choice_gateway")

# Field names confirmed against live FundsViewNew and FundsView responses.
#
#   FundsViewNew -> FundsAvailable, MarginUtilized, LedgerBalance,
#                   MarginAgainstAssets, PledgeValue, RealizedPnL, UnRealizedPnL
#   FundsView    -> MarginAvailable, MarginUsed, CashAvailable, Collateral,
#                   RealisedProfit, UnRealisedProfit
#
# Note the two endpoints disagree on British/American spelling of "realised".
AVAILABLE_KEYS = (
    "FundsAvailable", "MarginAvailable", "AvailableMargin", "CashAvailable",
    "AvailableBalance", "NetAvailable", "AvlMargin", "available_margin",
)
USED_KEYS = (
    "MarginUtilized", "MarginUsed", "UsedMargin", "MarginUtilised",
    "UtilizedMargin", "used_margin",
)
LEDGER_KEYS = ("LedgerBalance", "TodaysBalance", "OpeningBalance", "Deposit")
COLLATERAL_KEYS = (
    "MarginAgainstAssets", "Collateral", "TotalCollateral", "CollateralValue",
    "Collateral_Manual", "total_collateral",
)
PLEDGE_KEYS = ("PledgeValue",)
REALIZED_KEYS = ("RealizedPnL", "RealisedProfit", "RealisedPnL")
UNREALIZED_KEYS = ("UnRealizedPnL", "UnRealisedProfit", "UnRealisedPnL")


def _normalize_funds(data: Dict[str, Any]) -> Dict[str, Any]:
    """Map a Choice funds payload onto our shape.

    Missing fields stay ``None`` so the caller can tell "Choice did not report
    this" apart from "the account holds zero".
    """
    available = pick_float(data, *AVAILABLE_KEYS)
    used = pick_float(data, *USED_KEYS)
    ledger = pick_float(data, *LEDGER_KEYS)
    collateral = pick_float(data, *COLLATERAL_KEYS)
    pledge = pick_float(data, *PLEDGE_KEYS)

    # Securities pledged for margin count towards collateral.
    if collateral is not None and pledge is not None:
        collateral += pledge
    elif collateral is None and pledge is not None:
        collateral = pledge

    # The total limit is what is free plus what is already committed.
    net_limit = None
    if available is not None and used is not None:
        net_limit = available + used
    elif available is not None:
        net_limit = available

    def money(value):
        # Sums of floats carry binary artefacts; currency is 2dp.
        return round(value, 2) if value is not None else None

    return {
        "NetLimit": money(net_limit),
        "AvailableMargin": money(available),
        "used_margin": money(used),
        "total_collateral": money(collateral),
        "ledger_balance": money(ledger),
        "realized_pnl": money(pick_float(data, *REALIZED_KEYS)),
        "unrealized_pnl": money(pick_float(data, *UNREALIZED_KEYS)),
    }


def get_funds(session: ChoiceSession) -> Dict[str, Any]:
    """Return margin for this session. Raises on upstream failure."""
    if session.is_demo:
        return {
            "status": "SUCCESS",
            "mode": "DEMO",
            "data": {
                "NetLimit": session.demo_margin,
                "AvailableMargin": session.demo_margin - session.demo_used_margin,
                "used_margin": session.demo_used_margin,
                "total_collateral": session.demo_margin,
            },
        }

    client = session.require_client()
    last_error = None
    unmapped = None

    for call in ("get_funds_view_new", "get_funds_view"):
        fetch = getattr(client.funds, call, None)
        if fetch is None:
            continue
        try:
            raw = fetch()
        except ChoiceUpstreamError as exc:
            last_error = exc
            logger.warning("Choice %s failed: %s", call, exc.message)
            continue

        if is_failure(raw):
            last_error = ChoiceUpstreamError(
                f"Choice rejected {call}", failure_reason(raw)
            )
            logger.warning("Choice %s: %s", call, failure_reason(raw))
            continue

        record = unwrap_dict(raw)
        normalized = _normalize_funds(record)

        if any(v is not None for v in normalized.values()):
            # Choice's own realised figure is authoritative for a live account,
            # so the daily loss cap tracks the broker rather than a tally this
            # process kept. Set, never add: a refresh must not book twice.
            booked = normalized.get("realized_pnl")
            if booked is not None and session.mode.sends_real_orders:
                from engine.app.strategy_engine.risk_manager import risk_manager
                risk_manager.set_realized_pnl(session.owner_key, booked)

            return {
                "status": "SUCCESS",
                "mode": session.mode.value,
                "data": normalized,
                "source": call,
            }

        # A response arrived but nothing mapped. Keep it: reporting an empty
        # balance would be a guess, and the raw keys are what a fix needs.
        if record:
            unmapped = {"call": call, "keys": sorted(record.keys())[:40]}
            logger.warning(
                "Choice %s returned fields we do not map: %s", call, unmapped["keys"]
            )

    if unmapped:
        raise ChoiceUpstreamError(
            "Choice returned funds data in an unrecognised format. "
            f"Fields received from {unmapped['call']}: {', '.join(unmapped['keys'])}",
            "Report these field names so the mapping can be corrected.",
        )
    if last_error:
        raise last_error
    raise ChoiceUpstreamError("Choice returned no funds data for this account")


# -- moving money ----------------------------------------------------------
#
# These are the only calls in this codebase that move a user's cash rather than
# their positions. Three rules apply to all of them:
#
#   1. A simulated session must never reach them. Paper trading exists so a
#      mistake costs nothing, and a real transfer from a paper session would
#      break that promise entirely.
#   2. The amount is validated here, not only at the API edge. This module is
#      importable from the engine and the backend both.
#   3. The destination is whatever Choice has on record for the account. We do
#      not accept an arbitrary bank account, so a compromised token cannot
#      redirect a withdrawal.

def _require_real_session(session: ChoiceSession, action: str) -> None:
    if session.simulates_orders:
        raise ChoiceUpstreamError(
            f"Cannot {action} from a simulated session. Paper and sandbox "
            "sessions hold no real funds."
        )


# A payout larger than this is a typo, not an instruction. Chosen well above
# any plausible retail transfer and well below the point where a float starts
# losing rupees.
MAX_TRANSFER = 100_000_000.0


def _validate_amount(amount: Any) -> float:
    try:
        value = float(amount)
    except (TypeError, ValueError):
        raise ChoiceUpstreamError(f"Amount must be a number, got {amount!r}")
    # `nan <= 0` is False and `inf <= 0` is False, so both slipped through a
    # bare positivity check. JSON has no NaN or Infinity literal, but Python's
    # parser accepts both, so both could reach a live payout endpoint.
    if not math.isfinite(value):
        raise ChoiceUpstreamError(f"Amount must be a real number, got {amount!r}")
    if value <= 0:
        raise ChoiceUpstreamError("Amount must be greater than zero.")
    if value > MAX_TRANSFER:
        raise ChoiceUpstreamError(
            f"Amount of {value:,.2f} exceeds the per-transfer limit of "
            f"{MAX_TRANSFER:,.2f}. Move it in smaller amounts if this is intended."
        )
    return round(value, 2)


def withdraw(session: ChoiceSession, amount: Any, bank_acc_no: str,
             product_type: int = 0) -> Dict[str, Any]:
    """Request a payout to the account's registered bank account."""
    _require_real_session(session, "withdraw")
    value = _validate_amount(amount)
    if not str(bank_acc_no or "").strip():
        raise ChoiceUpstreamError("A registered bank account is required.")

    client = session.require_client()
    logger.info("Payout requested: user=%s amount=%s", session.owner_key, value)
    try:
        raw = client.funds.process_payout(
            amount=value, bank_acc_no=str(bank_acc_no).strip(),
            product_type=int(product_type),
        )
    except Exception as exc:
        raise ChoiceUpstreamError("Payout request failed", str(exc)) from exc

    if is_failure(raw):
        raise ChoiceUpstreamError("Choice rejected the payout", failure_reason(raw))
    return {"status": "SUCCESS", "mode": session.mode.value,
            "message": f"Payout of Rs {value:.2f} requested",
            "data": raw if isinstance(raw, dict) else {}}


def check_vpa(session: ChoiceSession, user_vpa: str) -> Dict[str, Any]:
    """Verify a UPI VPA before a deposit is attempted against it."""
    _require_real_session(session, "verify a UPI id")
    vpa = str(user_vpa or "").strip()
    if "@" not in vpa:
        raise ChoiceUpstreamError(f"{vpa!r} is not a UPI id.")

    client = session.require_client()
    try:
        raw = client.funds.check_vpa(user_vpa=vpa)
    except Exception as exc:
        raise ChoiceUpstreamError("VPA check failed", str(exc)) from exc
    return {"status": "SUCCESS", "valid": not is_failure(raw),
            "data": raw if isinstance(raw, dict) else {}}


def add_funds(session: ChoiceSession, amount: Any, method: str,
              bank_acc_no: str, segment_id: int = 1, user_vpa: str = "",
              bank_ifsc_code: str = "", return_url: str = "",
              product_type: int = 0) -> Dict[str, Any]:
    """Start a deposit. ``method`` is ``upi``, ``netbanking`` or ``razorpay``.

    Returns whatever Choice replies with — typically a redirect URL or a UPI
    collect request. Nothing is debited here; the user completes the payment
    at their bank.
    """
    _require_real_session(session, "add funds")
    value = _validate_amount(amount)
    how = str(method or "").strip().lower()
    account = str(bank_acc_no or "").strip()
    if not account:
        raise ChoiceUpstreamError("A registered bank account is required.")

    client = session.require_client()
    try:
        if how == "upi":
            vpa = str(user_vpa or "").strip()
            if "@" not in vpa:
                raise ChoiceUpstreamError(f"{vpa!r} is not a UPI id.")
            raw = client.funds.payment_via_hdfc_upi(
                amount=value, bank_acc_no=account, user_vpa=vpa,
                segment_id=int(segment_id), product_type=int(product_type))
        elif how == "netbanking":
            if not str(bank_ifsc_code or "").strip():
                raise ChoiceUpstreamError("An IFSC code is required for net banking.")
            raw = client.funds.payment_via_netbanking(
                amount=value, bank_acc_no=account,
                bank_ifsc_code=str(bank_ifsc_code).strip(),
                return_url=str(return_url or ""), segment_id=int(segment_id),
                product_type=int(product_type))
        elif how == "razorpay":
            raw = client.funds.payment_via_razorpay(
                amount=value, bank_acc_no=account,
                bank_ifsc_code=str(bank_ifsc_code or "").strip(),
                upi_id=str(user_vpa or "").strip(), segment_id=int(segment_id),
                product_type=int(product_type))
        else:
            raise ChoiceUpstreamError(
                f"Unknown payment method {method!r}. Use upi, netbanking or razorpay.")
    except ChoiceUpstreamError:
        raise
    except Exception as exc:
        raise ChoiceUpstreamError("Deposit request failed", str(exc)) from exc

    if is_failure(raw):
        raise ChoiceUpstreamError("Choice rejected the deposit", failure_reason(raw))
    return {"status": "SUCCESS", "mode": session.mode.value, "method": how,
            "message": f"Deposit of Rs {value:.2f} started via {how}",
            "data": raw if isinstance(raw, dict) else {}}
