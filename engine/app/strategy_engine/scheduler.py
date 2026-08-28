"""Drives paper strategy runs from live Choice quotes.

`LiveStrategyRunner` knows how to react to a bar or a tick. Nothing was feeding
it. This is that feed: one thread per run, polling touchline quotes, passing
every quote to ``on_tick`` for stop and target monitoring, and aggregating them
into bars for ``on_bar`` to evaluate the strategy on.

**Polling, not a socket.** Choice publishes a FIX3.0 feed whose address arrives
in the logon response, and connecting to it would be lower latency. Polling the
REST touchline is a fraction of the code, needs no new protocol handling, and
is entirely adequate at bar granularity — which is what the strategy DSL works
in. The socket is an upgrade with a clear seam, not a prerequisite.

Three rules decide when a run stops, and all three fail safe:

* **No market data, no run.** A strategy with no prices cannot make a decision,
  and a run that starts anyway would look alive while doing nothing.
* **A feed gap halts the run.** Deciding on stale prices is worse than not
  deciding. Prompting instead assumes somebody is watching, which is the one
  thing an automated runner exists to avoid.
* **An expired credential halts the run.** The Choice API key lasts about a
  day while a run lasts hours, so a run *will* meet an expiry. Halting with a
  stated reason beats a run that silently stops producing orders while still
  displaying RUNNING.
"""

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional

from engine.app.choice_gateway.errors import ChoiceCredentialExpired
from engine.app.strategy_engine.runner import LiveStrategyRunner, RunState

logger = logging.getLogger("scheduler")

# How often a quote is fetched. The gateway caches quotes for 3 seconds, so
# polling faster only burns rate limit without seeing anything new.
POLL_SECONDS = 5.0

# Consecutive failed polls before the run halts. Three at five seconds tolerates
# a brief blip without trading through a real outage.
MAX_CONSECUTIVE_FAILURES = 3

BAR_SECONDS = {
    "1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800,
    "1h": 3600, "1d": 86400,
}


class _BarBuilder:
    """Aggregates ticks into OHLCV bars on fixed boundaries.

    Bars close on wall-clock boundaries rather than N ticks after the first, so
    a run started mid-minute produces bars aligned with every other consumer of
    the same timeframe.
    """

    def __init__(self, seconds: int):
        self.seconds = max(1, int(seconds))
        self._current: Optional[Dict[str, Any]] = None
        self._bucket: Optional[int] = None

    def add(self, price: float, at: float) -> Optional[Dict[str, Any]]:
        """Feed a price. Returns a completed bar when one closes."""
        bucket = int(at // self.seconds)
        closed = None

        if self._bucket is not None and bucket != self._bucket:
            closed = self._current
            self._current = None

        if self._current is None:
            self._current = {
                "timestamp": datetime.fromtimestamp(
                    bucket * self.seconds, tz=timezone.utc
                ).replace(tzinfo=None),
                "open": price, "high": price, "low": price,
                "close": price, "volume": 0,
            }
            self._bucket = bucket
        else:
            self._current["high"] = max(self._current["high"], price)
            self._current["low"] = min(self._current["low"], price)
            self._current["close"] = price

        return closed


class PaperRunScheduler:
    """One polling thread per run."""

    def __init__(self):
        self._threads: Dict[str, threading.Thread] = {}
        self._stop_flags: Dict[str, threading.Event] = {}
        self._lock = threading.RLock()

    def start(
        self,
        runner: LiveStrategyRunner,
        timeframe: str = "1m",
        on_halt: Optional[Callable[[str, str], None]] = None,
    ) -> None:
        """Begin feeding this runner. Raises if it is already running."""
        with self._lock:
            if runner.run_id in self._threads:
                raise RuntimeError(f"Run {runner.run_id} is already scheduled")
            stop = threading.Event()
            thread = threading.Thread(
                target=self._loop,
                args=(runner, timeframe, stop, on_halt),
                name=f"paper-run-{runner.run_id[:8]}",
                daemon=True,
            )
            self._threads[runner.run_id] = thread
            self._stop_flags[runner.run_id] = stop
            thread.start()
        logger.info("Paper run %s started on %s", runner.run_id, timeframe)

    def stop(self, run_id: str) -> bool:
        """Ask a run to stop. Returns False if it was not scheduled."""
        with self._lock:
            flag = self._stop_flags.pop(run_id, None)
            self._threads.pop(run_id, None)
        if flag is None:
            return False
        flag.set()
        return True

    def is_running(self, run_id: str) -> bool:
        with self._lock:
            thread = self._threads.get(run_id)
        return bool(thread and thread.is_alive())

    # -- the loop ----------------------------------------------------------

    def _loop(self, runner, timeframe, stop, on_halt) -> None:
        from engine.app.choice_gateway.market import get_multiple_touchline

        builder = _BarBuilder(BAR_SECONDS.get(str(timeframe).lower(), 60))
        seg_token = f"{runner.segment_id}_{runner.token}"
        failures = 0

        def halt(reason: str) -> None:
            # Record the reason *before* changing the state. Anything watching
            # the run — the UI poller, a supervising process — reacts to the
            # state, and a run that reads as stopped with no reason attached is
            # the same puzzle this scheduler exists to avoid.
            runner.errors.append(reason)
            logger.warning("Paper run %s halted: %s", runner.run_id, reason)
            if on_halt:
                try:
                    on_halt(runner.run_id, reason)
                except Exception:
                    logger.exception("halt callback failed for %s", runner.run_id)
            runner.stop()

        while not stop.is_set() and runner.state is RunState.RUNNING:
            try:
                quotes = get_multiple_touchline(runner.session, seg_token)
                rows = quotes.get("data") or []
                price = next(
                    (float(r["ltp"]) for r in rows if r.get("ltp")), None
                )
                if price is None:
                    raise ValueError("quote carried no last traded price")
                failures = 0

            except ChoiceCredentialExpired:
                # The key lapsed mid-run. Stop rather than retry: no amount of
                # retrying revives an expired key, and the user has to paste a
                # new one.
                halt("Choice API key expired during the run — reconnect with a "
                     "fresh key, then start the run again")
                return

            except Exception as exc:
                failures += 1
                logger.info(
                    "Paper run %s: quote poll failed (%d/%d): %s",
                    runner.run_id, failures, MAX_CONSECUTIVE_FAILURES, exc,
                )
                if failures >= MAX_CONSECUTIVE_FAILURES:
                    halt(f"Market data unavailable after "
                         f"{MAX_CONSECUTIVE_FAILURES} attempts: {exc}")
                    return
                stop.wait(POLL_SECONDS)
                continue

            now = time.time()
            # Ticks drive stops and targets; bars drive entries and exits.
            try:
                runner.on_tick({"ltp": price})
                closed = builder.add(price, now)
                if closed is not None:
                    runner.on_bar(closed)
            except Exception:
                logger.exception("Paper run %s: strategy step failed", runner.run_id)

            stop.wait(POLL_SECONDS)

        if runner.state is RunState.RUNNING:
            runner.stop()
        logger.info("Paper run %s loop ended (%s)", runner.run_id, runner.state.value)


paper_scheduler = PaperRunScheduler()
