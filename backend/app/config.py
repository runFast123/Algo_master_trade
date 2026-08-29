import logging
import secrets
from typing import List, Optional

try:
    from pydantic_settings import BaseSettings
except ImportError:  # pragma: no cover - pydantic v1 fallback
    from pydantic import BaseSettings

from engine.app.env_paths import ENV_FILE, user_config_dir

logger = logging.getLogger("config")

# Owner-only on POSIX. Windows inherits the per-user directory's ACL, which is
# already restricted to the account that owns it.
_KEY_MODE = 0o600


def _install_signing_key() -> str:
    """A signing key belonging to this installation, generated once.

    Kept beside the database rather than in the ``.env``, so a user never has
    to create or paste one — which was the practical barrier to handing someone
    the executable and nothing else.
    """
    path = user_config_dir() / "secret.key"
    try:
        existing = path.read_text(encoding="utf-8").strip()
        if len(existing) >= 32:
            return existing
    except OSError:
        pass

    key = secrets.token_urlsafe(48)
    try:
        path.write_text(key, encoding="utf-8")
        try:
            path.chmod(_KEY_MODE)
        except (OSError, NotImplementedError):
            pass          # Windows: the directory ACL already restricts it
        logger.info("Generated a signing key for this installation: %s", path)
    except OSError as exc:
        # Nowhere to write means falling back to the old behaviour: usable, but
        # sign-in will not survive a restart. Say which, rather than leaving the
        # user to discover it.
        logger.warning(
            "Could not store a signing key at %s (%s). Sessions will not "
            "survive a restart until this is writable.", path, exc,
        )
    return key


class Settings(BaseSettings):
    PROJECT_NAME: str = "Algo Trading Platform Backend"
    VERSION: str = "1.2.2"
    API_V1_STR: str = "/api/v1"
    GITHUB_REPO: str = "runFast123/Algo_master_trade"

    # "development" relaxes a few checks; "production" refuses to start on any
    # insecure default.
    APP_ENV: str = "development"

    DATABASE_URL: Optional[str] = None

    # Signing key for platform JWTs. Never defaulted to a literal: an
    # unconfigured deployment gets a random per-process key, which invalidates
    # tokens on restart rather than shipping a key that is public in source.
    SECRET_KEY: Optional[str] = None
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 8

    # Browser origins allowed to call the API. The desktop client talks to the
    # API through its own local proxy and needs no cross-origin access.
    CORS_ORIGINS: List[str] = []

    # Interface the API binds to when started directly. Loopback by default so
    # a development run is not exposed to the local network.
    HOST: str = "127.0.0.1"
    PORT: int = 8000



    class Config:
        env_file = ENV_FILE
        case_sensitive = True
        # The backend and the engine share one .env, so each must
        # tolerate the other's keys rather than refusing to start.
        extra = "ignore"

    @property
    def is_production(self) -> bool:
        return self.APP_ENV.lower() in {"production", "prod"}

    def resolved_secret_key(self) -> str:
        """The key platform sign-in tokens are signed with.

        A configured ``SECRET_KEY`` always wins, so a server deployment sharing
        one key across processes is unaffected.

        Failing that, the key is generated **once per install** and kept beside
        the database. It used to be generated per *process*, which invalidated
        every token on every restart: a desktop user signed in again each time
        they opened the app, for no security benefit — the key was equally
        unknown to an attacker either way, it was only unknown to yesterday's
        session as well.
        """
        if self.SECRET_KEY:
            if len(self.SECRET_KEY) < 32:
                raise RuntimeError(
                    "SECRET_KEY must be at least 32 characters. "
                    "Generate one with: python -c \"import secrets;"
                    "print(secrets.token_urlsafe(48))\""
                )
            return self.SECRET_KEY

        if self.is_production:
            raise RuntimeError(
                "SECRET_KEY is required when APP_ENV=production. "
                "Set it in the environment or .env file."
            )

        if not hasattr(self, "_install_key"):
            object.__setattr__(self, "_install_key", _install_signing_key())
        return getattr(self, "_install_key")


settings = Settings()
