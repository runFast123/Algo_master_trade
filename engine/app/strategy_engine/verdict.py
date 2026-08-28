"""A plain-language reading of a backtest.

The metrics are correct and dense. Profit factor, Sharpe and max drawdown are
not vocabulary every trader has, and a large green number invites more
confidence than the run supports.

Two rules govern everything here:

* **Only quote what was computed.** Every sentence is derived from a metric the
  runner actually returns. Nothing is estimated, and a missing metric is
  omitted rather than filled in.
* **Past tense, never forecast.** "The worst stretch lasted four months" is an
  observation about this run. "Expect a four-month losing stretch" is a
  prediction the data does not support.

The uncomfortable half is stated as plainly as the profit, because a verdict
that only reports the good news is marketing.
"""

from typing import Any, Dict, List, Optional

# Bars per calendar unit, for turning a drawdown length into words. Only the
# timeframes the backtester supports appear here.
BARS_PER_DAY = {
    "1m": 375, "3m": 125, "5m": 75, "15m": 25, "30m": 12, "1h": 6,
    "1d": 1, "1w": 0.2,
}


def _duration_phrase(bars: int, timeframe: str) -> Optional[str]:
    """Turn a bar count into an English duration, or None if it cannot."""
    per_day = BARS_PER_DAY.get(str(timeframe).lower())
    if not bars or not per_day:
        return None

    days = bars / per_day
    if days < 1:
        return f"{bars} bars"
    if days < 14:
        return f"{round(days)} trading day{'s' if round(days) != 1 else ''}"
    if days < 60:
        weeks = round(days / 5)          # trading weeks
        return f"about {weeks} week{'s' if weeks != 1 else ''}"
    months = round(days / 21)            # trading months
    return f"about {months} month{'s' if months != 1 else ''}"


def verdict(metrics: Dict[str, Any], timeframe: str = "1d") -> Dict[str, Any]:
    """A short reading of the run, plus the caveats that belong beside it."""
    pnl = metrics.get("total_pnl")
    ret = metrics.get("return_pct")
    dd = metrics.get("max_drawdown_pct")
    trades = metrics.get("total_trades") or 0
    win_rate = metrics.get("win_rate")
    profit_factor = metrics.get("profit_factor")
    charges = metrics.get("total_charges")

    if not trades:
        return {
            "headline": "This strategy never traded over the period tested.",
            "detail": ["No entry condition was met, so there is nothing to judge. "
                       "Try a longer range or looser conditions."],
            "caveats": [],
        }

    profitable = (pnl or 0) > 0
    headline = (
        f"Profitable over this period: {ret:+.1f}% across {trades} trades."
        if profitable and ret is not None
        else f"Lost money over this period: {ret:+.1f}% across {trades} trades."
        if ret is not None
        else ("Profitable over this period." if profitable else "Lost money over this period.")
    )

    detail: List[str] = []

    # The drawdown is the part that decides whether a strategy is livable, so
    # it is stated immediately after the headline rather than buried.
    if dd:
        phrase = f"It fell {abs(dd):.1f}% from its peak along the way"
        duration = _duration_phrase(metrics.get("max_drawdown_bars") or 0, timeframe)
        if duration:
            phrase += f", and the longest stretch below a previous peak lasted {duration}"
        detail.append(phrase + ".")

    if win_rate is not None:
        losing = trades - (metrics.get("winning_trades") or 0)
        detail.append(
            f"{win_rate:.0f}% of trades were winners, so {losing} of {trades} lost money."
        )

    if profit_factor:
        detail.append(
            f"Gross profit was {profit_factor:.2f}x gross loss."
            + (" A figure near 1.0 means the edge is thin."
               if profit_factor < 1.3 else "")
        )

    # Costs are modelled, so it is worth saying how much of the result they ate.
    if charges and pnl is not None:
        gross = pnl + charges
        if gross > 0:
            share = charges / gross * 100
            detail.append(
                f"Charges of {charges:,.0f} took {share:.0f}% of the gross profit."
            )
        else:
            detail.append(f"Charges of {charges:,.0f} are included in that result.")

    caveats = [
        "This is what the strategy did on past data, not a forecast.",
        "Fills are modelled at the next bar's open with slippage and Indian "
        "retail charges — real fills can still differ.",
        "Not modelled: regime changes, corporate actions, liquidity limits, "
        "or data errors in the source bars.",
    ]
    if trades < 30:
        caveats.insert(
            0,
            f"Only {trades} trades — too few to tell skill from luck.",
        )

    return {"headline": headline, "detail": detail, "caveats": caveats}
