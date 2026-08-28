"""The plain-language reading of a backtest.

Two rules are load-bearing and each has a test: only quote metrics that were
actually computed, and never phrase an observation as a forecast.
"""

import pytest

from engine.app.strategy_engine.verdict import verdict

GOOD = {
    "total_pnl": 18450.0, "return_pct": 18.45, "max_drawdown_pct": -22.3,
    "max_drawdown_bars": 84, "total_trades": 47, "winning_trades": 26,
    "win_rate": 55.3, "profit_factor": 1.42, "total_charges": 3120.0,
}


def test_a_profitable_run_still_states_the_drawdown():
    """A verdict that reports only the profit is marketing."""
    v = verdict(GOOD, "1d")

    assert "Profitable" in v["headline"]
    assert any("fell 22.3%" in d for d in v["detail"])


def test_the_drawdown_duration_is_quoted_when_it_was_computed():
    v = verdict(GOOD, "1d")
    assert any("about 4 months" in d for d in v["detail"])


def test_no_duration_is_quoted_when_it_was_not_computed():
    """The metric is the only source. Absent it, say nothing rather than
    estimate — inventing a number here is the failure this guards."""
    without = {k: val for k, val in GOOD.items() if k != "max_drawdown_bars"}
    v = verdict(without, "1d")

    joined = " ".join(v["detail"])
    assert "fell 22.3%" in joined
    assert "month" not in joined and "day" not in joined


def test_nothing_is_phrased_as_a_forecast():
    """Past tense only. "Expect a 4-month losing stretch" is a prediction the
    data does not support."""
    v = verdict(GOOD, "1d")
    text = " ".join([v["headline"], *v["detail"], *v["caveats"]]).lower()

    for forecast in ("expect ", "will likely", "should return", "predicts"):
        assert forecast not in text
    assert "not a forecast" in text


def test_a_thin_sample_is_called_out_first():
    v = verdict({**GOOD, "total_trades": 4, "winning_trades": 1}, "1d")
    assert "too few to tell skill from luck" in v["caveats"][0]


def test_a_strategy_that_never_traded_says_so_plainly():
    v = verdict({"total_trades": 0})
    assert "never traded" in v["headline"]
    assert v["caveats"] == []


def test_a_losing_run_is_not_dressed_up():
    v = verdict({**GOOD, "total_pnl": -900.0, "return_pct": -9.0}, "1d")
    assert "Lost money" in v["headline"]


def test_costs_are_reported_as_a_share_of_gross_profit():
    v = verdict(GOOD, "1d")
    assert any("took 14% of the gross profit" in d for d in v["detail"])


def test_a_thin_edge_is_flagged():
    v = verdict({**GOOD, "profit_factor": 1.05}, "1d")
    assert any("edge is thin" in d for d in v["detail"])


@pytest.mark.parametrize("timeframe,bars,expected", [
    ("1d", 3, "3 trading days"),
    ("1d", 20, "about 4 weeks"),
    ("1d", 84, "about 4 months"),
    ("5m", 30, "30 bars"),
])
def test_durations_are_rendered_in_units_a_person_uses(timeframe, bars, expected):
    v = verdict({**GOOD, "max_drawdown_bars": bars}, timeframe)
    assert any(expected in d for d in v["detail"])
