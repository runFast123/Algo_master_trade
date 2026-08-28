"""Tests for Marketstack historical data client and fallback."""

from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from engine.app.choice_gateway.client_manager import ChoiceSession, SessionMode
from engine.app.choice_gateway.historical import get_historical_ohlcv
from engine.app.choice_gateway.marketstack import (
    MarketstackClient,
    MarketstackError,
    marketstack_client,
)
from engine.app.config import EngineSettings


SAMPLE_EOD_RESPONSE = {
    "pagination": {"limit": 100, "offset": 0, "count": 2, "total": 2},
    "data": [
        {
            "open": 2450.0,
            "high": 2510.0,
            "low": 2440.0,
            "close": 2504.5,
            "volume": 1200000,
            "adj_high": 2510.0,
            "adj_low": 2440.0,
            "adj_close": 2504.5,
            "adj_open": 2450.0,
            "adj_volume": 1200000,
            "symbol": "RELIANCE.XNSE",
            "exchange": "XNSE",
            "date": "2024-01-15T00:00:00+0000",
        },
        {
            "open": 2505.0,
            "high": 2530.0,
            "low": 2490.0,
            "close": 2520.0,
            "volume": 1400000,
            "adj_high": 2530.0,
            "adj_low": 2490.0,
            "adj_close": 2520.0,
            "adj_open": 2505.0,
            "adj_volume": 1400000,
            "symbol": "RELIANCE.XNSE",
            "exchange": "XNSE",
            "date": "2024-01-16T00:00:00+0000",
        },
    ],
}

SAMPLE_INTRADAY_RESPONSE = {
    "pagination": {"limit": 100, "offset": 0, "count": 2, "total": 2},
    "data": [
        {
            "open": 2450.0,
            "high": 2465.0,
            "low": 2448.0,
            "close": 2460.0,
            "volume": 50000,
            "symbol": "RELIANCE.XNSE",
            "exchange": "XNSE",
            "date": "2024-01-15T09:15:00+0000",
        },
        {
            "open": 2460.0,
            "high": 2470.0,
            "low": 2455.0,
            "close": 2468.0,
            "volume": 60000,
            "symbol": "RELIANCE.XNSE",
            "exchange": "XNSE",
            "date": "2024-01-15T09:20:00+0000",
        },
    ],
}


def test_marketstack_client_not_configured():
    client = MarketstackClient(api_key="")
    assert not client.is_configured
    with pytest.raises(MarketstackError, match="API key is not configured"):
        client.fetch_eod("RELIANCE")


