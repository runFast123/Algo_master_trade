"""Choice OpenAPI historical data is primary; Yahoo Finance is secondary.

Marketstack is no longer on the live path. These tests pin the remaining
pipeline so a leftover MARKETSTACK env value cannot resurrect it, and so
Choice ChartData shapes (DataFrame, list, empty, missing columns) stay
mapped correctly.
"""

from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from engine.app.choice_gateway.client_manager import ChoiceSession, SessionMode
from engine.app.choice_gateway.errors import ChoiceNotConnected, ChoiceUpstreamError
from engine.app.choice_gateway.historical import (
    _validate_frame,
    choice_interval,
    get_historical_ohlcv,
)
from engine.app.config import EngineSettings


def _paper(owner: str = "hist-user") -> ChoiceSession:
    session = ChoiceSession(owner)
    session.mode = SessionMode.PAPER
    session.session_id = "sid_123"
    session.client = MagicMock()
    return session


def _choice_df(n: int = 2) -> pd.DataFrame:
    """Shape the Choice SDK actually returns: Time + OHLCV, not timestamp."""
    return pd.DataFrame({
        "Time": pd.to_datetime(["2024-01-15", "2024-01-16"][:n]),
        "Open": [2450.0, 2505.0][:n],
        "High": [2510.0, 2530.0][:n],
        "Low": [2440.0, 2490.0][:n],
        "Close": [2504.5, 2520.0][:n],
        "Volume": [1_200_000, 1_400_000][:n],
        "OI": [0, 0][:n],
    })


def _yahoo_df(n: int = 3) -> pd.DataFrame:
    return pd.DataFrame({
        "timestamp": pd.to_datetime(
            ["2024-01-15", "2024-01-16", "2024-01-17"][:n]
        ),
        "open": [100.0, 101.0, 102.0][:n],
        "high": [110.0, 111.0, 112.0][:n],
        "low": [90.0, 91.0, 92.0][:n],
        "close": [105.0, 106.0, 107.0][:n],
        "volume": [1000, 1100, 1200][:n],
    })


def _fetch(**kwargs):
    defaults = dict(
        symbol="RELIANCE",
        timeframe="1d",
        start_date="2024-01-01",
        end_date="2024-01-20",
        segment_id=1,
        token="2885",
    )
    defaults.update(kwargs)
    return get_historical_ohlcv(**defaults)


# -- interval mapping -------------------------------------------------------


@pytest.mark.parametrize("ours,theirs", [
    ("1m", "1"), ("5m", "5"), ("15m", "15"), ("1h", "60"),
    ("1d", "D"), ("1w", "W"),
])
def test_choice_interval_uses_tradingview_codes(ours, theirs):
    """Sending '1d' verbatim was silently wrong for every real-data backtest."""
    assert choice_interval(ours) == theirs


def test_unsupported_timeframe_is_refused():
    with pytest.raises(ChoiceUpstreamError, match="Unsupported timeframe"):
        choice_interval("2d")


# -- frame validation -------------------------------------------------------


def test_validate_frame_renames_choice_time_column():
    df = _validate_frame(_choice_df())
    assert df is not None
    assert "timestamp" in df.columns
    assert "close" in df.columns
    assert len(df) == 2


def test_validate_frame_accepts_list_of_dicts():
    raw = [
        {"timestamp": "2024-01-15", "open": 1, "high": 2, "low": 0.5,
         "close": 1.5, "volume": 10},
        {"timestamp": "2024-01-16", "open": 1.5, "high": 2.5, "low": 1,
         "close": 2, "volume": 20},
    ]
    df = _validate_frame(raw)
    assert df is not None
    assert len(df) == 2


def test_validate_frame_rejects_empty_dataframe():
    assert _validate_frame(pd.DataFrame()) is None


def test_validate_frame_rejects_missing_columns():
    raw = pd.DataFrame({"Time": ["2024-01-15"], "Close": [100.0]})
    assert _validate_frame(raw) is None


# -- Choice success ---------------------------------------------------------


def test_choice_dataframe_is_primary_source():
    session = _paper()
    session.client.historical.get_historical_data.return_value = _choice_df()

    with patch("engine.app.choice_gateway.historical.engine_settings",
               EngineSettings(HISTORICAL_DATA_PROVIDER="AUTO")):
        df, provenance = _fetch(session=session)

    assert len(df) == 2
    assert provenance["source"] == "CHOICE_OPENAPI"
    assert provenance["is_real_market_data"] is True
    assert provenance["bars"] == 2
    session.client.historical.get_historical_data.assert_called_once()
    kwargs = session.client.historical.get_historical_data.call_args.kwargs
    assert kwargs["segment_id"] == 1
    assert kwargs["token"] == 2885
    assert kwargs["resolution"] == "D"


def test_choice_list_payload_is_accepted():
    session = _paper()
    session.client.historical.get_historical_data.return_value = [
        {"timestamp": "2024-01-15", "open": 1, "high": 2, "low": 0.5,
         "close": 1.5, "volume": 10},
    ]

    df, provenance = _fetch(session=session)
    assert len(df) == 1
    assert provenance["source"] == "CHOICE_OPENAPI"


