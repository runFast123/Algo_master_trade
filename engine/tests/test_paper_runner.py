"""The paper strategy runner and its scheduler.

The safety rules are the reason this file exists. A runner that keeps trading
on a stale feed, or that sits at RUNNING after its credential expired, is worse
than one that refuses to start — so each of those paths is driven here with a
fake quote source rather than left to be discovered live.
"""

import time

import pytest

from engine.app.choice_gateway.errors import ChoiceCredentialExpired
from engine.app.strategy_engine.runner import LiveStrategyRunner, RunMode, RunState
from engine.app.strategy_engine.scheduler import (
    MAX_CONSECUTIVE_FAILURES,
    PaperRunScheduler,
    _BarBuilder,
)

RSI_DIP = {
    "indicators": {"r": {"type": "RSI", "length": 2}},
    "entry_conditions": [{"field": "r", "operator": "<", "value": 99}],
    "exit_conditions": [{"field": "r", "operator": ">", "value": 99.9}],
    "actions": {"buy_qty": 1},
}


class _FakeSession:
    """A session whose quotes we control."""
    is_demo = False
    owner_key = "u1"

    class mode:
        value = "PAPER"
        uses_broker_data = True
        sends_real_orders = False

    def __init__(self):
        self.is_paper = True
        self.simulates_orders = True
        self.simulated_orders = []
        self.paper_positions = {}
        self.paper_realized_pnl = 0.0

    def record_simulated_fill(self, *a, **k):
        return {"symbol": a[0], "position_quantity": 0, "average_price": 0.0,
                "realized_pnl": 0.0}


# -- bar aggregation -------------------------------------------------------

def test_bars_close_on_wall_clock_boundaries():
    """Aligned bars, so a run started mid-minute agrees with every other
    consumer of the same timeframe."""
    builder = _BarBuilder(60)
    base = 1_786_600_000 - (1_786_600_000 % 60)

    assert builder.add(100.0, base + 5) is None
    assert builder.add(105.0, base + 30) is None
    bar = builder.add(102.0, base + 61)

    assert bar["open"] == 100.0 and bar["high"] == 105.0
    assert bar["low"] == 100.0 and bar["close"] == 105.0
    assert bar["timestamp"].second == 0


def test_a_bar_tracks_high_and_low():
    builder = _BarBuilder(60)
    base = 1_786_600_000 - (1_786_600_000 % 60)
    for price in (100.0, 120.0, 90.0, 110.0):
        builder.add(price, base + 1)
    bar = builder.add(105.0, base + 61)

    assert bar["high"] == 120.0
    assert bar["low"] == 90.0


# -- the safety rules ------------------------------------------------------

def _runner():
    return LiveStrategyRunner(
        run_id="r1", session=_FakeSession(), dsl_def=RSI_DIP,
        params={"segment_id": 1, "token": "2885", "symbol": "RELIANCE"},
        mode=RunMode.PAPER,
    )


def _run_with_quotes(monkeypatch, quote_fn, wait=2.0):
    """Drive the scheduler with a controlled quote source."""
    monkeypatch.setattr(
        "engine.app.choice_gateway.market.get_multiple_touchline", quote_fn
    )
    monkeypatch.setattr("engine.app.strategy_engine.scheduler.POLL_SECONDS", 0.01)

    runner = _runner()
    runner.start()
    halts = []
    scheduler = PaperRunScheduler()
    scheduler.start(runner, timeframe="1m",
                    on_halt=lambda rid, reason: halts.append(reason))

    # Wait for the run to end *and* its reason to be recorded. Watching the
    # state alone races the halt path, which is precisely the ordering the
    # scheduler now guarantees.
    deadline = time.time() + wait
    while time.time() < deadline and runner.state is RunState.RUNNING:
        time.sleep(0.01)
    while time.time() < deadline and runner.state is not RunState.RUNNING and not halts:
        time.sleep(0.01)
    scheduler.stop(runner.run_id)
    return runner, halts


def test_a_feed_gap_halts_the_run(monkeypatch):
    """Deciding on stale prices is worse than not deciding, and prompting
    assumes somebody is watching."""
    def broken(session, seg_tokens):
        raise ConnectionError("feed unreachable")

    runner, halts = _run_with_quotes(monkeypatch, broken)

    assert runner.state is not RunState.RUNNING
    assert halts and "Market data unavailable" in halts[0]


