import csv
import io
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import get_choice_session, get_current_user
from app.models.order import Order
from app.models.user import User
from app.repositories import audit_repo
from app.schemas.order import (
    OrderModifyRequest,
    OrderPlacedResponse,
    OrderPreviewRequest,
    OrderRequest,
    OrderResponse,
)
from app.services.order_service import order_service
from engine.app.costs import preview_order
from engine.app.choice_gateway import orders as orders_gateway
from engine.app.choice_gateway.client_manager import ChoiceSession
from engine.app.strategy_engine.risk_manager import risk_manager

router = APIRouter()


@router.post("/", response_model=OrderPlacedResponse)
def place_order(
    req: OrderRequest,
    current_user: User = Depends(get_current_user),
    session: ChoiceSession = Depends(get_choice_session),
    db: Session = Depends(get_db),
):
    """Submit an order.

    A 2xx response means Choice accepted the order (or that a sandbox session
    simulated it). A rejection returns 4xx/5xx with the reason.
    """
    return order_service.place_order(db, current_user, session, req)


def _local_order(db: Session, user: User, reference) -> Optional[Order]:
    """This platform's record of one broker order, or None.

    Matched on any identifier the broker might have handed back, because an
    order can be referenced by its client, exchange or gateway number. Scoped
    to the tenant so one account can never amend another's record.

    None is a real answer: an order placed from the Choice website has no row
    here, and acting on it should still work.
    """
    key = str(reference).strip()
    if not key:
        return None
    return (
        db.query(Order)
        .filter(Order.tenant_id == user.tenant_id)
        .filter((Order.client_order_no == key) | (Order.exchange_order_no == key))
        .order_by(Order.created_at.desc())
        .first()
    )