def test_no_token_is_refused_before_any_network_call():
    session = _paper()
    with pytest.raises(ChoiceUpstreamError, match="No instrument token"):
        _fetch(session=session, token="")
    session.client.historical.get_historical_data.assert_not_called()


def test_disconnected_session_cannot_fetch_history():
    session = ChoiceSession("offline")
    assert session.mode is SessionMode.DISCONNECTED
    with pytest.raises(ChoiceNotConnected):
        _fetch(session=session)


def test_demo_uses_sandbox_and_never_calls_choice():
    session = ChoiceSession("demo-user")
    session.start_demo("DEMO")
    session.client = MagicMock()

    df, provenance = _fetch(session=session)
    assert not df.empty
    assert provenance["source"] == "SANDBOX_SYNTHETIC"
    assert provenance["is_real_market_data"] is False
    session.client.historical.get_historical_data.assert_not_called()


# -- Yahoo fallback ---------------------------------------------------------


def test_empty_choice_falls_back_to_yahoo_on_auto():
    session = _paper()
    session.client.historical.get_historical_data.return_value = pd.DataFrame()

    with patch("engine.app.choice_gateway.historical.engine_settings",
               EngineSettings(HISTORICAL_DATA_PROVIDER="AUTO")):
        with patch("engine.app.choice_gateway.historical._fetch_yfinance_ohlcv",
                   return_value=_yahoo_df()) as yahoo:
            df, provenance = _fetch(session=session)

    assert provenance["source"] == "YFINANCE"
    assert len(df) == 3
    yahoo.assert_called_once()


def test_choice_401_falls_back_to_yahoo_on_auto():
    session = _paper()
    session.client.historical.get_historical_data.side_effect = Exception(
        "401 Unauthorized"
    )

    with patch("engine.app.choice_gateway.historical.engine_settings",
               EngineSettings(HISTORICAL_DATA_PROVIDER="AUTO")):
        with patch("engine.app.choice_gateway.historical._fetch_yfinance_ohlcv",
                   return_value=_yahoo_df()):
            df, provenance = _fetch(session=session)

    assert provenance["source"] == "YFINANCE"
    assert provenance["is_real_market_data"] is True


def test_choice_provider_does_not_fall_back_to_yahoo():
    session = _paper()
    session.client.historical.get_historical_data.side_effect = Exception(
        "401 Unauthorized"
    )

    with patch("engine.app.choice_gateway.historical.engine_settings",
               EngineSettings(HISTORICAL_DATA_PROVIDER="CHOICE")):
        with patch("engine.app.choice_gateway.historical._fetch_yfinance_ohlcv") as yahoo:
            with pytest.raises(ChoiceUpstreamError, match="Could not fetch historical"):
                _fetch(session=session)

    yahoo.assert_not_called()


def test_yahoo_empty_after_choice_failure_still_raises():
    session = _paper()
    session.client.historical.get_historical_data.return_value = pd.DataFrame()

    with patch("engine.app.choice_gateway.historical.engine_settings",
               EngineSettings(HISTORICAL_DATA_PROVIDER="AUTO")):
        with patch("engine.app.choice_gateway.historical._fetch_yfinance_ohlcv",
                   return_value=None):
            with pytest.raises(ChoiceUpstreamError, match="no usable historical"):
                _fetch(session=session)


def test_leftover_marketstack_provider_is_treated_as_auto():
    """A leftover .env MARKETSTACK value must not call Marketstack."""
    session = _paper()
    session.client.historical.get_historical_data.return_value = pd.DataFrame()

    with patch("engine.app.choice_gateway.historical.engine_settings",
               EngineSettings(HISTORICAL_DATA_PROVIDER="MARKETSTACK")):
        with patch("engine.app.choice_gateway.historical._fetch_yfinance_ohlcv",
                   return_value=_yahoo_df()):
            df, provenance = _fetch(session=session)

    assert provenance["source"] == "YFINANCE"
    assert "MARKETSTACK" not in str(provenance)


def test_marketstack_module_is_not_imported_by_historical():
    import engine.app.choice_gateway.historical as hist
    assert not hasattr(hist, "marketstack_client")
    assert "marketstack" not in hist.__dict__


def test_yahoo_suffix_for_nse_cash():
    captured = {}

    class FakeTicker:
        pass

    def fake_download(symbol, start=None, end=None, interval=None, progress=None):
        captured["symbol"] = symbol
        captured["interval"] = interval
        idx = pd.date_range("2024-01-15", periods=2, freq="D")
        return pd.DataFrame({
            "Open": [1.0, 2.0], "High": [1.5, 2.5], "Low": [0.5, 1.5],
            "Close": [1.2, 2.2], "Volume": [10, 20],
        }, index=idx)

    session = _paper()
    session.client.historical.get_historical_data.return_value = pd.DataFrame()

    with patch("engine.app.choice_gateway.historical.engine_settings",
               EngineSettings(HISTORICAL_DATA_PROVIDER="AUTO")):
        with patch("engine.app.choice_gateway.historical.yf") as yf_mod:
            yf_mod.download.side_effect = fake_download
            df, provenance = _fetch(session=session)

    assert captured["symbol"] == "RELIANCE.NS"
    assert captured["interval"] == "1d"
    assert provenance["source"] == "YFINANCE"
    assert len(df) == 2
