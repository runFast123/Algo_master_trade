"""Where configuration is read from.

``pydantic-settings`` resolves ``env_file`` relative to the process working
directory, which for a desktop launch is wherever the user happened to start
the executable from. That makes a ``.env`` easy to write and impossible to
find. These helpers search explicit locations instead, in priority order.
"""

import os
import sys
from pathlib import Path

APP_DIR_NAME = "ChoiceFinxTrader"


def user_config_dir() -> Path:
    """Per-user configuration directory, created on demand."""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    elif sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    path = Path(base) / APP_DIR_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def candidate_env_files() -> list:
    """Locations a ``.env`` may live, most specific first.

    1. ``CHOICE_ENV_FILE`` if set - an explicit override always wins.
    2. The working directory, for a development run from the repository root.
    3. Beside the executable, so an installed copy can be configured in place.
    4. The per-user config directory, which is writable even when the install
       directory is not.
    """
    candidates = []

    override = os.environ.get("CHOICE_ENV_FILE")
    if override:
        candidates.append(Path(override))

    candidates.append(Path.cwd() / ".env")

    if getattr(sys, "frozen", False):
        candidates.append(Path(sys.executable).parent / ".env")
    else:
        candidates.append(Path(__file__).resolve().parents[2] / ".env")

    candidates.append(user_config_dir() / ".env")
    return candidates


def resolve_env_file() -> str:
    """First existing candidate, or the working-directory default."""
    for candidate in candidate_env_files():
        try:
            if candidate.is_file():
                return str(candidate)
        except OSError:
            continue
    return ".env"


ENV_FILE = resolve_env_file()
