"""Live and paper strategy execution.

A runner keeps a rolling bar window per instrument, re-evaluates the strategy's
DSL when a bar closes, and routes the resulting orders. In PAPER mode fills are
simulated locally and no order ever reaches Choice; in LIVE mode orders go
through the same validated, rate-limited path as manual orders.
"""

import logging
import threading
from collections import deque
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Deque, Dict, List, Optional

import pandas as pd

from engine.app import costs
from engine.app.choice_gateway import orders as orders_gateway
from engine.app.strategy_engine.risk_manager import risk_manager
from engine.app.choice_gateway.client_manager import ChoiceSession
from engine.app.choice_gateway.errors import ChoiceGatewayError, OrderRejected
from engine.app.strategy_engine.dsl import dsl_engine

logger = logging.getLogger("strategy_runner")

MAX_WINDOW_BARS = 500


class RunMode(str, Enum):
    PAPER = "PAPER"
    LIVE = "LIVE"


class RunState(str, Enum):
    IDLE = "IDLE"
    RUNNING = "RUNNING"
    STOPPED = "STOPPED"
    FAILED = "FAILED"


class LiveStrategyRunner:
    """Executes one strategy against a live or simulated tick stream."""

    def __init__(
        self,
        run_id: str,
        session: ChoiceSession,
        dsl_def: Dict[str, Any],
        params: Dict[str, Any],
        mode: RunMode = RunMode.PAPER,
        on_event: Optional[Callable[[Dict[str, Any]], None]] = None,
    ):
        dsl_engine.validate(dsl_def)

        self.run_id = run_id
        self.session = session
        self.dsl_def = dsl_def
        self.params = params
        self.mode = RunMode(mode)
        self.state = RunState.IDLE
        self.on_event = on_event

        self.segment_id = int(params.get("segment_id", 1))
        self.token = str(params.get("token") or "")
        self.symbol = str(params.get("symbol") or self.token)
        self.order_qty = int((dsl_def.get("actions") or {}).get("buy_qty", 1))

        self._bars: Deque[Dict[str, Any]] = deque(maxlen=MAX_WINDOW_BARS)
        self._lock = threading.RLock()
        self.position_qty = 0
        self.entry_price = 0.0
        self.realized_pnl = 0.0
        self.total_charges = 0.0
        # The same cost model the backtest uses, chosen for the product the run
        # trades under, so the two cannot quote different schedules.
        self._costs = costs.for_product(str(params.get("product_type") or "CNC"))
        self.orders: List[Dict[str, Any]] = []
        self.errors: List[str] = []

    # -- lifecycle ---------------------------------------------------------

    def seed(self, history: pd.DataFrame) -> None:
        """Prime the indicator window with recent bars before going live."""
        with self._lock:
            for record in history.tail(MAX_WINDOW_BARS).to_dict("records"):
                self._bars.append(record)

    def start(self) -> None:
        if self.mode is RunMode.LIVE and not self.session.is_live:
            raise ChoiceGatewayError(
                "A LIVE run needs a session connected in live mode. This session "
                f"is {self.session.mode.value}; reconnect in live mode, or start "
                "this run in PAPER mode instead."
            )
        self.state = RunState.RUNNING
        logger.info("Run %s started in %s mode for %s",
                    self.run_id, self.mode.value, self.symbol)
        self._emit({"type": "STARTED", "mode": self.mode.value})

    def stop(self) -> None:
        self.state = RunState.STOPPED
        logger.info("Run %s stopped", self.run_id)
        self._emit({"type": "STOPPED", "realized_pnl": self.realized_pnl})

    def _emit(self, event: Dict[str, Any]) -> None:
        event.setdefault("run_id", self.run_id)
        event.setdefault("at", datetime.now(timezone.utc).isoformat())
        if self.on_event:
            try:
                self.on_event(event)
            except Exception:  # a listener must not break execution
                logger.exception("Run %s event listener failed", self.run_id)

    # -- market events -----------------------------------------------------

    def on_bar(self, bar: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Handle a closed bar: evaluate the strategy and act on the signal."""
        if self.state is not RunState.RUNNING:
            return None

        with self._lock:
            self._bars.append(bar)
            if len(self._bars) < 2:
                return None

            frame = pd.DataFrame(list(self._bars))
            try:
                frame = dsl_engine.calculate_indicators(
                    frame, self.dsl_def.get("indicators", {})
                )
            except Exception as exc:
                self._fail(f"Indicator calculation failed: {exc}")
                return None

            row = frame.iloc[-1]
            prev_row = frame.iloc[-2]

            try:
                if self.position_qty == 0:
                    if dsl_engine.evaluate_conditions_all(
                        row, self.dsl_def.get("entry_conditions", []), prev_row
                    ):
                        return self._submit("BUY", float(row["close"]))
                elif dsl_engine.evaluate_conditions_all(
                    row, self.dsl_def.get("exit_conditions", []), prev_row
                ):
                    return self._submit("SELL", float(row["close"]))
            except Exception as exc:
                self._fail(f"Condition evaluation failed: {exc}")
        return None

    def on_tick(self, tick: Dict[str, Any]) -> None:
        """Handle a touchline tick; used for stop-loss and target monitoring."""
        if self.state is not RunState.RUNNING or self.position_qty == 0:
            return

        price = tick.get("ltp") or tick.get("price")
        if price is None:
            return
        price = float(price)
        actions = self.dsl_def.get("actions") or {}
        target_pct = actions.get("target_pct")
        stop_pct = actions.get("stop_loss_pct")

        with self._lock:
            if target_pct and price >= self.entry_price * (1 + float(target_pct) / 100):
                self._submit("SELL", price, reason="TARGET")
            elif stop_pct and price <= self.entry_price * (1 - float(stop_pct) / 100):
                self._submit("SELL", price, reason="STOP_LOSS")

    # -- order routing -----------------------------------------------------

    def _submit(
        self, side: str, price: float, reason: str = "SIGNAL"
    ) -> Optional[Dict[str, Any]]:
        qty = self.order_qty if side == "BUY" else self.position_qty
        if qty <= 0:
            return None

        if self.mode is RunMode.PAPER:
            # Validated, not just recorded. This branch used to build the fill
            # dict directly, so it never reached `orders.validate_order` and
            # therefore never reached the risk manager: the kill switch stopped
            # manual paper orders but not a running paper strategy, the paper
            # loss cap could never trip from a run, and neither the notional
            # nor the quantity cap applied to a strategy's own orders.
            try:
                orders_gateway.validate_order(
                    segment_id=self.segment_id,
                    token=int(self.token),
                    order_type="RL_MKT",
                    side=side,
                    quantity=qty,
                    price=price,
                    owner_key=self.session.owner_key,
                    simulated=True,
                    reference_price=price,
                )
            except (OrderRejected, ChoiceGatewayError) as exc:
                self.errors.append(str(exc))
                self._emit({"type": "ORDER_REJECTED", "side": side, "reason": str(exc)})
                logger.warning("Run %s paper order refused: %s", self.run_id, exc)
                return None

            result = {
                "status": "SUCCESS",
                "mode": "PAPER",
                "order_id": f"PAPER-{self.run_id[:8]}-{len(self.orders) + 1}",
                "message": f"Paper fill {side} {qty} @ {price:.2f}",
            }
        else:
            try:
                result = orders_gateway.place_order(
                    session=self.session,
                    segment_id=self.segment_id,
                    token=int(self.token),
                    order_type="RL_MKT",
                    side=side,
                    quantity=qty,
                    price=price,
                    symbol=self.symbol,
                )
            except (OrderRejected, ChoiceGatewayError) as exc:
                self.errors.append(str(exc))
                self._emit({"type": "ORDER_REJECTED", "side": side, "reason": str(exc)})
                logger.warning("Run %s order rejected: %s", self.run_id, exc)
                return None

        # Slippage and charges, the same schedule the backtest applies. Without
        # them a paper run and a backtest on identical bars disagreed: 200
        # round trips showed +4,000 on paper that the backtest scored at
        # roughly -4,000 once ~40 per round trip of charges and 5 bps of
        # slippage were taken off. Promoting a strategy to live on the first
        # number is the failure this exists to prevent.
        fill = self._costs.fill_price(price, side)
        leg_charges = self._costs.charges(fill * qty, side)
        self.total_charges += leg_charges

        if side == "BUY":
            self.position_qty = qty
            self.entry_price = fill
            self.realized_pnl -= leg_charges
        else:
            booked = (fill - self.entry_price) * qty - leg_charges
            self.realized_pnl += booked
            self.position_qty = 0
            self.entry_price = 0.0
            # Book the result against the paper loss ledger so a run that keeps
            # losing eventually trips its own budget. Real losses are untouched:
            # `simulated=True` writes only the paper ledger.
            if self.mode is RunMode.PAPER:
                risk_manager.record_realized_pnl(
                    self.session.owner_key, booked, simulated=True)

        record = {
            "side": side, "quantity": qty, "price": fill,
            "signal_price": price, "charges": round(leg_charges, 2),
            "reason": reason, "result": result,
        }
        self.orders.append(record)
        self._emit({"type": "ORDER_PLACED", **record})
        return result

    def _fail(self, message: str) -> None:
        self.state = RunState.FAILED
        self.errors.append(message)
        logger.error("Run %s failed: %s", self.run_id, message)
        self._emit({"type": "FAILED", "error": message})

    # -- status ------------------------------------------------------------

    def status(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "mode": self.mode.value,
            "state": self.state.value,
            "symbol": self.symbol,
            "segment_id": self.segment_id,
            "token": self.token,
            "position_qty": self.position_qty,
            "entry_price": round(self.entry_price, 2),
            "realized_pnl": round(self.realized_pnl, 2),
            "orders_placed": len(self.orders),
            "bars_seen": len(self._bars),
            "errors": self.errors[-5:],
        }


class RunRegistry:
    """Tracks active runs per user."""

    def __init__(self):
        self._runs: Dict[str, LiveStrategyRunner] = {}
        self._lock = threading.RLock()

    # A run in one of these states will not change again. IDLE is deliberately
    # absent: a runner is registered before it is started, and pruning one mid
    # setup would lose a run that is about to trade. ("HALTED" is a database
    # status for the row, not a state a runner ever holds.)
    TERMINAL = (RunState.STOPPED, RunState.FAILED)

    def add(self, runner: LiveStrategyRunner) -> None:
        with self._lock:
            # Drop finished runs as new ones arrive. Nothing removed them
            # before, so a process left open accumulated every runner it had
            # ever created, each holding a session reference and its order
            # history. Safe because `run_status` reads the database row first
            # and only consults the registry for a *live* run's metrics —
            # `_close_run` has already persisted the final figures by then.
            for run_id in [rid for rid, r in self._runs.items()
                           if r.state in self.TERMINAL]:
                self._runs.pop(run_id, None)
            self._runs[runner.run_id] = runner

    def get(self, run_id: str) -> Optional[LiveStrategyRunner]:
        with self._lock:
            return self._runs.get(run_id)

    def stop(self, run_id: str) -> bool:
        with self._lock:
            runner = self._runs.get(run_id)
        if not runner:
            return False
        runner.stop()
        return True

    def active_count(self) -> int:
        with self._lock:
            return sum(
                1 for r in self._runs.values() if r.state is RunState.RUNNING
            )


run_registry = RunRegistry()
