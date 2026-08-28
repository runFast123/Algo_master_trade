"""The signing key that keeps a user signed in across restarts.

Generated per *process* before this, so every restart invalidated every token
and a desktop user signed in again each time they opened the app — for no
security benefit, since the key was equally unknown to an attacker either way.
"""

from pathlib import Path

import pytest

import backend.app.config as config


@pytest.fixture
def install_dir(tmp_path, monkeypatch):
    """A clean per-user directory, as a fresh install would have."""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(config, "user_config_dir",
                        lambda: Path(tmp_path) / "ChoiceFinxTrader")
    (Path(tmp_path) / "ChoiceFinxTrader").mkdir(parents=True, exist_ok=True)
    return Path(tmp_path) / "ChoiceFinxTrader"


def test_the_key_survives_a_restart(install_dir):
    """Two Settings objects stand in for two launches."""
    first = config.Settings(SECRET_KEY=None).resolved_secret_key()
    second = config.Settings(SECRET_KEY=None).resolved_secret_key()

    assert first == second
    assert (install_dir / "secret.key").read_text().strip() == first


def test_the_key_is_long_enough_to_sign_with(install_dir):
    assert len(config.Settings(SECRET_KEY=None).resolved_secret_key()) >= 32


def test_a_configured_key_still_wins(install_dir):
    """A server deployment sharing one key across processes must be
    unaffected, and must not have a file written behind its back."""
    configured = "k" * 48
    assert config.Settings(SECRET_KEY=configured).resolved_secret_key() == configured
    assert not (install_dir / "secret.key").exists()


def test_a_short_configured_key_is_refused_rather_than_padded(install_dir):
    with pytest.raises(RuntimeError, match="at least 32"):
        config.Settings(SECRET_KEY="tooshort").resolved_secret_key()


def test_production_still_demands_an_explicit_key(install_dir):
    """A generated key is right for one desktop install. A production server
    must not quietly invent one — several processes would each make their own
    and reject each other's tokens."""
    with pytest.raises(RuntimeError, match="SECRET_KEY is required"):
        config.Settings(SECRET_KEY=None, APP_ENV="production").resolved_secret_key()


def test_an_unwritable_directory_still_starts(install_dir, monkeypatch):
    """Losing persistence must not stop the app: the user can still sign in,
    they just have to again after a restart."""
    monkeypatch.setattr(config, "user_config_dir",
                        lambda: Path("/nonexistent-path-for-this-test"))
    key = config.Settings(SECRET_KEY=None).resolved_secret_key()
    assert len(key) >= 32
