"""Pre-trade risk checks.

Applied to every order regardless of whether it came from the UI, a strategy
run, or the API. Failures raise :class:`OrderRejected` so a breach can never be
mistaken for an accepted order.
"""

import logging
import threading
from datetime import date
from typing import Dict, Optional

from engine.app.choice_gateway.errors import OrderRejected
from engine.app.config import engine_settings

logger = logging.getLogger("risk_manager")


class RiskManager:
    """Enforces per-order notional caps and a per-session daily loss cap."""

    def __init__(
        self,
        max_order_value: Optional[float] = None,
        max_order_quantity: Optional[int] = None,
        max_daily_loss: float = 100000.0,
    ):
        self.max_order_value = (
            max_order_value
            if max_order_value is not None
            else engine_settings.MAX_ORDER_VALUE
        )
        self.max_order_quantity = (
            max_order_quantity
            if max_order_quantity is not None
            else engine_settings.MAX_ORDER_QUANTITY
        )
        self.max_daily_loss = max_daily_loss
        # Two ledgers, because they answer different questions. `_realized` is
        # money actually lost at the broker and is what stops real orders.
        # `_simulated` is what a paper strategy would have lost; it stops
        # further paper orders and nothing else.
        #
        # One shared ledger meant a losing paper strategy — which this app
        # actively encourages people to run — halted the user's real trading
        # for the day, with a message that said "daily loss limit reached" and
        # gave no hint that none of it was real.
        self._realized: Dict[str, float] = {}
        self._simulated: Dict[str, float] = {}
        self._halted: Dict[str, str] = {}
        # Whether that halt stops everything or only real orders.
        self._halt_scope: Dict[str, str] = {}
        self._as_of: date = date.today()
        self._lock = threading.Lock()

    def _roll_day(self) -> None:
        today = date.today()
        if today != self._as_of:
            self._realized.clear()
            self._simulated.clear()
            self._halted.clear()
            self._halt_scope.clear()
            self._as_of = today

    def validate_order(
        self,
        quantity: int,
        price: float,
        side: str = "BUY",
        owner_key: Optional[str] = None,
        simulated: bool = False,
    ) -> None:
        """Raise :class:`OrderRejected` if the order breaches a limit.

        ``simulated`` selects which loss ledger applies. A paper order is
        checked against paper losses and a real order against real ones — the
        alternative, one shared budget, let a losing paper strategy stop
        somebody's real trading for the day.
        """
        if owner_key:
            halted = self.is_halted(owner_key)
            # A kill switch stops everything; a real-loss halt stops real
            # orders. Scoping every halt to real orders made the kill switch a
            # no-op for a paper user, which is worse than one that is too
            # broad — and scoping none of them blocked the one useful thing to
            # do after a bad day.
            with self._lock:
                scope = self._halt_scope.get(owner_key, "all")
            if halted and (scope == "all" or not simulated):
                raise OrderRejected(f"Trading is halted for today: {halted}")

        if quantity <= 0:
            raise OrderRejected(f"Order quantity must be positive, got {quantity}")
        if quantity > self.max_order_quantity:
            raise OrderRejected(
                f"Order quantity {quantity} exceeds the limit of "
                f"{self.max_order_quantity}"
            )

        order_value = quantity * price
        if order_value > self.max_order_value:
            raise OrderRejected(
                f"{side} order value of {order_value:,.2f} exceeds the per-order "
                f"limit of {self.max_order_value:,.2f}"
            )

        if owner_key:
            ledger = self._simulated if simulated else self._realized
            with self._lock:
                self._roll_day()
                loss = ledger.get(owner_key, 0.0)
            if loss <= -abs(self.max_daily_loss):
                if simulated:
                    # No breaker: a paper run hitting its budget is information,
                    # not an emergency, and halting would also stop real orders
                    # through the shared kill switch.
                    raise OrderRejected(
                        f"Paper daily loss limit of {self.max_daily_loss:,.2f} "
                        "reached. Real trading is unaffected."
                    )
                # Trip the breaker rather than refusing this order alone, so a
                # strategy retrying in a loop cannot keep probing the limit.
                self.halt(
                    owner_key,
                    f"Daily loss limit of {self.max_daily_loss:,.2f} reached",
                    scope="real",
                )
                raise OrderRejected(
                    f"Daily loss limit of {self.max_daily_loss:,.2f} reached; "
                    "no further orders will be sent today."
                )

    def record_realized_pnl(self, owner_key: str, pnl: float,
                            simulated: bool = False) -> None:
        """Add to the day's realised figure, in the ledger it belongs to."""
        with self._lock:
            self._roll_day()
            ledger = self._simulated if simulated else self._realized
            ledger[owner_key] = ledger.get(owner_key, 0.0) + pnl

    def set_realized_pnl(self, owner_key: str, pnl: float) -> None:
        """Replace the day's figure with the broker's own.

        Live realised P&L is reported by Choice and is authoritative; adding to
        it on every funds refresh would count the same profit repeatedly.
        """
        with self._lock:
            self._roll_day()
            self._realized[owner_key] = float(pnl)

    def realized_pnl(self, owner_key: str, simulated: bool = False) -> float:
        with self._lock:
            self._roll_day()
            ledger = self._simulated if simulated else self._realized
            return ledger.get(owner_key, 0.0)

    def halt(self, owner_key: str, reason: str = "Halted manually",
             scope: str = "all") -> None:
        """Stop accepting orders for this owner until the day rolls over.

        ``scope`` says what the halt is for, because two different events call
        this and they mean different things:

        * ``"all"`` — somebody pressed the kill switch. They mean *stop what I
          am doing*, and a kill switch that leaves a paper strategy running is
          a kill switch that did nothing.
        * ``"real"`` — the daily loss cap tripped on real money. That protects
          funds; simulated orders risk none, and blocking them would prevent
          working out what went wrong.

        Deliberately not clearable from the API: a limit you can switch off in
        a panic is not a limit.
        """
        with self._lock:
            self._roll_day()
            self._halted[owner_key] = reason
            self._halt_scope[owner_key] = "real" if scope == "real" else "all"
        logger.warning("Trading halted for %s (%s): %s", owner_key, scope, reason)

    def is_halted(self, owner_key: str) -> Optional[str]:
        with self._lock:
            self._roll_day()
            return self._halted.get(owner_key)

    def budget(self, owner_key: str) -> Dict[str, object]:
        """Limits and remaining headroom, for display.

        A limit the trader can see is a safety feature; one they discover by
        hitting it reads as a bug.
        """
        with self._lock:
            self._roll_day()
            realized = self._realized.get(owner_key, 0.0)
            simulated = self._simulated.get(owner_key, 0.0)
            halted = self._halted.get(owner_key)

        cap = abs(self.max_daily_loss)
        loss = abs(min(realized, 0.0))
        return {
            "max_order_value": self.max_order_value,
            "max_order_quantity": self.max_order_quantity,
            "max_daily_loss": cap,
            "realized_pnl_today": round(realized, 2),
            "daily_loss_used": round(loss, 2),
            "daily_loss_remaining": round(max(0.0, cap - loss), 2),
            "daily_loss_used_pct": round((loss / cap * 100), 2) if cap else 0.0,
            # Reported separately so the number on screen is the one that
            # governs real orders, with the paper figure beside it rather than
            # folded in. A single total would be neither.
            "simulated_pnl_today": round(simulated, 2),
            "simulated_loss_used": round(abs(min(simulated, 0.0)), 2),
            "halted": halted is not None,
            "halt_reason": halted,
            "as_of": self._as_of.isoformat(),
        }


risk_manager = RiskManager()
