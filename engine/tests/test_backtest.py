import pandas as pd
import pytest

from engine.app.choice_gateway.client_manager import ChoiceSession
from engine.app.choice_gateway.errors import ChoiceNotConnected
from engine.app.strategy_engine.backtest_runner import _period_consistency, CostModel, run_backtest
from engine.app.strategy_engine.dsl import DSLError

DSL = {
    "indicators": {"rsi_14": {"type": "RSI", "length": 14}},
    "entry_conditions": [{"field": "rsi_14", "operator": "<", "value": 45}],
    "exit_conditions": [{"field": "rsi_14", "operator": ">", "value": 55}],
    "actions": {"buy_qty": 20},
}

PARAMS = {
    "symbol": "RELIANCE", "segment_id": 1, "token": "2885", "timeframe": "1d",
    "start_date": "2025-01-01", "end_date": "2025-06-30", "initial_capital": 100000.0,
}


@pytest.fixture
def sandbox_session():
    session = ChoiceSession("test-user")
    session.start_demo("DEMO")
    return session


def test_backtest_produces_real_metrics(sandbox_session):
    result = run_backtest(sandbox_session, DSL, PARAMS)
    metrics = result["metrics"]

    assert result["status"] == "COMPLETED"
    assert metrics["initial_capital"] == 100000.0
    # The removed fallback always returned exactly these values.
    assert not (metrics["return_pct"] == 15.0 and metrics["win_rate"] == 66.7)
    assert metrics["final_capital"] == pytest.approx(
        metrics["initial_capital"] + metrics["total_pnl"], abs=0.5
    )
    for key in ("max_drawdown_pct", "sharpe_ratio", "profit_factor", "total_charges"):
        assert key in metrics
    assert len(metrics["equity_curve"]) > 0


def test_sandbox_results_are_labelled(sandbox_session):
    result = run_backtest(sandbox_session, DSL, PARAMS)
    assert result["data_source"]["source"] == "SANDBOX_SYNTHETIC"
    assert result["data_source"]["is_real_market_data"] is False
    assert any("synthetic" in line.lower() for line in result["logs"])


def test_instrument_is_honoured(sandbox_session):
    """The requested symbol must drive the data, not a default token."""
    reliance = run_backtest(sandbox_session, DSL, PARAMS)
    infy = run_backtest(sandbox_session, DSL, {**PARAMS, "symbol": "INFY", "token": "1594"})

    assert reliance["instrument"]["token"] == "2885"
    assert infy["instrument"]["token"] == "1594"
    assert reliance["metrics"]["total_pnl"] != infy["metrics"]["total_pnl"]


def test_costs_reduce_net_pnl(sandbox_session):
    """Charges must actually be deducted, not merely reported."""
    with_costs = run_backtest(sandbox_session, DSL, PARAMS)
    free = run_backtest(
        sandbox_session, DSL, PARAMS,
        cost_model=CostModel(brokerage_pct=0, brokerage_cap=0, stt_pct=0,
                             exchange_pct=0, sebi_pct=0, stamp_duty_pct=0,
                             gst_pct=0, slippage_pct=0),
    )
    if with_costs["metrics"]["total_trades"] > 0:
        assert with_costs["metrics"]["total_charges"] > 0
        assert with_costs["metrics"]["total_pnl"] < free["metrics"]["total_pnl"]


def test_position_size_respects_capital(sandbox_session):
    """A run that cannot afford the position must skip it, not go negative."""
    result = run_backtest(
        sandbox_session,
        {**DSL, "actions": {"buy_qty": 10000}},
        {**PARAMS, "initial_capital": 1000.0},
    )
    assert result["metrics"]["final_capital"] >= 0
    assert result["metrics"]["total_trades"] == 0


def test_invalid_strategy_is_rejected(sandbox_session):
    with pytest.raises(DSLError):
        run_backtest(sandbox_session, {"indicators": {}, "actions": {}}, PARAMS)


def test_backtest_requires_a_connected_session():
    """A disconnected session fails loudly rather than inventing data."""
    with pytest.raises(ChoiceNotConnected):
        run_backtest(ChoiceSession("nobody"), DSL, PARAMS)


def test_trades_carry_entry_and_exit_details(sandbox_session):
    result = run_backtest(sandbox_session, DSL, PARAMS)
    for trade in result["metrics"]["trades"]:
        assert trade["entry_price"] > 0
        assert trade["exit_price"] > 0
        assert trade["charges"] >= 0
        assert trade["entry_date"] and trade["exit_date"]


# -- consistency across periods --------------------------------------------
#
# A single headline return says nothing about *when* it was earned. These check
# the split reports that, and that it never claims more rigour than it has.

def _rising_then_falling(bars=40):
    # One equity point per bar, exactly as the runner produces — an odd bar
    # count must not leave the two out of step.
    half = bars // 2
    equity = [100000 + i * 500 for i in range(half)]
    equity += [equity[-1] - i * 400 for i in range(1, bars - half + 1)]
    df = pd.DataFrame({"timestamp": pd.date_range("2026-01-01", periods=bars, freq="D")})
    trades = [{"exit_date": str(df["timestamp"][i]),
               "pnl": 500 if i < bars // 2 else -400}
              for i in range(2, bars, 4)]
    return df, equity, trades


def test_periods_reveal_a_result_earned_in_only_half_the_range():
    df, equity, trades = _rising_then_falling()
    periods = _period_consistency(df, equity, trades)

    assert len(periods) == 4
    assert [p["return_pct"] > 0 for p in periods] == [True, True, False, False]
    # The headline is mildly positive and hides the second-half decline.
    assert (equity[-1] - equity[0]) > 0


def test_every_bar_lands_in_a_period():
    """The last segment absorbs the remainder, so an odd bar count drops
    nothing off the end."""
    df, equity, trades = _rising_then_falling(bars=41)
    periods = _period_consistency(df, equity, trades)

    assert periods[0]["from"] == str(df["timestamp"].iloc[0])[:10]
    assert periods[-1]["to"] == str(df["timestamp"].iloc[-1])[:10]


def test_trades_are_attributed_to_the_period_they_closed_in():
    df, equity, trades = _rising_then_falling()
    periods = _period_consistency(df, equity, trades)

    assert sum(p["trades"] for p in periods) == len(trades)
    assert periods[0]["win_rate"] == 100.0
    assert periods[-1]["win_rate"] == 0.0


def test_a_period_with_no_trades_reports_no_win_rate_rather_than_zero():
    """Zero percent means "tried and lost every time"; this period did not
    trade, and the two must not read the same."""
    df = pd.DataFrame({"timestamp": pd.date_range("2026-01-01", periods=40, freq="D")})
    equity = [100000] * 40
    periods = _period_consistency(df, equity, trades=[])

    assert all(p["win_rate"] is None for p in periods)
    assert all(p["trades"] == 0 for p in periods)


def test_too_few_bars_reports_nothing_rather_than_noise():
    """Splitting six bars four ways measures nothing; say so by staying silent."""
    df = pd.DataFrame({"timestamp": pd.date_range("2026-01-01", periods=6, freq="D")})
    assert _period_consistency(df, [100000] * 6, []) == []
