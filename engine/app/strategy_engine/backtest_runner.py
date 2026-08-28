"""Historical backtesting.

Results are always derived from an actual run over actual bars. There is no
fallback that manufactures a plausible-looking result: if data cannot be
fetched or the strategy is invalid, the run fails and says why.

Fills are modelled at the *next* bar's open with slippage and Indian retail
transaction costs applied, because filling at the close of the bar that
generated the signal is not reachable in live trading and inflates results.
"""

import logging
import math
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from engine.app.choice_gateway.client_manager import ChoiceSession
from engine.app.costs import CostModel
from engine.app.choice_gateway.historical import get_historical_ohlcv
from engine.app.choice_gateway.scrip_master import resolve_instrument
from engine.app.strategy_engine.dsl import DSLError, dsl_engine
from engine.app.strategy_engine.verdict import verdict

logger = logging.getLogger("backtest")

TRADING_DAYS_PER_YEAR = 252


def _metrics(
    equity: List[float],
    trades: List[Dict[str, Any]],
    initial_capital: float,
    bars_per_year: float,
) -> Dict[str, Any]:
    equity_series = pd.Series(equity, dtype="float64")
    final_capital = float(equity_series.iloc[-1]) if len(equity_series) else initial_capital
    total_pnl = final_capital - initial_capital

    running_max = equity_series.cummax()
    drawdown = (equity_series - running_max) / running_max.replace(0, np.nan)
    max_drawdown_pct = float(drawdown.min() * 100) if len(drawdown) else 0.0
    if math.isnan(max_drawdown_pct):
        max_drawdown_pct = 0.0

    # How long the worst drawdown lasted, in bars. The depth of a drawdown says
    # what you would have lost; the length says how long you would have had to
    # sit through it, which is the part that decides whether a strategy is
    # actually livable. Measured as the longest run of bars below a prior peak.
    longest, current = 0, 0
    for value, peak in zip(equity_series, running_max):
        if value < peak:
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    max_drawdown_bars = int(longest)

    returns = equity_series.pct_change().dropna()
    if len(returns) > 1 and returns.std() > 0:
        sharpe = float(returns.mean() / returns.std() * math.sqrt(bars_per_year))
    else:
        sharpe = 0.0

    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    gross_profit = sum(t["pnl"] for t in wins)
    gross_loss = abs(sum(t["pnl"] for t in losses))

    return {
        "initial_capital": round(initial_capital, 2),
        "final_capital": round(final_capital, 2),
        "total_pnl": round(total_pnl, 2),
        "return_pct": round((total_pnl / initial_capital) * 100, 2) if initial_capital else 0.0,
        "total_trades": len(trades),
        "winning_trades": len(wins),
        "losing_trades": len(losses),
        "win_rate": round(len(wins) / len(trades) * 100, 2) if trades else 0.0,
        "avg_win": round(gross_profit / len(wins), 2) if wins else 0.0,
        "avg_loss": round(-gross_loss / len(losses), 2) if losses else 0.0,
        "profit_factor": round(gross_profit / gross_loss, 2) if gross_loss else None,
        "max_drawdown_pct": round(max_drawdown_pct, 2),
        "max_drawdown_bars": max_drawdown_bars,
        "sharpe_ratio": round(sharpe, 2),
        "total_charges": round(sum(t["charges"] for t in trades), 2),
        "trades": trades,
    }


def _bars_per_year(timeframe: str) -> float:
    per_day = {
        "1m": 375, "3m": 125, "5m": 75, "15m": 25, "30m": 12, "1h": 6,
        "1d": 1, "1w": 0.2,
    }.get(str(timeframe).lower(), 1)
    return TRADING_DAYS_PER_YEAR * per_day


