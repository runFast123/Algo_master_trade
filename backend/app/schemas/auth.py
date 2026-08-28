from typing import Optional

from pydantic import BaseModel, EmailStr, Field, field_validator
from engine.app.config import CHOICE_BASE_URLS

# A Choice API key is an opaque bearer credential. Depending on how it was
# issued it can be a short key or a signed token of a few kilobytes, so the
# bound here only exists to reject obvious junk, not to describe a format.
MAX_API_KEY_LENGTH = 4096


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=256)


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=10, max_length=256)
    full_name: str = Field(min_length=1, max_length=255)
    tenant_name: Optional[str] = Field(default="Default Organization", max_length=100)


class _ChoiceCredentials(BaseModel):
    """Shared handling for Choice credentials.

    Values are stripped before validation: a key copied from the FinX portal
    often arrives with a trailing newline or space, and rejecting that would be
    a confusing failure for something the user cannot see.
    """

    @field_validator("*", mode="before")
    @classmethod
    def _strip_whitespace(cls, value):
        return value.strip() if isinstance(value, str) else value


class ChoiceTotpRequest(_ChoiceCredentials):
    """Credentials for connecting a Choice account.

    ``mode`` decides what the session is allowed to do:

    * ``paper`` (default) - signs in for real and uses real market data, but
      fills orders locally. Nothing is ever sent to Choice, so no funds move.
    * ``live`` - signs in and submits orders to Choice. Real money.
    * ``sandbox`` - no broker connection at all; fixture data only. Also
      selected by entering DEMO as the vendor id or API key.

    The default is deliberately the safe one: connecting must not start placing
    real orders unless that was asked for explicitly.
    """

    vendor_id: str = Field(min_length=1, max_length=64)
    api_key: str = Field(min_length=1, max_length=MAX_API_KEY_LENGTH)
    mobile_no: str = Field(default="", max_length=20)
    mode: str = Field(default="paper")
    custom_margin: Optional[float] = Field(default=None, ge=0)
    # Opt in to keeping the broker session across a restart on this machine.
    # Defaults to off: persisting a live broker credential is a choice the user
    # makes, not one the app makes for them.
    remember: bool = Field(default=False)

    # Which Choice server this session talks to. Chosen per connection rather
    # than read from a shared .env, because on a multi-user install one person
    # testing against UAT must not move everyone else, and a production Client
    # ID is simply rejected by the sandbox — the commonest setup failure there
    # is. Empty means "whatever this deployment is configured for".
    environment: Optional[str] = Field(default=None)

    @field_validator("mode")
    @classmethod
    def _check_mode(cls, value: str) -> str:
        lowered = str(value).strip().lower()
        if lowered not in {"paper", "live", "sandbox", "demo"}:
            raise ValueError("mode must be one of: paper, live, sandbox")
        return "sandbox" if lowered == "demo" else lowered

    @field_validator("environment")
    @classmethod
    def _check_environment(cls, value: Optional[str]) -> Optional[str]:
        if value is None or not str(value).strip():
            return None
        upper = str(value).strip().upper()
        if upper not in CHOICE_BASE_URLS:
            raise ValueError(
                f"environment must be one of: {', '.join(sorted(CHOICE_BASE_URLS))}"
            )
        return upper


class ChoiceValidateTotpRequest(_ChoiceCredentials):
    vendor_id: str = Field(min_length=1, max_length=64)
    api_key: str = Field(min_length=1, max_length=MAX_API_KEY_LENGTH)
    mobile_no: str = Field(min_length=1, max_length=20)
    otp: str = Field(min_length=1, max_length=12)


class ChoiceSessionStatus(BaseModel):
    connected: bool = False
    mode: str = "DISCONNECTED"
    vendor_id: Optional[str] = None
    environment: Optional[str] = None
    base_url: Optional[str] = None
    # Stated explicitly rather than inferred from the mode string, so the UI
    # never has to guess whether an order would reach the exchange.
    sends_real_orders: bool = False
    uses_real_market_data: bool = False
    expires_in_seconds: Optional[int] = None
    session_ttl_seconds: Optional[int] = None
    # Broker health, so the interface states credential and entitlement status
    # instead of inferring either from a failed call.
    credential_state: Optional[str] = None       # OK / EXPIRED / REJECTED / UNKNOWN
    last_success_at: Optional[float] = None
    last_failure_at: Optional[float] = None
    last_error: Optional[str] = None
    market_data_ok: Optional[bool] = None
    paper: Optional[dict] = None


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user_id: str
    tenant_id: str
    role: str


class TokenData(BaseModel):
    user_id: Optional[str] = None
    tenant_id: Optional[str] = None
    role: Optional[str] = None
