"""Historical OHLCV, scoped to a single user's Choice session.

A live session always returns exchange data; if Choice cannot supply it the
call fails rather than silently substituting a generated series. Synthetic
data is produced only for sandbox sessions and is labelled as such, so a
backtest can never present made-up prices as real ones.
"""

import logging
import zlib
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import numpy as np

from engine.app.config import engine_settings
from . import marketstack as marketstack_client
from .client_manager import ChoiceSession, SessionMode
from .errors import ChoiceNotConnected, ChoiceUpstreamError

logger = logging.getLogger("choice_gateway.historical")

try:
    import yfinance as yf
except ImportError:
    yf = None

REQUIRED_COLUMNS = ("timestamp", "open", "high", "low", "close", "volume")

# Reference opening levels used only to seed sandbox series.
_SANDBOX_BASE_PRICES = {
    "RELIANCE": 2500.0,
    "TCS": 4120.0,
    "INFY": 1540.0,
    "HDFCBANK": 1650.0,
    "NIFTY 50": 24326.0,
    "BANKNIFTY": 52140.0,
}


# Choice's chart endpoint takes TradingView-style intervals: bare minutes as
# numbers, "D" for daily, "W" for weekly. Our own vocabulary is "1m".."1w",
# which the API does not understand — sending it verbatim was silently wrong
# for every real-data backtest.
CHOICE_INTERVALS = {
    "1m": "1", "3m": "3", "5m": "5", "15m": "15", "30m": "30",
    "1h": "60", "1d": "D", "1w": "W",
}


def choice_interval(timeframe: str) -> str:
    """Map our timeframe vocabulary to the interval Choice expects."""
    key = str(timeframe).lower()
    if key not in CHOICE_INTERVALS:
        raise ChoiceUpstreamError(
            f"Unsupported timeframe {timeframe!r}. "
            f"Supported: {', '.join(sorted(CHOICE_INTERVALS))}"
        )
    return CHOICE_INTERVALS[key]


def _frequency_for(timeframe: str) -> str:
    mapping = {
        "1m": "1min", "3m": "3min", "5m": "5min", "15m": "15min",
        "30m": "30min", "1h": "1h", "1d": "1D", "1w": "1W",
    }
    return mapping.get(str(timeframe).lower(), "1D")


def generate_sandbox_ohlcv(
    symbol: str, timeframe: str, start_date: str, end_date: str
) -> pd.DataFrame:
    """Deterministic synthetic series for sandbox backtests."""
    freq = _frequency_for(timeframe)
    index = pd.date_range(start=start_date, end=end_date, freq=freq)
    if len(index) == 0:
        index = pd.date_range(end=datetime.now(), periods=100, freq=freq)

    n = len(index)
    seed = zlib.adler32(f"{symbol}_{timeframe}_{start_date}_{end_date}".encode()) % (2 ** 32)
    rng = np.random.default_rng(seed)

    base_price = _SANDBOX_BASE_PRICES.get(str(symbol).upper(), 1500.0)
    returns = rng.normal(loc=0.0005, scale=0.015, size=n)
    closes = base_price * np.exp(np.cumsum(returns))
    highs = closes * (1 + np.abs(rng.normal(0, 0.008, n)))
    lows = closes * (1 - np.abs(rng.normal(0, 0.008, n)))
    opens = closes * (1 + rng.normal(0, 0.005, n))

    return pd.DataFrame({
        "timestamp": index,
        "open": opens,
        "high": np.maximum.reduce([highs, opens, closes]),
        "low": np.minimum.reduce([lows, opens, closes]),
        "close": closes,
        "volume": rng.integers(10000, 500000, size=n),
    })


def _validate_frame(raw: Any) -> Optional[pd.DataFrame]:
    """Ensure the DataFrame has the columns the backtester requires."""
    if isinstance(raw, pd.DataFrame):
        if raw.empty:
            return None
        df = raw.copy()
        df.columns = [str(c).lower() for c in df.columns]
        time_cols = [c for c in df.columns if c in ("time", "date", "datetime", "timestamp")]
        if time_cols and "timestamp" not in df.columns:
            df = df.rename(columns={time_cols[0]: "timestamp"})
    else:
        if not raw:
            return None
        df = pd.DataFrame(raw)
        df.columns = [str(c).lower() for c in df.columns]

    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        logger.warning("Choice historical data missing columns: %s", missing)
        return None
    return df


