"""Marketstack API v2 historical market data client.

Alternative historical bars for backtesting when Choice OpenAPI chart data is
unavailable — which, for this account, it is: ``OpenGraph/ChartData`` returns
401 on a session that reads holdings and funds fine.

**Only bars Marketstack actually returned.** Intraday timeframes come from the
intraday endpoint or not at all. An earlier version answered an intraday
request by expanding daily bars into a modelled trajectory and labelling it
``is_real_market_data: True``; the shape was smooth by construction, so a
strategy could score well against a price path that never happened. If the plan
is end-of-day only, the caller is told to run the backtest daily instead.
"""

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import requests

from engine.app.config import engine_settings
from engine.app.choice_gateway.errors import ChoiceUpstreamError

logger = logging.getLogger("marketstack")

# Marketstack intraday interval mapping from our platform vocabulary
MARKETSTACK_INTRADAY_INTERVALS = {
    "1m": "1min",
    "3m": "1min",
    "5m": "5min",
    "15m": "15min",
    "30m": "30min",
    "1h": "1hour",
}

EOD_TIMEFRAMES = {"1d", "1w"}


class MarketstackError(ChoiceUpstreamError):
    """Failure communicating with Marketstack API."""


def _foreign_listing(rows: List[Dict[str, Any]]) -> Optional[str]:
    """Describe a non-Indian listing, or None when the rows look Indian.

    Marketstack returns `price_currency` and `exchange` on every row and this
    module used to discard both. They are the only evidence available that a
    symbol resolved to the instrument that was actually meant.
    """
    for row in rows[:3]:
        if not isinstance(row, dict):
            continue
        currency = str(row.get("price_currency") or "").strip().upper()
        if currency and currency != "INR":
            exchange = str(row.get("exchange") or "?")
            return f"{exchange} in {currency}"
    return None