def test_a_brief_blip_does_not_halt_the_run(monkeypatch):
    """One failed poll is a blip; the threshold exists so a blip is tolerated."""
    calls = {"n": 0}

    def flaky(session, seg_tokens):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("one-off")
        return {"data": [{"ltp": 100.0}]}

    runner, halts = _run_with_quotes(monkeypatch, flaky, wait=0.5)

    assert not halts
    assert runner.state is RunState.RUNNING
    assert calls["n"] > MAX_CONSECUTIVE_FAILURES   # recovered and kept polling


def test_an_expired_credential_halts_immediately(monkeypatch):
    """No amount of retrying revives an expired key, so the run stops on the
    first occurrence rather than after the failure threshold."""
    def expired(session, seg_tokens):
        raise ChoiceCredentialExpired("expired", "Token Expired")

    runner, halts = _run_with_quotes(monkeypatch, expired)

    assert runner.state is not RunState.RUNNING
    assert halts and "API key expired" in halts[0]


def test_a_quote_without_a_price_counts_as_a_gap(monkeypatch):
    """An empty quote is not a price. Treating it as one would feed the
    strategy a null and produce a decision from nothing."""
    def priceless(session, seg_tokens):
        return {"data": [{"ltp": None}]}

    runner, halts = _run_with_quotes(monkeypatch, priceless)

    assert runner.state is not RunState.RUNNING
    assert halts


def test_stopping_a_run_that_was_never_scheduled_is_not_an_error():
    scheduler = PaperRunScheduler()
    assert scheduler.stop("no-such-run") is False


def test_a_run_cannot_be_scheduled_twice(monkeypatch):
    """Two runners on one strategy would compete for the same position."""
    monkeypatch.setattr(
        "engine.app.choice_gateway.market.get_multiple_touchline",
        lambda s, t: {"data": [{"ltp": 100.0}]},
    )
    monkeypatch.setattr("engine.app.strategy_engine.scheduler.POLL_SECONDS", 0.05)

    runner = _runner()
    runner.start()
    scheduler = PaperRunScheduler()
    scheduler.start(runner, timeframe="1m")
    try:
        with pytest.raises(RuntimeError, match="already scheduled"):
            scheduler.start(runner, timeframe="1m")
    finally:
        scheduler.stop(runner.run_id)


def test_paper_orders_never_reach_the_broker(monkeypatch):
    """The whole premise: a paper run must have no path to Choice's order
    endpoint. The session used here would raise if one were attempted."""
    runner = _runner()
    runner.start()

    class Exploding(_FakeSession):
        def require_client(self):
            raise AssertionError("a paper run must never obtain a broker client")

    runner.session = Exploding()
    runner.on_bar({"timestamp": 0, "open": 1, "high": 1, "low": 1,
                   "close": 1, "volume": 0})
    runner.on_bar({"timestamp": 1, "open": 1, "high": 2, "low": 1,
                   "close": 2, "volume": 0})
    assert runner.state in (RunState.RUNNING, RunState.STOPPED)


# -- the paper position book, signed ---------------------------------------
#
# Strategies never go short: the runner sells only what it holds. The order
# ticket has no such guard, and the arithmetic used to invent money when it was
# used — `min(qty, held)` is negative once `held` is, which is truthy, so adding
# to a short booked a fabricated loss while covering one booked nothing. The
# figure feeds the daily loss cap, so a profitable trade could halt the day.

def _paper_session():
    from engine.app.choice_gateway.client_manager import ChoiceSession, SessionMode
    session = ChoiceSession(owner_key="paper-book")
    session.mode = SessionMode.PAPER
    session.paper_positions = {}
    session.paper_realized_pnl = 0.0
    return session


def _fills(*steps):
    session = _paper_session()
    for side, qty, price in steps:
        session.record_simulated_fill("ACME", side, qty, price)
    return session


@pytest.mark.parametrize("name,steps,expected", [
    ("a long closed for a gain", (("BUY", 10, 100), ("SELL", 10, 110)), 100),
    ("a long closed for a loss", (("BUY", 10, 100), ("SELL", 10, 90)), -100),
    ("a partial close books only the part closed",
     (("BUY", 10, 100), ("SELL", 4, 110)), 40),
    ("averaging up before the exit",
     (("BUY", 10, 100), ("BUY", 10, 120), ("SELL", 20, 130)), 400),
    ("a short covered for a gain",
     (("SELL", 10, 100), ("SELL", 5, 100), ("BUY", 15, 80)), 300),
    ("a short covered for a loss", (("SELL", 10, 100), ("BUY", 10, 120)), -200),
    ("reversing from long to short books only the long",
     (("BUY", 10, 100), ("SELL", 15, 110)), 100),
])
def test_realised_pnl_is_correct_in_both_directions(name, steps, expected):
    assert round(_fills(*steps).paper_realized_pnl, 2) == expected, name