def _period_consistency(
    df: pd.DataFrame, equity: List[float], trades: List[Dict[str, Any]],
    segments: int = 4,
) -> List[Dict[str, Any]]:
    """The same rules, measured over consecutive stretches of the range.

    A single headline return says nothing about *when* it was earned. A
    strategy that made everything in one favourable quarter and lost slowly
    through the other three reports the same total as one that worked
    throughout, and only the second is worth running tomorrow.

    Deliberately **not** walk-forward optimisation. There is no parameter
    search here, so nothing is fitted on one stretch and tested on the next —
    calling it walk-forward would claim a rigour this does not have. It is the
    same fixed rules, reported per period.
    """
    bars = len(equity)
    if bars < segments * 2 or df.empty:
        return []

    times = df["timestamp"].astype(str).tolist()
    size = bars // segments
    out: List[Dict[str, Any]] = []

    for n in range(segments):
        start = n * size
        # The last segment absorbs the remainder, so no bars are dropped.
        end = bars - 1 if n == segments - 1 else (n + 1) * size - 1
        opening, closing = equity[start], equity[end]

        first_time, last_time = times[start], times[min(end, len(times) - 1)]
        in_window = [
            t for t in trades
            if first_time <= str(t.get("exit_date", "")) <= last_time
        ]
        wins = [t for t in in_window if (t.get("pnl") or 0) > 0]

        out.append({
            "period": n + 1,
            "from": first_time[:10],
            "to": last_time[:10],
            "return_pct": round(((closing - opening) / opening) * 100, 2) if opening else None,
            "pnl": round(closing - opening, 2),
            "trades": len(in_window),
            "win_rate": round(len(wins) / len(in_window) * 100, 1) if in_window else None,
        })

    return out