@router.post("/modify")
def modify_order(
    req: OrderModifyRequest,
    current_user: User = Depends(get_current_user),
    session: ChoiceSession = Depends(get_choice_session),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """Amend a working order in place.

    The alternative is cancel-and-replace, which surrenders the order's place
    in the exchange queue. Amending is validated as strictly as placing,
    because raising a quantity or a price raises exposure the same way.
    """
    result = orders_gateway.modify_order(
        session,
        client_order_no=req.client_order_no,
        exchange_order_no=req.exchange_order_no,
        gateway_order_no=req.gateway_order_no,
        segment_id=req.segment_id,
        token=int(req.token),
        order_type=req.order_type,
        side=req.side,
        quantity=req.quantity,
        price=req.price_in_paisa / 100.0,
        trigger_price=req.trigger_price_in_paisa / 100.0,
        validity=req.validity,
        product_type=req.product_type,
        disclosed_qty=req.disclosed_quantity,
    )
    # The local book is what the interface, the CSV export and reconciliation
    # all read. Leaving it at the original values meant an order amended from
    # 10 @ 2400 to 200 @ 2500 still reported 10 @ 2400 forever, and
    # reconciliation could not see the divergence either.
    record = _local_order(db, current_user, req.client_order_no)
    if record is not None:
        record.quantity = req.quantity
        record.price_in_paisa = req.price_in_paisa
        record.order_type = req.order_type
        record.status = "MODIFIED"
        db.commit()

    audit_repo.log(
        db, actor_id=current_user.id, tenant_id=current_user.tenant_id,
        action="ORDER_MODIFIED", entity_type="order",
        entity_id=record.id if record is not None else None,
        details={"client_order_no": req.client_order_no, "quantity": req.quantity,
                 "price_in_paisa": req.price_in_paisa, "symbol": req.symbol,
                 "local_record_found": record is not None},
    )
    return result


@router.post("/{client_order_no}/cancel")
def cancel_one_order(
    client_order_no: int,
    current_user: User = Depends(get_current_user),
    session: ChoiceSession = Depends(get_choice_session),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """Cancel a single working order.

    The kill switch cancels everything; this cancels one. Both go through the
    same echo-the-book-back path, so a field that works for one works for both.
    """
    result = orders_gateway.cancel_one(session, client_order_no)

    record = _local_order(db, current_user, client_order_no)
    if record is not None:
        record.status = "CANCELLED"
        db.commit()

    audit_repo.log(
        db, actor_id=current_user.id, tenant_id=current_user.tenant_id,
        action="ORDER_CANCELLED", entity_type="order",
        entity_id=record.id if record is not None else None,
        details={"client_order_no": client_order_no,
                 "local_record_found": record is not None},
    )
    return result


@router.post("/preview")
def preview(
    req: OrderPreviewRequest,
    session: ChoiceSession = Depends(get_choice_session),
) -> Dict[str, Any]:
    """What this order will cost, before it is sent.

    Nothing is submitted and no rate-limit token is spent. The same cost model
    backtests use is quoted here, so a strategy's assumptions and a manual
    order's charges cannot disagree.
    """
    # Paisa to rupees is always /100. Deliberately not PRICE_UNIT_DIVISOR:
    # that constant describes the unit Choice wants on the wire, which is a
    # separate and still-unconfirmed question (OPEN-2). Conflating a display
    # conversion with a protocol one is what inflated the portfolio 100x.
    price = (req.price_in_paisa / 100.0
             if req.price_in_paisa else float(req.price or 0.0))
    quote = preview_order(req.side, req.quantity, price,
                          product_type=req.product_type)

    budget = risk_manager.budget(session.owner_key)
    value = quote["turnover"]
    quote["limits"] = {
        "max_order_value": budget["max_order_value"],
        "within_order_value": value <= budget["max_order_value"],
        "max_order_quantity": budget["max_order_quantity"],
        "within_order_quantity": req.quantity <= budget["max_order_quantity"],
        "halted": budget["halted"],
        "halt_reason": budget["halt_reason"],
    }
    quote["mode"] = session.mode.value
    quote["sends_real_orders"] = session.mode.sends_real_orders
    return quote


@router.get("/limits")
def limits(session: ChoiceSession = Depends(get_choice_session)) -> Dict[str, Any]:
    """The risk budget and how much of it is left today."""
    return {"status": "SUCCESS", "mode": session.mode.value,
            "data": risk_manager.budget(session.owner_key)}


@router.post("/halt")
def halt(
    current_user: User = Depends(get_current_user),
    session: ChoiceSession = Depends(get_choice_session),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """Kill switch: cancel every working order and stop accepting new ones.

    Cancellation is attempted for each order independently, so one the broker
    refuses cannot block the rest. The halt is applied even if some
    cancellations fail — stopping new orders is the part that must not depend
    on the broker answering.
    """
    risk_manager.halt(session.owner_key, "Kill switch used")

    result: Dict[str, Any] = {"cancelled": 0, "failed": 0, "failures": []}
    error = None
    try:
        result = orders_gateway.cancel_all_open(session)
    except Exception as exc:                    # the halt still stands
        error = str(exc)

    audit_repo.log(
        db, actor_id=current_user.id, tenant_id=current_user.tenant_id,
        action="ORDERS_HALTED", entity_type="order",
        details={
            "cancelled": result.get("cancelled", 0),
            "failed": result.get("failed", 0),
            "cancel_error": error,
        },
    )

    return {
        "status": "SUCCESS",
        "halted": True,
        "cancel_error": error,
        **{k: v for k, v in result.items() if k not in ("status", "mode")},
        "budget": risk_manager.budget(session.owner_key),
    }


@router.get("/reconcile")
def reconcile(
    current_user: User = Depends(get_current_user),
    session: ChoiceSession = Depends(get_choice_session),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """Compare this platform's order record against the broker's.

    The interface shows the local book, so a divergence means the interface is
    wrong — worth surfacing rather than discovering during a reconciliation at
    the end of the month.
    """
    local = [
        {"client_order_no": o.client_order_no, "symbol": o.symbol, "status": o.status}
        for o in db.query(Order)
        .filter(Order.tenant_id == current_user.tenant_id)
        .order_by(Order.created_at.desc())
        .limit(500)
        .all()
    ]
    return orders_gateway.reconcile(session, local)


@router.get("/", response_model=List[OrderResponse])
def list_orders(
    status_filter: Optional[str] = Query(None, alias="status"),
    limit: int = Query(200, ge=1, le=1000),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Orders this platform submitted, newest first, scoped to the tenant."""
    query = db.query(Order).filter(Order.tenant_id == current_user.tenant_id)
    if status_filter:
        query = query.filter(Order.status == status_filter.upper())
    return query.order_by(Order.created_at.desc()).limit(limit).all()


ORDER_CSV_COLUMNS = [
    "created_at", "client_order_no", "exchange_order_no", "symbol", "segment_id",
    "token", "side", "order_type", "product_type", "quantity", "price_in_paisa",
    "executed_price", "status", "execution_mode", "source", "failure_reason",
]


@router.get("/export.csv")
def export_orders(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """The order log as CSV, for accounting or a spreadsheet."""
    rows = (
        db.query(Order)
        .filter(Order.tenant_id == current_user.tenant_id)
        .order_by(Order.created_at.desc())
        .all()
    )

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(ORDER_CSV_COLUMNS)
    for order in rows:
        writer.writerow([getattr(order, column, "") for column in ORDER_CSV_COLUMNS])
    buffer.seek(0)

    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="orders.csv"'},
    )


@router.get("/book")
def get_order_book(
    session: ChoiceSession = Depends(get_choice_session),
) -> Dict[str, Any]:
    """The broker-side order book for this user's Choice session."""
    return orders_gateway.get_order_book(session)


@router.get("/trades")
def get_trade_book(
    session: ChoiceSession = Depends(get_choice_session),
) -> Dict[str, Any]:
    return orders_gateway.get_trade_book(session)
