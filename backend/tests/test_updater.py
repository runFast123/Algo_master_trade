import json
import unittest.mock
import urllib.error
import pytest
from app.services.updater_service import _parse_version, check_for_updates


def test_parse_version():
    assert _parse_version("1.1.0") == (1, 1, 0)
    assert _parse_version("v1.2.3") == (1, 2, 3)
    assert _parse_version("V2.0") == (2, 0, 0)
    assert _parse_version("1.0") == (1, 0, 0)
    assert _parse_version("v2.1.4.5") == (2, 1, 4, 5)
    assert _parse_version("1.2.0") > _parse_version("1.1.0")
    assert _parse_version("v1.1.0") == _parse_version("1.1.0")


def test_check_for_updates_available():
    mock_payload = {
        "tag_name": "v1.2.0",
        "name": "Choice FINX Algo v1.2.0",
        "body": "New auto-update feature included.",
        "html_url": "https://github.com/runFast123/Algo_master_trade/releases/tag/v1.2.0",
        "published_at": "2026-08-28T12:00:00Z",
        "assets": [
            {
                "name": "ChoiceFinxTrader.exe",
                "size": 110000000,
                "browser_download_url": "https://github.com/runFast123/Algo_master_trade/releases/download/v1.2.0/ChoiceFinxTrader.exe",
            }
        ],
    }

    mock_resp = unittest.mock.MagicMock()
    mock_resp.status = 200
    mock_resp.read.return_value = json.dumps(mock_payload).encode("utf-8")
    mock_resp.__enter__.return_value = mock_resp

    with unittest.mock.patch("urllib.request.urlopen", return_value=mock_resp):
        res = check_for_updates(
            repo="runFast123/Algo_master_trade",
            current_version="1.1.0",
            force_check=True,
        )
        assert res["update_available"] is True
        assert res["latest_version"] == "1.2.0"
        assert res["download_url"] == "https://github.com/runFast123/Algo_master_trade/releases/download/v1.2.0/ChoiceFinxTrader.exe"
        assert res["asset_name"] == "ChoiceFinxTrader.exe"


def test_check_for_updates_same_version():
    mock_payload = {
        "tag_name": "v1.1.0",
        "name": "Choice FINX Algo v1.1.0",
        "body": "Initial release",
        "html_url": "https://github.com/runFast123/Algo_master_trade/releases/tag/v1.1.0",
        "assets": [],
    }

    mock_resp = unittest.mock.MagicMock()
    mock_resp.status = 200
    mock_resp.read.return_value = json.dumps(mock_payload).encode("utf-8")
    mock_resp.__enter__.return_value = mock_resp

    with unittest.mock.patch("urllib.request.urlopen", return_value=mock_resp):
        res = check_for_updates(
            repo="runFast123/Algo_master_trade",
            current_version="1.1.0",
            force_check=True,
        )
        assert res["update_available"] is False
        assert res["latest_version"] == "1.1.0"


def test_check_for_updates_offline_graceful():
    with unittest.mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("No internet")):
        res = check_for_updates(
            repo="runFast123/Algo_master_trade",
            current_version="1.1.0",
            force_check=True,
        )
        assert res["update_available"] is False
        assert res["status"] == "error"
        assert "No internet" in str(res["error"])


def test_system_endpoints(client):
    r_ver = client.get("/api/v1/system/version")
    assert r_ver.status_code == 200
    ver_data = r_ver.json()
    assert "version" in ver_data
    assert "repo" in ver_data

    r_chk = client.get("/api/v1/system/update-check")
    assert r_chk.status_code == 200
    chk_data = r_chk.json()
    assert "current_version" in chk_data
    assert "update_available" in chk_data