def run_backtest(
    session: ChoiceSession,
    dsl_def: Dict[str, Any],
    params: Dict[str, Any],
    cost_model: Optional[CostModel] = None,
) -> Dict[str, Any]:
    """Run ``dsl_def`` over historical bars and return real metrics."""
    symbol = params.get("symbol")
    if not symbol:
        raise DSLError("A backtest needs a symbol")

    timeframe = params.get("timeframe", "1d")
    start_date = params.get("start_date")
    end_date = params.get("end_date")
    initial_capital = float(params.get("initial_capital", 100000.0))
    if initial_capital <= 0:
        raise DSLError(f"initial_capital must be positive, got {initial_capital}")

    dsl_engine.validate(dsl_def)
    costs = cost_model or CostModel()

    # Resolve the instrument explicitly; never fall back to a default token.
    instrument = resolve_instrument(
        session, symbol, params.get("segment_id"), params.get("token")
    )
    df, provenance = get_historical_ohlcv(
        session=session,
        symbol=instrument["symbol"],
        timeframe=timeframe,
        start_date=start_date,
        end_date=end_date,
        segment_id=instrument["segment_id"],
        token=instrument["token"],
    )

    df = dsl_engine.calculate_indicators(df, dsl_def.get("indicators", {}))
    entry_conds = dsl_def.get("entry_conditions", [])
    exit_conds = dsl_def.get("exit_conditions", [])
    actions = dsl_def.get("actions", {}) or {}
    order_qty = int(actions.get("buy_qty", 1))
    target_pct = actions.get("target_pct")
    stop_loss_pct = actions.get("stop_loss_pct")

    capital = initial_capital
    position_qty = 0
    entry_price = 0.0
    entry_time = ""
    pending_signal: Optional[str] = None

    trades: List[Dict[str, Any]] = []
    equity: List[float] = []
    logs: List[str] = [
        f"Backtest {instrument['symbol']} ({instrument['segment_id']}_{instrument['token']}) "
        f"{timeframe} {start_date}..{end_date}",
        f"Data source: {provenance['source']} ({len(df)} bars)",
        f"Starting capital {initial_capital:,.2f}",
    ]
    if not provenance.get("is_real_market_data"):
        logs.append(
            "WARNING: sandbox session - prices are synthetic, not exchange data."
        )

    rows = list(df.itertuples(index=False))
    columns = list(df.columns)

    def as_series(record) -> pd.Series:
        return pd.Series(record._asdict() if hasattr(record, "_asdict") else record,
                         index=columns)

    prev_row = None
    for i, record in enumerate(rows):
        row = as_series(record)
        bar_open = float(row["open"])
        bar_time = str(row["timestamp"])

        # Execute the signal raised on the previous bar, at this bar's open.
        if pending_signal == "ENTER" and position_qty == 0:
            fill = costs.fill_price(bar_open, "BUY")
            turnover = fill * order_qty
            charge = costs.charges(turnover, "BUY")
            if turnover + charge > capital:
                logs.append(
                    f"[{bar_time}] SKIP entry: needs {turnover + charge:,.2f}, "
                    f"capital {capital:,.2f}"
                )
            else:
                capital -= turnover + charge
                position_qty = order_qty
                entry_price = fill
                entry_time = bar_time
                logs.append(
                    f"[{bar_time}] BUY {order_qty} @ {fill:.2f} (charges {charge:.2f})"
                )
        elif pending_signal == "EXIT" and position_qty > 0:
            fill = costs.fill_price(bar_open, "SELL")
            turnover = fill * position_qty
            charge = costs.charges(turnover, "SELL")
            capital += turnover - charge
            entry_charge = costs.charges(entry_price * position_qty, "BUY")
            pnl = (fill - entry_price) * position_qty - charge - entry_charge
            trades.append({
                "entry_date": entry_time,
                "exit_date": bar_time,
                "side": "BUY",
                "qty": position_qty,
                "entry_price": round(entry_price, 2),
                "exit_price": round(fill, 2),
                "charges": round(charge + entry_charge, 2),
                "pnl": round(pnl, 2),
                "return_pct": round((fill - entry_price) / entry_price * 100, 2),
            })
            logs.append(
                f"[{bar_time}] SELL {position_qty} @ {fill:.2f} | net PnL {pnl:,.2f}"
            )
            position_qty = 0
            entry_price = 0.0

        pending_signal = None
        close_price = float(row["close"])

        # Evaluate signals on the closed bar; act on the next one.
        if position_qty == 0:
            if dsl_engine.evaluate_conditions_all(row, entry_conds, prev_row):
                pending_signal = "ENTER"
        else:
            hit_target = (
                target_pct is not None
                and close_price >= entry_price * (1 + float(target_pct) / 100)
            )
            hit_stop = (
                stop_loss_pct is not None
                and close_price <= entry_price * (1 - float(stop_loss_pct) / 100)
            )
            if hit_target or hit_stop:
                pending_signal = "EXIT"
                logs.append(
                    f"[{bar_time}] {'TARGET' if hit_target else 'STOP'} triggered"
                )
            elif dsl_engine.evaluate_conditions_all(row, exit_conds, prev_row):
                pending_signal = "EXIT"

        equity.append(capital + position_qty * close_price)
        prev_row = row

    # Close any open position at the final close.
    if position_qty > 0 and rows:
        final_close = float(as_series(rows[-1])["close"])
        fill = costs.fill_price(final_close, "SELL")
        turnover = fill * position_qty
        charge = costs.charges(turnover, "SELL")
        entry_charge = costs.charges(entry_price * position_qty, "BUY")
        capital += turnover - charge
        pnl = (fill - entry_price) * position_qty - charge - entry_charge
        trades.append({
            "entry_date": entry_time,
            "exit_date": str(as_series(rows[-1])["timestamp"]),
            "side": "BUY",
            "qty": position_qty,
            "entry_price": round(entry_price, 2),
            "exit_price": round(fill, 2),
            "charges": round(charge + entry_charge, 2),
            "pnl": round(pnl, 2),
            "return_pct": round((fill - entry_price) / entry_price * 100, 2),
            "closed_at_end_of_run": True,
        })
        logs.append(f"[end] Closed open position of {position_qty} @ {fill:.2f}")
        equity[-1] = capital

    if not equity:
        equity = [initial_capital]

    metrics = _metrics(equity, trades, initial_capital, _bars_per_year(timeframe))
    metrics["equity_curve"] = [round(v, 2) for v in equity]
    metrics["periods"] = _period_consistency(df, equity, trades)
    logs.append(
        f"Completed: {metrics['total_trades']} trades, "
        f"net PnL {metrics['total_pnl']:,.2f} ({metrics['return_pct']}%), "
        f"charges {metrics['total_charges']:,.2f}"
    )

    return {
        "status": "COMPLETED",
        "instrument": instrument,
        "data_source": provenance,
        "cost_model": costs.describe(),
        "metrics": metrics,
        # A plain-language reading of the metrics above, derived only from what
        # was actually computed. See verdict.py for why it is past tense.
        "verdict": verdict(metrics, timeframe),
        "logs": logs,
    }