class MarketstackClient:
    """Client for Marketstack REST API v2."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        timeout: float = 15.0,
    ):
        if api_key is not None:
            self.api_key = api_key
        else:
            self.api_key = engine_settings.MARKETSTACK_API_KEY
        self.base_url = (base_url or engine_settings.MARKETSTACK_BASE_URL).rstrip("/")
        self.timeout = timeout

    @property
    def is_configured(self) -> bool:
        return bool(self.api_key and self.api_key.strip())

    def _request(self, path: str, params: Dict[str, Any]) -> Dict[str, Any]:
        if not self.is_configured:
            raise MarketstackError(
                "Marketstack API key is not configured. "
                "Set MARKETSTACK_API_KEY in your .env to use Marketstack data."
            )

        url = f"{self.base_url}/{path.lstrip('/')}"
        query = {
            "access_key": self.api_key,
            **params,
        }

        try:
            resp = requests.get(url, params=query, timeout=self.timeout)
        except requests.RequestException as exc:
            raise MarketstackError(
                f"Marketstack network error: {exc}"
            ) from exc

        try:
            data = resp.json()
        except ValueError:
            raise MarketstackError(
                f"Marketstack returned invalid non-JSON response ({resp.status_code})"
            )

        if not resp.ok or data.get("error"):
            err = data.get("error") or {}
            message = err.get("message") or err.get("info") or f"HTTP {resp.status_code}"
            code = err.get("code", resp.status_code)
            raise MarketstackError(
                f"Marketstack API error ({code}): {message}"
            )

        return data

    def _paginate(self, path: str, params: Dict[str, Any], want: int) -> List[Dict[str, Any]]:
        """Collect up to ``want`` rows, following Marketstack's paging.

        One call returns at most 1000 rows. Without paging, that ceiling is a
        silent truncation of the *date range* the caller asked for: 1000 daily
        bars is four years, but 1000 five-minute bars is thirteen trading days,
        so a month-long intraday backtest quietly measured a fortnight and
        reported it as the whole period.
        """
        collected: List[Dict[str, Any]] = []
        offset = 0
        while len(collected) < want:
            page_size = min(1000, want - len(collected))
            payload = self._request(path, {**params, "limit": page_size, "offset": offset})
            rows = payload.get("data") or []
            collected.extend(rows)

            pagination = payload.get("pagination") or {}
            total = pagination.get("total")
            # Stop on a short page, on reaching the reported total, or when the
            # API stops advancing — any of which means there is no more data.
            if not rows or len(rows) < page_size:
                break
            if isinstance(total, int) and len(collected) >= total:
                break
            offset += len(rows)

        return collected

    def _format_date(self, date_val: Any) -> Optional[str]:
        if not date_val:
            return None
        if isinstance(date_val, datetime):
            return date_val.strftime("%Y-%m-%d")
        d_str = str(date_val).strip()
        if "T" in d_str:
            d_str = d_str.split("T")[0]
        elif " " in d_str:
            d_str = d_str.split(" ")[0]
        return d_str

    def fetch_eod(
        self,
        symbol: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        exchange: Optional[str] = None,
        limit: int = 50_000,
    ) -> List[Dict[str, Any]]:
        """Fetch End-of-Day bars for a symbol."""
        params: Dict[str, Any] = {
            "symbols": symbol,
            "sort": "ASC",
        }
        d_from = self._format_date(start_date)
        d_to = self._format_date(end_date)
        if d_from:
            params["date_from"] = d_from
        if d_to:
            params["date_to"] = d_to
        if exchange:
            params["exchange"] = exchange

        return self._paginate("v2/eod", params, want=limit)

    def fetch_intraday(
        self,
        symbol: str,
        timeframe: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        exchange: Optional[str] = None,
        limit: int = 50_000,
    ) -> List[Dict[str, Any]]:
        """Fetch Intraday bars for a symbol."""
        interval = MARKETSTACK_INTRADAY_INTERVALS.get(timeframe.lower(), "1hour")
        params: Dict[str, Any] = {
            "symbols": symbol,
            "interval": interval,
            "sort": "ASC",
        }
        d_from = self._format_date(start_date)
        d_to = self._format_date(end_date)
        if d_from:
            params["date_from"] = d_from
        if d_to:
            params["date_to"] = d_to
        if exchange:
            params["exchange"] = exchange

        return self._paginate("v2/intraday", params, want=limit)

    def get_historical_ohlcv(
        self,
        symbol: str,
        timeframe: str = "1d",
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        exchange: Optional[str] = None,
    ) -> Tuple[pd.DataFrame, Dict[str, Any]]:
        """Fetch and normalize historical OHLCV data from Marketstack into a DataFrame."""
        tf = timeframe.lower()
        symbol_clean = symbol.strip().upper()

        # Try symbol candidate formats (e.g. RELIANCE.NS, RELIANCE.BO, RELIANCE)
        candidates = []
        if "." in symbol_clean:
            candidates.append(symbol_clean)
        else:
            # Indian listings only. The bare ticker used to be the last
            # candidate, and Marketstack resolves a bare "INFY" to the New York
            # ADR at about 11.62 USD rather than Infosys at 1139.90 INR, and a
            # bare "IEX" to Idex Corporation — a different company entirely.
            # A backtest scored against a US instrument in dollars, while the
            # holdings it was sized from were in rupees, is wrong in a way
            # nothing downstream could detect.
            candidates.extend([
                f"{symbol_clean}.NS",
                f"{symbol_clean}.BO",
                f"{symbol_clean}.XNSE",
                f"{symbol_clean}.XBOM",
            ])

        data: List[Dict[str, Any]] = []
        used_symbol = symbol_clean
        last_error: Optional[MarketstackError] = None

        for cand in candidates:
            try:
                if tf in EOD_TIMEFRAMES:
                    data = self.fetch_eod(
                        cand, start_date=start_date, end_date=end_date, exchange=exchange
                    )
                else:
                    # No EOD substitution here. An earlier version answered an
                    # intraday request with daily bars expanded into a modelled
                    # trajectory and reported it as real market data. The shape
                    # was smooth by construction — one day came out with three
                    # direction changes and identical volume in every bar — so a
                    # momentum strategy scored brilliantly against a path that
                    # never existed. A backtest that cannot be run is recoverable;
                    # one that is quietly wrong is not.
                    data = self.fetch_intraday(
                        cand, timeframe=tf, start_date=start_date, end_date=end_date,
                        exchange=exchange,
                    )
                if data:
                    # The rows carry their own currency and exchange. Checking
                    # is the difference between "this symbol resolved" and
                    # "this symbol resolved to the instrument we meant".
                    wrong = _foreign_listing(data)
                    if wrong:
                        logger.warning(
                            "Marketstack candidate %s resolved to %s; skipping.",
                            cand, wrong)
                        data = []
                        continue
                    used_symbol = cand
                    break
            except MarketstackError as exc:
                last_error = exc
                logger.debug("Marketstack candidate %s failed: %s", cand, exc)
                continue

        if not data:
            if tf not in EOD_TIMEFRAMES:
                raise MarketstackError(
                    f"Marketstack has no {timeframe} data for {symbol}. Intraday bars "
                    "need a Marketstack plan that includes the intraday endpoint; on an "
                    "end-of-day plan, run this backtest on a Daily timeframe instead."
                    + (f" (last error: {last_error})" if last_error else "")
                )
            raise MarketstackError(
                f"Marketstack returned no historical data for {symbol} "
                f"({timeframe}, {start_date} to {end_date})"
                + (f" (last error: {last_error})" if last_error else "")
            )

        # Convert to standardized DataFrame
        df = self._normalize_records(data)
        provenance = {
            "symbol": symbol,
            "queried_symbol": used_symbol,
            "timeframe": timeframe,
            "source": "MARKETSTACK",
            "is_real_market_data": True,
            "bars": len(df),
        }

        # State the window actually covered, not the one requested. The plan
        # holds about a year of history, so a three-year backtest returned one
        # year and reported success — with the Sharpe, the drawdown and the win
        # rate all computed over a period nobody asked for, and nothing on
        # screen saying so.
        if len(df):
            covered_from = df["timestamp"].min()
            covered_to = df["timestamp"].max()
            provenance["covered_from"] = str(covered_from.date())
            provenance["covered_to"] = str(covered_to.date())
            requested_from = self._format_date(start_date)
            if requested_from and str(covered_from.date()) > requested_from:
                provenance["truncated"] = True
                provenance["requested_from"] = requested_from
                provenance["note"] = (
                    f"Marketstack returned history from {covered_from.date()}, not "
                    f"{requested_from}. This plan does not reach further back, so "
                    "the results below cover the shorter period."
                )
                logger.warning(
                    "Backtest window truncated for %s: asked from %s, got from %s.",
                    symbol, requested_from, covered_from.date(),
                )
        return df, provenance

    @staticmethod
    def _normalize_records(records: List[Dict[str, Any]]) -> pd.DataFrame:
        """Convert Marketstack raw data items into standard OHLCV DataFrame."""
        rows = []
        for r in records:
            dt_raw = r.get("date")
            if not dt_raw:
                continue
            try:
                # Handle ISO format dates
                dt = pd.to_datetime(dt_raw)
                if getattr(dt, "tzinfo", None) is not None:
                    dt = dt.tz_localize(None)
            except Exception:
                continue

            open_p = r.get("open")
            high_p = r.get("high")
            low_p = r.get("low")
            close_p = r.get("close")
            vol = r.get("volume", 0)

            # Fallback to adj_* if unadjusted are missing
            if open_p is None:
                open_p = r.get("adj_open", 0.0)
            if high_p is None:
                high_p = r.get("adj_high", open_p)
            if low_p is None:
                low_p = r.get("adj_low", open_p)
            if close_p is None:
                close_p = r.get("adj_close", open_p)

            rows.append({
                "timestamp": dt,
                "open": float(open_p or 0.0),
                "high": float(high_p or 0.0),
                "low": float(low_p or 0.0),
                "close": float(close_p or 0.0),
                "volume": float(vol or 0),
            })

        if not rows:
            raise MarketstackError("Marketstack payload contained no valid OHLCV records")

        df = pd.DataFrame(rows)
        df = df.sort_values("timestamp").drop_duplicates(subset=["timestamp"]).reset_index(drop=True)
        return df


marketstack_client = MarketstackClient()
