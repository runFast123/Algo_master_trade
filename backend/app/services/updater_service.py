"""GitHub Releases auto-updater service.

Checks for updates from GitHub Releases API for ChoiceFinxTrader desktop app.
Results are cached in memory to avoid GitHub API rate limits.
"""

import json
import logging
import re
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger("updater")

_CACHE_TTL_SECONDS = 15 * 60  # 15 minutes cache
_cached_release_info: Optional[Dict[str, Any]] = None
_last_checked_time: float = 0.0


def _parse_version(version_str: str) -> Tuple[int, ...]:
    """Parse version string like 'v1.2.3' or '1.1.0' into comparable tuple of ints."""
    clean = re.sub(r"^[vV]", "", (version_str or "").strip())
    parts = []
    for piece in clean.split("."):
        # Match leading digits
        match = re.match(r"^(\d+)", piece)
        if match:
            parts.append(int(match.group(1)))
        else:
            parts.append(0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts)


def check_for_updates(
    repo: str = "runFast123/Algo_master_trade",
    current_version: str = "1.1.0",
    force_check: bool = False,
) -> Dict[str, Any]:
    """Check GitHub Releases for newer version of the application.

    Returns dictionary containing update status, latest release info,
    and download URLs. Never raises exceptions to caller.
    """
    global _cached_release_info, _last_checked_time

    now = time.time()
    if not force_check and _cached_release_info is not None and (now - _last_checked_time) < _CACHE_TTL_SECONDS:
        return _cached_release_info

    fallback_result: Dict[str, Any] = {
        "update_available": False,
        "current_version": current_version,
        "latest_version": current_version,
        "release_name": "",
        "release_notes": "",
        "published_at": "",
        "html_url": f"https://github.com/{repo}/releases",
        "download_url": "",
        "asset_name": "",
        "asset_size": 0,
        "checked_at": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(now)),
        "status": "up_to_date",
        "error": None,
    }

    url = f"https://api.github.com/repos/{repo}/releases/latest"
    headers = {
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "ChoiceFinxTrader-AutoUpdater/1.0",
    }

    try:
        request = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(request, timeout=8) as response:
            if response.status != 200:
                fallback_result["status"] = "error"
                fallback_result["error"] = f"GitHub API responded with status {response.status}"
                return fallback_result

            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        fallback_result["status"] = "error"
        if exc.code == 404:
            fallback_result["error"] = "No releases found on GitHub repository."
        elif exc.code == 403:
            fallback_result["error"] = "GitHub rate limit exceeded. Please try again later."
        else:
            fallback_result["error"] = f"HTTP Error {exc.code}"
        return fallback_result
    except Exception as exc:
        fallback_result["status"] = "error"
        fallback_result["error"] = f"Could not reach GitHub: {exc}"
        return fallback_result

    tag_name = data.get("tag_name", "").strip()
    latest_version = re.sub(r"^[vV]", "", tag_name)
    release_name = data.get("name") or tag_name or "Latest Release"
    release_notes = data.get("body") or ""
    html_url = data.get("html_url") or f"https://github.com/{repo}/releases"
    published_at = data.get("published_at") or ""

    # Find the best executable / asset
    download_url = ""
    asset_name = ""
    asset_size = 0
    assets = data.get("assets", [])
    if assets:
        for asset in assets:
            name = asset.get("name", "")
            if name.lower().endswith(".exe"):
                download_url = asset.get("browser_download_url", "")
                asset_name = name
                asset_size = asset.get("size", 0)
                if "choicefinxtrader" in name.lower():
                    break
        if not download_url and assets:
            download_url = assets[0].get("browser_download_url", "")
            asset_name = assets[0].get("name", "")
            asset_size = assets[0].get("size", 0)

    curr_parsed = _parse_version(current_version)
    latest_parsed = _parse_version(latest_version)
    update_available = latest_parsed > curr_parsed

    result = {
        "update_available": update_available,
        "current_version": current_version,
        "latest_version": latest_version or current_version,
        "tag_name": tag_name,
        "release_name": release_name,
        "release_notes": release_notes,
        "published_at": published_at,
        "html_url": html_url,
        "download_url": download_url or html_url,
        "asset_name": asset_name,
        "asset_size": asset_size,
        "checked_at": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(now)),
        "status": "update_available" if update_available else "up_to_date",
        "error": None,
    }

    _cached_release_info = result
    _last_checked_time = now
    return result


def _download_and_apply_update(download_url: str) -> None:
    import os
    import sys
    import subprocess
    import tempfile
    
    try:
        # sys.executable points to the current Python interpreter.
        # In a PyInstaller one-file frozen build, sys.executable is the standalone .exe.
        exe_path = os.path.abspath(sys.executable)
        exe_name = os.path.basename(exe_path)
        new_exe_path = exe_path + ".new"
        old_exe_path = exe_path + ".old"
        
        logger.info(f"Downloading update from {download_url} to {new_exe_path}")
        urllib.request.urlretrieve(download_url, new_exe_path)
        logger.info("Download complete. Creating updater batch script.")
        
        bat_path = os.path.join(tempfile.gettempdir(), "choice_updater.bat")
        log_path = os.path.join(tempfile.gettempdir(), "choice_updater_log.txt")
        with open(bat_path, "w") as f:
            f.write(f"""@echo off
title Choice FINX Algo Updater
echo Updater started > "{log_path}"
:: Issue kill signal
taskkill /F /IM "{exe_name}" >> "{log_path}" 2>&1

:: Wait for process to fully exit
:waitloop
tasklist | find /i "{exe_name}" >nul
if not errorlevel 1 (
    timeout /t 1 /nobreak >nul
    goto waitloop
)
echo Process terminated >> "{log_path}"

del /f /q "{old_exe_path}" >> "{log_path}" 2>&1
move /y "{exe_path}" "{old_exe_path}" >> "{log_path}" 2>&1
move /y "{new_exe_path}" "{exe_path}" >> "{log_path}" 2>&1
echo Starting new application >> "{log_path}"
start "" "{exe_path}"
echo Updater finished >> "{log_path}"
del "%~f0"
""")
        
        logger.info("Running updater script and shutting down.")
        subprocess.Popen(
            ["cmd.exe", "/c", bat_path],
            creationflags=0x00000010  # CREATE_NEW_CONSOLE - makes the batch window visible so the launched app is also visible
        )
        
        # Exit the current process quickly
        os._exit(0)
    except Exception as e:
        logger.error(f"Failed to apply update: {e}", exc_info=True)


def apply_update_async(download_url: str) -> None:
    import sys
    import threading
    
    if not getattr(sys, "frozen", False):
        raise RuntimeError("In-app auto-update is only available in the compiled Windows executable.")
        
    threading.Thread(target=_download_and_apply_update, args=(download_url,), daemon=True).start()
