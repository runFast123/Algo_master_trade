import csv
import io
from typing import Any, Dict

from sqlalchemy.orm import Session

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.database import get_db
from app.dependencies import get_choice_session, get_current_user
from app.models.user import User
from app.repositories.audit_repo import audit_repo
from engine.app.choice_gateway import analytics
from engine.app.choice_gateway import funds as funds_gateway
from engine.app.choice_gateway import portfolio as portfolio_gateway
from engine.app.choice_gateway.client_manager import ChoiceSession

router = APIRouter()


@router.get("/holdings")
def get_holdings(session: ChoiceSession = Depends(get_choice_session)) -> Dict[str, Any]:
    """Holdings for this user's own Choice session, with derived analytics.

    Day change, value, cost and return are computed here rather than in the
    browser so the exports, the API and the interface cannot disagree about
    the same numbers.
    """
    result = portfolio_gateway.get_holdings(session)
    rows = analytics.enrich(result.get("data") or [])
    result["data"] = rows
    result["summary"] = analytics.summarise(rows)
    return result


@router.get("/positions")
def get_positions(session: ChoiceSession = Depends(get_choice_session)) -> Dict[str, Any]:
    result = portfolio_gateway.get_positions(session)
    result["data"] = analytics.enrich(result.get("data") or [])
    return result


@router.get("/funds")
def get_funds(session: ChoiceSession = Depends(get_choice_session)) -> Dict[str, Any]:
    """Margin for this user's own Choice session.

    A field Choice did not report comes back as null rather than a placeholder,
    so a zero balance is never displayed as available margin.
    """
    return funds_gateway.get_funds(session)


HOLDINGS_CSV_COLUMNS = [
    "symbol", "isin", "token", "segment_id", "quantity", "sellable_quantity",
    "average_price", "current_price", "close_price", "value", "cost", "pnl",
    "return_pct", "day_change", "day_change_pct", "day_pnl",
]


@router.get("/holdings/export.csv")
def export_holdings(session: ChoiceSession = Depends(get_choice_session)):
    """Holdings as CSV, for accounting or a spreadsheet."""
    rows = analytics.enrich(portfolio_gateway.get_holdings(session).get("data") or [])

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(HOLDINGS_CSV_COLUMNS)
    for row in rows:
        writer.writerow([row.get(column, "") for column in HOLDINGS_CSV_COLUMNS])
    buffer.seek(0)

    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="holdings.csv"'},
    )


# -- position actions ------------------------------------------------------

class ConvertPositionRequest(BaseModel):
    """Move an open position between product types."""

    segment_id: int = Field(ge=1)
    token: int = Field(ge=1)
    client_order_no: int = Field(ge=0)
    side: str = Field(description="BUY or SELL, matching the position")
    quantity: int = Field(ge=1)
    product_type: str = Field(description="Target product: CNC, MIS or NRML")
    source_product_type: str = Field(description="The position's current product")
    is_edis_req: bool = True


@router.post("/convert")
def convert_position(
    req: ConvertPositionRequest,
    current_user: User = Depends(get_current_user),
    session: ChoiceSession = Depends(get_choice_session),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """Convert an open position, e.g. MIS intraday to CNC delivery.

    Places no order and moves no money, but it decides whether the position
    survives the close — and after the broker's cutoff it cannot be undone.
    """
    result = portfolio_gateway.convert_position(
        session,
        segment_id=req.segment_id,
        token=req.token,
        client_order_no=req.client_order_no,
        side=req.side,
        quantity=req.quantity,
        product_type=req.product_type,
        source_product_type=req.source_product_type,
        is_edis_req=req.is_edis_req,
    )
    audit_repo.log(
        db, actor_id=current_user.id, tenant_id=current_user.tenant_id,
        action="POSITION_CONVERTED", entity_type="position",
        details={"token": req.token, "segment_id": req.segment_id,
                 "quantity": req.quantity, "side": req.side,
                 "from": req.source_product_type, "to": req.product_type},
    )
    return result


@router.get("/edis")
def edis_status(
    session: ChoiceSession = Depends(get_choice_session),
) -> Dict[str, Any]:
    """eDIS authorisation status. Selling delivery holdings requires it."""
    return portfolio_gateway.get_dis_status(session)


# -- funds movement --------------------------------------------------------

class WithdrawRequest(BaseModel):
    amount: float = Field(gt=0)
    bank_acc_no: str = Field(min_length=1, max_length=32)
    # Money leaving the account is not undone by a second click, so the
    # interface has to say the amount out loud and be answered — the same
    # shape as the live-trading switch.
    confirm: bool = False


@router.post("/funds/withdraw")
def withdraw_funds(
    req: WithdrawRequest,
    current_user: User = Depends(get_current_user),
    session: ChoiceSession = Depends(get_choice_session),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """Request a payout to the account's registered bank account."""
    if not req.confirm:
        raise HTTPException(
            status_code=400,
            detail=f"Withdrawing Rs {req.amount:,.2f} moves real money out of "
                   "your trading account. Confirm to proceed.",
        )
    # Logged either way. A disputed payout needs a record naming the actor, the
    # amount and the destination — and a refused one is worth keeping too,
    # since a run of refusals is what a compromised token looks like.
    try:
        result = funds_gateway.withdraw(session, req.amount, req.bank_acc_no)
    except Exception as exc:
        audit_repo.log(
            db, actor_id=current_user.id, tenant_id=current_user.tenant_id,
            action="FUNDS_WITHDRAWAL_REFUSED", entity_type="funds",
            details={"amount": req.amount, "bank_acc_no": req.bank_acc_no,
                     "reason": str(exc)[:300]},
        )
        raise
    audit_repo.log(
        db, actor_id=current_user.id, tenant_id=current_user.tenant_id,
        action="FUNDS_WITHDRAWN", entity_type="funds",
        details={"amount": req.amount, "bank_acc_no": req.bank_acc_no,
                 "mode": session.mode.value},
    )
    return result


class AddFundsRequest(BaseModel):
    amount: float = Field(gt=0)
    method: str = Field(description="upi, netbanking or razorpay")
    bank_acc_no: str = Field(min_length=1, max_length=32)
    segment_id: int = 1
    user_vpa: str = ""
    bank_ifsc_code: str = ""
    return_url: str = ""


@router.post("/funds/add")
def add_funds(
    req: AddFundsRequest,
    current_user: User = Depends(get_current_user),
    session: ChoiceSession = Depends(get_choice_session),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """Start a deposit. Nothing is debited here — the user pays at their bank."""
    result = funds_gateway.add_funds(
        session, amount=req.amount, method=req.method,
        bank_acc_no=req.bank_acc_no, segment_id=req.segment_id,
        user_vpa=req.user_vpa, bank_ifsc_code=req.bank_ifsc_code,
        return_url=req.return_url,
    )
    audit_repo.log(
        db, actor_id=current_user.id, tenant_id=current_user.tenant_id,
        action="FUNDS_DEPOSIT_STARTED", entity_type="funds",
        details={"amount": req.amount, "method": req.method,
                 "bank_acc_no": req.bank_acc_no, "mode": session.mode.value},
    )
    return result


@router.get("/funds/check-vpa")
def check_vpa(
    vpa: str = Query(..., min_length=3, max_length=64),
    session: ChoiceSession = Depends(get_choice_session),
) -> Dict[str, Any]:
    """Verify a UPI id before a deposit is attempted against it."""
    return funds_gateway.check_vpa(session, vpa)
