"""Marketstack is off the live historical path.

The unused client module remains on disk so leftover imports do not explode,
but get_historical_ohlcv must never credit MARKETSTACK as a source. Leftover
HISTORICAL_DATA_PROVIDER=MARKETSTACK is treated as AUTO (Choice then Yahoo).
"""

from unittest.mock import MagicMock, patch

import pandas as pd

from engine.app.choice_gateway.client_manager import ChoiceSession, SessionMode
from engine.app.choice_gateway.historical import get_historical_ohlcv
from engine.app.config import EngineSettings


def _paper() -> ChoiceSession:
    session = ChoiceSession("ms-user")
    session.mode = SessionMode.PAPER
    session.session_id = "sid_123"
    session.client = MagicMock()
    return session


def _yahoo_df() -> pd.DataFrame:
    return pd.DataFrame({
        "timestamp": pd.to_datetime(["2024-01-15", "2024-01-16"]),
        "open": [100.0, 101.0],
        "high": [110.0, 111.0],
        "low": [90.0, 91.0],
        "close": [105.0, 106.0],
        "volume": [1000, 1100],
    })


def test_historical_module_does_not_import_marketstack():
    import engine.app.choice_gateway.historical as hist

    assert "marketstack" not in hist.__dict__
    assert not hasattr(hist, "marketstack_client")
    assert not hasattr(hist, "MarketstackClient")


def test_leftover_marketstack_provider_falls_through_to_yahoo():
    session = _paper()
    session.client.historical.get_historical_data.return_value = pd.DataFrame()

    with patch(
        "engine.app.choice_gateway.historical.engine_settings",
        EngineSettings(HISTORICAL_DATA_PROVIDER="MARKETSTACK"),
    ):
        with patch(
            "engine.app.choice_gateway.historical._fetch_yfinance_ohlcv",
            return_value=_yahoo_df(),
        ) as yahoo:
            df, provenance = get_historical_ohlcv(
                session=session,
                symbol="RELIANCE",
                timeframe="1d",
                start_date="2024-01-01",
                end_date="2024-01-20",
                segment_id=1,
                token="2885",
            )

    assert provenance["source"] == "YFINANCE"
    assert "MARKETSTACK" not in str(provenance)
    yahoo.assert_called_once()
    assert len(df) == 2


def test_auto_provider_never_credits_marketstack():
    session = _paper()
    session.client.historical.get_historical_data.return_value = pd.DataFrame()

    with patch(
        "engine.app.choice_gateway.historical.engine_settings",
        EngineSettings(HISTORICAL_DATA_PROVIDER="AUTO"),
    ):
        with patch(
            "engine.app.choice_gateway.historical._fetch_yfinance_ohlcv",
            return_value=_yahoo_df(),
        ):
            _, provenance = get_historical_ohlcv(
                session=session,
                symbol="RELIANCE",
                timeframe="1d",
                start_date="2024-01-01",
                end_date="2024-01-20",
                segment_id=1,
                token="2885",
            )

    assert provenance["source"] == "YFINANCE"
    assert provenance["source"] != "MARKETSTACK"


def test_engine_settings_has_no_marketstack_fields():
    settings = EngineSettings()
    assert not hasattr(settings, "MARKETSTACK_API_KEY")
    assert not hasattr(settings, "MARKETSTACK_BASE_URL")
    assert settings.HISTORICAL_DATA_PROVIDER == "AUTO"
