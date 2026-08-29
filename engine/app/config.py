try:
    from pydantic_settings import BaseSettings
except ImportError:  # pragma: no cover - pydantic v1 fallback
    from pydantic import BaseSettings

from typing import Optional

from engine.app.env_paths import ENV_FILE

# Choice FinX environments.
#   UAT  - sandbox for integration testing; no real orders or money.
#   PROD - live trading; real orders, real funds.
# Source: Choice OpenAPI Integration Guide s10 "API Documentation & Environments".
CHOICE_BASE_URLS = {
    "UAT": "https://uat.jiffy.in",
    "PROD": "https://finxomne.choiceindia.com",
}


class EngineSettings(BaseSettings):
    PROJECT_NAME: str = "Choice FINX Trading Engine Service"
    VERSION: str = "1.2.8"
    API_V1_STR: str = "/api/v1"

    # Which Choice environment order flow is routed to. Defaults to UAT so that
    # an unconfigured deployment can never place a live order by accident
    # (Integration Guide s11.2: "Do not route live client orders without
    # completing UAT certification").
    CHOICE_ENV: str = "UAT"

    # Explicit override; when unset the URL is derived from CHOICE_ENV.
    CHOICE_BASE_URL: Optional[str] = None

    # Network behaviour for every call to Choice OpenAPI.
    CHOICE_HTTP_TIMEOUT: float = 15.0
    CHOICE_HTTP_RETRIES: int = 2
    CHOICE_HTTP_BACKOFF: float = 0.5

    # Broker sessions are dropped after this many seconds of inactivity. The
    # Choice 2FA token is valid for roughly 8 hours.
    CHOICE_SESSION_TTL_SECONDS: int = 8 * 60 * 60

    # SEBI/NSE cap unregistered strategies at 10 orders/second (Integration
    # Guide s9). Enforced per session before the request leaves the process.
    ORDER_RATE_LIMIT_PER_SEC: float = 10.0

    # Risk limits applied to every order regardless of source.
    MAX_ORDER_VALUE: float = 500000.0
    MAX_ORDER_QUANTITY: int = 100000

    # AES key for decrypting Choice partner OAuth callback parameters. Issued
    # per vendor by the Choice IT team (Partner Product Integration Guide s6).
    # Base64 or hex encoded 16/24/32-byte key. When unset the OAuth callback is
    # disabled rather than trusting plaintext parameters.
    CHOICE_OAUTH_AES_KEY: Optional[str] = None
    CHOICE_OAUTH_AES_IV: Optional[str] = None

    # Alternative historical market data provider (e.g. Marketstack) for
    # backtesting when Choice historical chart data is unavailable.
    HISTORICAL_DATA_PROVIDER: str = "AUTO"  # "AUTO", "CHOICE", "MARKETSTACK"
    MARKETSTACK_API_KEY: Optional[str] = None
    MARKETSTACK_BASE_URL: str = "https://api.marketstack.com"

    class Config:
        env_file = ENV_FILE
        case_sensitive = True
        # The backend and the engine share one .env, so each must
        # tolerate the other's keys rather than refusing to start.
        extra = "ignore"

    @property
    def choice_base_url(self) -> str:
        if self.CHOICE_BASE_URL:
            return self.CHOICE_BASE_URL.rstrip("/")
        env = (self.CHOICE_ENV or "UAT").upper()
        if env not in CHOICE_BASE_URLS:
            raise ValueError(
                f"CHOICE_ENV must be one of {sorted(CHOICE_BASE_URLS)}, got {env!r}"
            )
        return CHOICE_BASE_URLS[env]

    @property
    def is_production(self) -> bool:
        return (self.CHOICE_ENV or "UAT").upper().startswith("PROD")


engine_settings = EngineSettings()