def test_marketstack_fetch_eod_success():
    client = MarketstackClient(api_key="test_key")
    with patch("requests.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = SAMPLE_EOD_RESPONSE
        mock_get.return_value = mock_resp

        data = client.fetch_eod("RELIANCE.XNSE", start_date="2024-01-01", end_date="2024-01-20")
        assert len(data) == 2
        assert data[0]["close"] == 2504.5

        mock_get.assert_called_once()
        call_params = mock_get.call_args[1]["params"]
        assert call_params["access_key"] == "test_key"
        assert call_params["symbols"] == "RELIANCE.XNSE"
        assert call_params["date_from"] == "2024-01-01"
        assert call_params["date_to"] == "2024-01-20"


def test_marketstack_fetch_intraday_interval_mapping():
    client = MarketstackClient(api_key="test_key")
    with patch("requests.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = SAMPLE_INTRADAY_RESPONSE
        mock_get.return_value = mock_resp

        data = client.fetch_intraday("RELIANCE.XNSE", timeframe="5m")
        assert len(data) == 2
        call_params = mock_get.call_args[1]["params"]
        assert call_params["interval"] == "5min"


def test_marketstack_normalize_records():
    client = MarketstackClient(api_key="test_key")
    df = client._normalize_records(SAMPLE_EOD_RESPONSE["data"])
    assert isinstance(df, pd.DataFrame)
    assert list(df.columns) == ["timestamp", "open", "high", "low", "close", "volume"]
    assert len(df) == 2
    assert df["close"].iloc[0] == 2504.5
    assert df["open"].iloc[1] == 2505.0


def test_marketstack_api_error_handling():
    client = MarketstackClient(api_key="bad_key")
    with patch("requests.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.ok = False
        mock_resp.status_code = 401
        mock_resp.json.return_value = {
            "error": {
                "code": 101,
                "type": "invalid_access_key",
                "info": "You have not supplied a valid API Access Key.",
            }
        }
        mock_get.return_value = mock_resp

        with pytest.raises(MarketstackError, match="101"):
            client.fetch_eod("RELIANCE")


def test_get_historical_ohlcv_with_marketstack_provider():
    session = ChoiceSession("user_1")
    session.mode = SessionMode.PAPER
    session.session_id = "sid_123"
    session.client = MagicMock()

    with patch("engine.app.choice_gateway.historical.engine_settings",
               EngineSettings(HISTORICAL_DATA_PROVIDER="MARKETSTACK")):
        with patch("engine.app.choice_gateway.historical.marketstack_client.fetch_eod",
                   return_value=SAMPLE_EOD_RESPONSE["data"]):
            df, provenance = get_historical_ohlcv(
                session=session,
                symbol="RELIANCE",
                timeframe="1d",
                start_date="2024-01-01",
                end_date="2024-01-20",
                segment_id=1,
                token="2885",
            )
            assert len(df) == 2
            assert provenance["source"] == "MARKETSTACK"
            assert provenance["is_real_market_data"] is True


def test_get_historical_ohlcv_auto_fallback_to_marketstack():
    session = ChoiceSession("user_1")
    session.mode = SessionMode.PAPER
    session.session_id = "sid_123"
    fake_client = MagicMock()
    # Simulate Choice 401 on ChartData
    fake_client.historical.get_historical_data.side_effect = Exception("401 Unauthorized")
    session.client = fake_client

    with patch("engine.app.choice_gateway.historical.engine_settings",
               EngineSettings(HISTORICAL_DATA_PROVIDER="AUTO", MARKETSTACK_API_KEY="test_key")):
        with patch.object(marketstack_client, "api_key", "test_key"):
            with patch("engine.app.choice_gateway.historical.marketstack_client.fetch_eod",
                       return_value=SAMPLE_EOD_RESPONSE["data"]):
                df, provenance = get_historical_ohlcv(
                    session=session,
                    symbol="RELIANCE",
                    timeframe="1d",
                    start_date="2024-01-01",
                    end_date="2024-01-20",
                    segment_id=1,
                    token="2885",
                )
                assert len(df) == 2
                assert provenance["source"] == "MARKETSTACK"


# -- intraday is never substituted with daily bars -------------------------
#
# The client used to answer an intraday request, when the plan had no intraday
# endpoint, by expanding daily bars into a modelled trajectory and reporting it
# as real market data. The path was smooth by construction, so a strategy could
# post an excellent result against prices that never happened. These tests exist
# so that cannot come back quietly.

def _client_where_only_eod_works():
    client = MarketstackClient(api_key="test_key")

    def fake_get(url, params=None, timeout=None):
        resp = MagicMock()
        if "intraday" in url:
            resp.ok = False
            resp.status_code = 403
            resp.json.return_value = {
                "error": {"code": "function_access_restricted",
                          "message": "Your subscription plan does not support this endpoint"}
            }
        else:
            resp.ok = True
            resp.status_code = 200
            resp.json.return_value = SAMPLE_EOD_RESPONSE
        return resp

    return client, fake_get


def test_an_eod_only_plan_refuses_intraday_instead_of_inventing_it():
    client, fake_get = _client_where_only_eod_works()
    with patch("requests.get", side_effect=fake_get):
        with pytest.raises(MarketstackError) as excinfo:
            client.get_historical_ohlcv("RELIANCE", timeframe="15m",
                                        start_date="2024-01-01", end_date="2024-01-20")

    message = str(excinfo.value)
    assert "Daily timeframe" in message, "the refusal must name the way forward"
    assert "15m" in message


def test_daily_still_works_on_the_same_plan():
    """The refusal is specific to intraday: EOD is real data and must be served."""
    client, fake_get = _client_where_only_eod_works()
    with patch("requests.get", side_effect=fake_get):
        df, provenance = client.get_historical_ohlcv(
            "RELIANCE", timeframe="1d", start_date="2024-01-01", end_date="2024-01-20")

    assert len(df) == 2
    assert provenance["source"] == "MARKETSTACK"
    assert provenance["is_real_market_data"] is True


def test_nothing_reports_bars_it_did_not_receive():
    """Whatever the timeframe, the bar count returned must be the bar count the
    API actually supplied — the synthesis turned 2 daily bars into 50."""
    client = MarketstackClient(api_key="test_key")
    with patch("requests.get") as mock_get:
        resp = MagicMock()
        resp.ok = True
        resp.json.return_value = SAMPLE_EOD_RESPONSE
        mock_get.return_value = resp
        df, provenance = client.get_historical_ohlcv("RELIANCE", timeframe="1d")

    assert provenance["bars"] == len(df) == len(SAMPLE_EOD_RESPONSE["data"])


def test_the_synthesiser_is_gone():
    """A named guard against reintroduction: this is the one function whose
    output was indistinguishable from real data to everything downstream."""
    assert not hasattr(MarketstackClient, "_synthesize_intraday_from_eod")


# -- paging: the 1000-row ceiling was a silent range truncation -------------

def _page_server(total: int):
    """A Marketstack that holds `total` daily bars and serves 1000 at a time."""
    calls = []

    def fake_get(url, params=None, timeout=None):
        offset = int(params.get("offset", 0))
        limit = int(params.get("limit", 1000))
        calls.append((offset, limit))
        rows = [
            {"date": "2020-01-01T00:00:00+0000", "open": 100.0 + i, "high": 101.0 + i,
             "low": 99.0 + i, "close": 100.5 + i, "volume": 1000, "symbol": "X"}
            for i in range(offset, min(offset + limit, total))
        ]
        resp = MagicMock()
        resp.ok = True
        resp.status_code = 200
        resp.json.return_value = {
            "pagination": {"limit": limit, "offset": offset,
                           "count": len(rows), "total": total},
            "data": rows,
        }
        return resp

    return fake_get, calls


def test_more_than_one_page_is_collected():
    """1000 five-minute bars is thirteen trading days. A month-long intraday
    backtest used to measure a fortnight and report it as the whole period."""
    fake_get, calls = _page_server(total=2500)
    client = MarketstackClient(api_key="k")
    with patch("requests.get", side_effect=fake_get):
        rows = client.fetch_eod("X", start_date="2020-01-01", end_date="2024-01-01")

    assert len(rows) == 2500, "every page must be collected"
    assert len(calls) == 3, calls
    assert [c[0] for c in calls] == [0, 1000, 2000], "offset must advance"


def test_paging_stops_at_the_end_rather_than_looping():
    fake_get, calls = _page_server(total=1200)
    client = MarketstackClient(api_key="k")
    with patch("requests.get", side_effect=fake_get):
        rows = client.fetch_eod("X")

    assert len(rows) == 1200
    assert len(calls) == 2


def test_a_single_short_page_needs_only_one_call():
    fake_get, calls = _page_server(total=40)
    client = MarketstackClient(api_key="k")
    with patch("requests.get", side_effect=fake_get):
        rows = client.fetch_eod("X")

    assert len(rows) == 40
    assert len(calls) == 1, "a short page means there is no more data"
