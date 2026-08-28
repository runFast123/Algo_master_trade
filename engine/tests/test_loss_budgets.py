"""Paper losses and real losses are separate budgets.

They were one. A losing paper strategy — which this app actively encourages
people to run — consumed the allowance protecting real funds and halted the
user's actual trading for the day, with a message reading "daily loss limit
reached" and no hint that none of it was real.

Making mode switching easy made this reachable in a click, which is what turned
a latent design flaw into a live one.
"""

import pytest

import engine.app.strategy_engine.risk_manager as rmod
from engine.app.choice_gateway.client_manager import ChoiceSession, SessionMode
from engine.app.choice_gateway.errors import OrderRejected


@pytest.fixture
def risk(monkeypatch):
    """One manager, patched everywhere it is bound.

    `orders.py` does `from ... import risk_manager` at module import, so it
    holds its own reference; `client_manager.py` imports it inside the function
    and resolves the module attribute each call. Patching only the module left
    the two halves of a test talking to different managers — which is a fault
    in the test, not the code, but one that reported a pass while measuring
    nothing.
    """
    import engine.app.choice_gateway.orders as gateway

    manager = rmod.RiskManager(max_daily_loss=5000.0)
    monkeypatch.setattr(rmod, "risk_manager", manager)
    monkeypatch.setattr(gateway, "risk_manager", manager)
    return manager


@pytest.fixture
def paper_session(risk):
    session = ChoiceSession(owner_key="budgets")
    session.mode = SessionMode.PAPER
    session.session_id = "SESS"
    return session


def _lose_on_paper(session, amount):
    """Book `amount` of simulated loss through the real fill path."""
    session.record_simulated_fill("ACME", "BUY", 100, 1000.0)
    session.record_simulated_fill("ACME", "SELL", 100, 1000.0 - amount / 100)


def test_a_paper_loss_does_not_stop_real_orders(risk, paper_session):
    """The fault this file exists for."""
    _lose_on_paper(paper_session, 10000)

    risk.validate_order(quantity=1, price=100.0, side="BUY",
                        owner_key="budgets", simulated=False)


def test_a_paper_loss_does_stop_further_paper_orders(risk, paper_session):
    """The budget still means something where it applies."""
    _lose_on_paper(paper_session, 10000)

    with pytest.raises(OrderRejected, match="Paper daily loss"):
        risk.validate_order(quantity=1, price=100.0, side="BUY",
                            owner_key="budgets", simulated=True)


def test_the_paper_refusal_says_real_trading_is_unaffected(risk, paper_session):
    """Someone reading this at 2pm needs to know immediately whether their
    real account is stopped."""
    _lose_on_paper(paper_session, 10000)

    with pytest.raises(OrderRejected) as excinfo:
        risk.validate_order(quantity=1, price=100.0, side="BUY",
                            owner_key="budgets", simulated=True)
    assert "Real trading is unaffected" in str(excinfo.value)


def test_a_paper_loss_does_not_trip_the_kill_switch(risk, paper_session):
    """Halting is shared: tripping it on a simulated loss would stop real
    orders through the back door, which is the same bug wearing a hat."""
    _lose_on_paper(paper_session, 10000)

    with pytest.raises(OrderRejected):
        risk.validate_order(quantity=1, price=100.0, side="BUY",
                            owner_key="budgets", simulated=True)
    assert risk.is_halted("budgets") is None


def test_a_real_loss_still_stops_real_orders(risk):
    """The protection that matters must survive the fix."""
    risk.set_realized_pnl("budgets", -9000.0)

    with pytest.raises(OrderRejected, match="Daily loss limit"):
        risk.validate_order(quantity=1, price=100.0, side="BUY",
                            owner_key="budgets", simulated=False)


def test_a_real_loss_does_trip_the_kill_switch(risk):
    risk.set_realized_pnl("budgets", -9000.0)

    with pytest.raises(OrderRejected):
        risk.validate_order(quantity=1, price=100.0, side="BUY", owner_key="budgets")
    assert risk.is_halted("budgets") is not None


def test_the_ledgers_do_not_leak_into_each_other(risk, paper_session):
    _lose_on_paper(paper_session, 10000)

    assert risk.realized_pnl("budgets", simulated=True) == pytest.approx(-10000.0)
    assert risk.realized_pnl("budgets") == 0.0


