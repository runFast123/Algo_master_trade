"""Order submission: validate, record, submit, record the outcome.

Every attempt is persisted before the broker call and updated after it, so the
order book reflects what was actually attempted even when the submission
failed. A failure raises, and the caller turns that into a non-2xx response;
nothing here returns a success-shaped payload for a rejected order.
"""

import logging
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from app.models.order import Order
from app.models.user import User
from app.repositories.audit_repo import audit_repo
from app.schemas.order import OrderRequest
from engine.app.choice_gateway import orders as orders_gateway
from engine.app.choice_gateway.client_manager import ChoiceSession
from engine.app.choice_gateway.errors import ChoiceGatewayError

logger = logging.getLogger("orders")


def refresh_realized_pnl(session: ChoiceSession) -> None:
    """Pull today's realised P&L from Choice so the daily loss cap is current.

    The cap is enforced against ``risk_manager._realized``, and the only writer
    of that ledger was ``funds.get_funds`` — reachable only from
    ``GET /portfolio/funds``, which only the dashboard calls. So the circuit
    breaker advertised on the Health panel and by ``/orders/limits`` was
    updated by a *display poll*: someone trading through the API, or with the
    dashboard closed, could lose any amount without it ever tripping.

    Best effort on purpose. This runs after the order has already been placed
    and recorded, so a funds call that fails must not turn a successful order
    into an error response — the next call will catch up.
    """
    if not session.mode.sends_real_orders:
        return
    try:
        from engine.app.choice_gateway import funds as funds_gateway

        funds_gateway.get_funds(session)
    except Exception as exc:
        logger.warning(
            "Could not refresh realised P&L for %s; the daily loss cap may be "
            "stale until the next funds call: %s", session.owner_key, exc,
        )

# The API and UI express limit prices in paisa (integer), which avoids float
# rounding on the wire. The Choice SDK forwards `Price` verbatim to
# api/OpenAPI/V2/NewOrder.
#
# IMPORTANT: whether that field is rupees or paisa is not covered by the
# supplied PDF specifications - the order schema lives in the live API docs at
# finx.choiceindia.com/api/OpenAPI/Info. Confirm it in UAT before placing a
# limit order; if Choice expects paisa, set PRICE_UNIT_DIVISOR to 1.
PRICE_UNIT_DIVISOR = 100.0


def paisa_to_broker_price(price_in_paisa: int) -> float:
    return round(price_in_paisa / PRICE_UNIT_DIVISOR, 2)


class OrderService:
    @staticmethod
    def place_order(
        db: Session,
        user: User,
        session: ChoiceSession,
        req: OrderRequest,
        strategy_run_id: Optional[str] = None,
        source: str = "MANUAL",
    ) -> Dict[str, Any]:
        broker_price = paisa_to_broker_price(req.price_in_paisa)

        record = Order(
            strategy_run_id=strategy_run_id,
            tenant_id=user.tenant_id,
            user_id=user.id,
            symbol=req.symbol or str(req.token),
            segment_id=req.segment_id,
            token=str(req.token),
            side=req.side.upper(),
            order_type=req.order_type.upper(),
            product_type=req.product_type.upper(),
            quantity=req.quantity,
            price_in_paisa=req.price_in_paisa,
            status="SUBMITTED",
            execution_mode=session.mode.value,
            source=source,
        )
        db.add(record)
        db.commit()
        db.refresh(record)

        try:
            result = orders_gateway.place_order(
                session=session,
                segment_id=req.segment_id,
                token=int(req.token),
                order_type=req.order_type,
                side=req.side,
                quantity=req.quantity,
                price=broker_price,
                trigger_price=paisa_to_broker_price(req.trigger_price_in_paisa),
                validity=req.validity,
                product_type=req.product_type,
                disclosed_qty=req.disclosed_quantity,
                symbol=req.symbol or "",
            )
        except ChoiceGatewayError as exc:
            record.status = "REJECTED"
            record.failure_reason = f"{exc.message} {exc.details}".strip()
            db.commit()
            audit_repo.log(
                db, actor_id=user.id, tenant_id=user.tenant_id,
                action="ORDER_REJECTED", entity_type="order", entity_id=record.id,
                details={
                    "symbol": record.symbol, "side": record.side,
                    "quantity": record.quantity, "reason": exc.message,
                    "mode": record.execution_mode,
                },
            )
            logger.warning(
                "Order %s rejected for user %s: %s", record.id, user.id, exc.message
            )
            raise

        # PAPER and DEMO fills are recorded as SIMULATED so the order book can
        # never present a locally filled order as one the exchange accepted.
        record.status = (
            "SIMULATED" if result.get("mode") in ("DEMO", "PAPER") else "ACCEPTED"
        )
        record.client_order_no = result.get("order_id") or None
        record.execution_mode = result.get("mode", record.execution_mode)

        # Record the price it actually filled at, not the price submitted. A
        # market order is submitted at 0 and fills at the traded price, so
        # storing the request would report every market order as free.
        fill = (result.get("data") or {}).get("price")
        record.executed_price = float(fill) if fill is not None else broker_price
        db.commit()
        db.refresh(record)

        audit_repo.log(
            db, actor_id=user.id, tenant_id=user.tenant_id,
            action="ORDER_PLACED", entity_type="order", entity_id=record.id,
            details={
                "symbol": record.symbol, "side": record.side,
                "quantity": record.quantity, "price_in_paisa": record.price_in_paisa,
                "mode": record.execution_mode, "broker_order_id": record.client_order_no,
            },
        )

        refresh_realized_pnl(session)

        return {
            "status": "SUCCESS",
            "mode": result.get("mode"),
            "order_id": record.id,
            "broker_order_id": result.get("order_id"),
            "message": result.get("message"),
            "order": record,
            # Present for simulated fills only: the resulting paper position
            # and running P&L, so the caller does not need a second request.
            "position": result.get("position"),
            "paper": result.get("paper"),
        }


order_service = OrderService()