def get_historical_ohlcv(
    session: ChoiceSession,
    symbol: str,
    timeframe: str,
    start_date: str,
    end_date: str,
    segment_id: int,
    token: str,
) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    """Return ``(dataframe, provenance)`` for the requested instrument.

    ``segment_id`` and ``token`` are required: resolving them is the caller's
    job, so a backtest can never quietly run against a default instrument.
    """
    if not token:
        raise ChoiceUpstreamError(
            f"No instrument token supplied for {symbol!r}; cannot fetch history."
        )

    provenance = {
        "symbol": symbol,
        "segment_id": int(segment_id),
        "token": str(token),
        "timeframe": timeframe,
    }

    if session.is_demo:
        df = generate_sandbox_ohlcv(symbol, timeframe, start_date, end_date)
        provenance.update(source="SANDBOX_SYNTHETIC", is_real_market_data=False)
        return df, provenance

    provider = (engine_settings.HISTORICAL_DATA_PROVIDER or "AUTO").upper()

    # 1. Explicit Marketstack provider
    if provider == "MARKETSTACK":
        df, m_prov = marketstack_client.get_historical_ohlcv(
            symbol=symbol,
            timeframe=timeframe,
            start_date=start_date,
            end_date=end_date,
        )
        provenance.update(m_prov)
        return df, provenance

    # 2. Try Choice OpenAPI first
    if session.mode is SessionMode.DISCONNECTED and provider != "MARKETSTACK":
        raise ChoiceNotConnected(
            "Not signed in to Choice FINX. Connect your Choice account first."
        )

    choice_err = None
    df = None
    try:
        client = session.require_client()
        raw = client.historical.get_historical_data(
            segment_id=int(segment_id),
            token=int(token),
            from_date=start_date,
            to_date=end_date,
            resolution=choice_interval(timeframe),
        )
        df = _validate_frame(raw)
    except Exception as exc:
        choice_err = exc

    if df is not None:
        provenance.update(source="CHOICE_OPENAPI", is_real_market_data=True, bars=len(df))
        return df, provenance

    # 3. Fallback to Marketstack if Choice is unavailable and Marketstack is configured
    # We create an instance to check configuration because marketstack_client acts
    # as the module locally, but it exports the MarketstackClient class
    ms_client = marketstack_client.MarketstackClient()
    if provider == "AUTO" and ms_client.is_configured:
        logger.info(
            "Choice historical data unavailable (%s); falling back to Marketstack for %s",
            choice_err or "empty/invalid response", symbol,
        )
        try:
            df, m_prov = ms_client.get_historical_ohlcv(
                symbol=symbol,
                timeframe=timeframe,
                start_date=start_date,
                end_date=end_date,
            )
            provenance.update(m_prov)
            return df, provenance
        except Exception as ms_exc:
            logger.warning("Marketstack fallback also failed for %s: %s", symbol, ms_exc)

    # 4. Fallback to yfinance if Choice and Marketstack both fail
    if provider in ("AUTO", "YFINANCE"):
        logger.info("Falling back to yfinance for %s (Choice err: %s)", symbol, choice_err)
        try:
            df = _fetch_yfinance_ohlcv(symbol, timeframe, start_date, end_date, segment_id)
            if df is not None and not df.empty:
                provenance.update(source="YFINANCE", is_real_market_data=True, bars=len(df))
                return df, provenance
        except Exception as yf_exc:
            logger.warning("yfinance fallback failed for %s: %s", symbol, yf_exc)

    if choice_err:
        raise ChoiceUpstreamError(
            f"Could not fetch historical data for {symbol} (token {token})", str(choice_err)
        ) from choice_err

    raise ChoiceUpstreamError(
        f"Choice returned no usable historical data for {symbol} "
        f"(token {token}, {start_date} to {end_date})"
    )