def test_extending_a_short_books_nothing():
    """The specific arithmetic that invented a loss: adding to a short is an
    opening trade and realises nothing at all."""
    session = _fills(("SELL", 10, 100), ("SELL", 5, 100))
    assert session.paper_realized_pnl == 0.0


def test_a_short_records_the_price_it_was_opened_at():
    """Left at zero, every unrealised figure on an open short is nonsense."""
    session = _fills(("SELL", 10, 250.0))
    assert session.paper_positions["ACME"]["average_price"] == 250.0
    assert session.paper_positions["ACME"]["quantity"] == -10


def test_reversing_starts_the_new_side_at_the_reversing_price():
    session = _fills(("BUY", 10, 100), ("SELL", 15, 110))
    position = session.paper_positions["ACME"]
    assert position["quantity"] == -5
    assert position["average_price"] == 110.0


def test_a_flat_position_is_forgotten():
    session = _fills(("BUY", 10, 100), ("SELL", 10, 110))
    assert "ACME" not in session.paper_positions


def test_the_fill_path_holds_the_book_lock():
    """One session is shared by every paper run for a user, and the scheduler
    gives each run its own thread. A lost update on `paper_realized_pnl` is
    money quietly missing from the day's figure — and from the loss cap that
    reads it. Asserted directly rather than by racing threads, because a
    concurrency test that only sometimes fails is worse than none."""
    session = _paper_session()
    observed = {}
    original = session._apply_fill

    def spy(*args, **kwargs):
        observed["locked"] = session.book_lock.locked()
        return original(*args, **kwargs)

    session._apply_fill = spy
    session.record_simulated_fill("ACME", "BUY", 1, 100.0)

    assert observed["locked"] is True


def test_concurrent_fills_do_not_lose_any(monkeypatch):
    """Every fill lands. Exact because the book is serialised."""
    import threading as _t
    session = _paper_session()
    session.record_simulated_fill("ACME", "BUY", 400, 100.0)

    def sell_one():
        session.record_simulated_fill("ACME", "SELL", 1, 110.0)

    threads = [_t.Thread(target=sell_one) for _ in range(200)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert session.paper_positions["ACME"]["quantity"] == 200
    assert round(session.paper_realized_pnl, 2) == 2000.0    # 200 x 10


# -- the run registry does not grow forever --------------------------------

def _finished_runner(run_id, state):
    from engine.app.strategy_engine.runner import RunState
    runner = LiveStrategyRunner(
        run_id=run_id, session=_paper_session(), dsl_def=RSI_DIP,
        params={"segment_id": 1, "token": "2885", "symbol": "RELIANCE"},
        mode=RunMode.PAPER)
    runner.state = state
    return runner


def test_finished_runs_are_dropped_as_new_ones_start():
    """Nothing removed them, so a process left open accumulated every runner it
    had ever created, each holding a session and its order history."""
    from engine.app.strategy_engine.runner import RunRegistry, RunState

    registry = RunRegistry()
    registry.add(_finished_runner("old-1", RunState.STOPPED))
    registry.add(_finished_runner("old-3", RunState.FAILED))

    live = _finished_runner("live-1", RunState.RUNNING)
    registry.add(live)

    assert registry.get("live-1") is live
    assert registry.get("old-1") is None
    assert registry.get("old-3") is None


def test_an_idle_run_is_not_pruned_before_it_starts():
    """A runner is registered before it is started; dropping one mid-setup
    would lose a run that is about to trade."""
    from engine.app.strategy_engine.runner import RunRegistry, RunState

    registry = RunRegistry()
    idle = _finished_runner("about-to-start", RunState.IDLE)
    registry.add(idle)
    registry.add(_finished_runner("another", RunState.RUNNING))

    assert registry.get("about-to-start") is idle


def test_a_running_run_is_never_dropped():
    """Pruning must not reach a run that is still trading."""
    from engine.app.strategy_engine.runner import RunRegistry, RunState

    registry = RunRegistry()
    running = _finished_runner("still-going", RunState.RUNNING)
    registry.add(running)
    registry.add(_finished_runner("newer", RunState.RUNNING))

    assert registry.get("still-going") is running
    assert registry.active_count() == 2