def test_a_real_loss_does_not_stop_paper_testing(risk):
    """The reverse case: someone who lost real money today should still be able
    to test a strategy on paper."""
    risk.set_realized_pnl("budgets", -9000.0)

    risk.validate_order(quantity=1, price=100.0, side="BUY",
                        owner_key="budgets", simulated=True)


def test_a_paper_order_placed_through_the_gateway_uses_the_paper_budget(risk, paper_session):
    """End to end: the session's own nature selects the budget, so nothing
    depends on a caller remembering to pass a flag."""
    from engine.app.choice_gateway.orders import validate_order

    _lose_on_paper(paper_session, 10000)
    # A real order is unaffected...
    validate_order(segment_id=1, token=2885, order_type="RL_MKT", side="BUY",
                   quantity=1, price=100.0, owner_key="budgets", simulated=False)
    # ...and a simulated one is refused.
    with pytest.raises(OrderRejected, match="Paper daily loss"):
        validate_order(segment_id=1, token=2885, order_type="RL_MKT", side="BUY",
                       quantity=1, price=100.0, owner_key="budgets", simulated=True)


@pytest.mark.parametrize("mode,expected", [
    (SessionMode.PAPER, True),
    (SessionMode.DEMO, True),
    (SessionMode.LIVE, False),
])
def test_place_order_reports_the_sessions_nature(risk, monkeypatch, mode, expected):
    """Nothing depends on a caller remembering a flag: the session decides.

    Asserted at the call, because passing `simulated=` explicitly in a test
    proves the risk manager works and says nothing about whether the gateway
    ever tells it the truth.
    """
    import engine.app.choice_gateway.orders as gateway

    session = ChoiceSession(owner_key="wiring")
    session.mode = mode
    session.session_id = "SESS"

    seen = {}

    def spy(**kwargs):
        seen.update(kwargs)
        raise OrderRejected("stop here — the flag is what matters")

    monkeypatch.setattr(gateway, "validate_order", spy)

    with pytest.raises(OrderRejected):
        gateway.place_order(session=session, segment_id=1, token=2885,
                            order_type="RL_MKT", side="BUY", quantity=1,
                            price=100.0, symbol="RELIANCE")

    assert seen["simulated"] is expected


# -- what the kill switch stops --------------------------------------------

def test_a_halt_stops_real_orders(risk):
    risk.halt("budgets", "pulled by the user")

    with pytest.raises(OrderRejected, match="halted"):
        risk.validate_order(quantity=1, price=100.0, side="BUY",
                            owner_key="budgets", simulated=False)


def test_the_kill_switch_stops_paper_too(risk):
    """Someone pressing it means "stop what I am doing". A kill switch that
    leaves a paper strategy running is a kill switch that did nothing — and
    scoping every halt to real orders made it exactly that."""
    risk.halt("budgets", "pulled by the user")          # default scope: all

    with pytest.raises(OrderRejected, match="halted"):
        risk.validate_order(quantity=1, price=100.0, side="BUY",
                            owner_key="budgets", simulated=True)


def test_a_real_loss_halt_does_not_stop_paper_testing(risk):
    """The automatic cap protects funds. Simulated orders risk none, and
    blocking them prevents working out what went wrong."""
    risk.halt("budgets", "daily loss reached", scope="real")

    risk.validate_order(quantity=1, price=100.0, side="BUY",
                        owner_key="budgets", simulated=True)


def test_a_real_loss_halt_still_leaves_paper_available(risk):
    """The end-to-end version: losing real money trips the breaker, and paper
    keeps working."""
    risk.set_realized_pnl("budgets", -9000.0)
    with pytest.raises(OrderRejected):
        risk.validate_order(quantity=1, price=100.0, side="BUY", owner_key="budgets")

    assert risk.is_halted("budgets") is not None
    risk.validate_order(quantity=1, price=100.0, side="BUY",
                        owner_key="budgets", simulated=True)


def test_the_paper_budget_still_applies_while_halted(risk, paper_session):
    """"Paper keeps working" is not "paper has no limits"."""
    risk.halt("budgets", "daily loss reached", scope="real")
    _lose_on_paper(paper_session, 10000)

    with pytest.raises(OrderRejected, match="Paper daily loss"):
        risk.validate_order(quantity=1, price=100.0, side="BUY",
                            owner_key="budgets", simulated=True)
