"""Per-user Choice FINX session management.

Every authenticated platform user gets their own :class:`ChoiceSession`. There
is deliberately no process-wide "current client": sharing one broker session
across tenants would let any user read and trade another user's account.

Sessions are held in memory by :class:`ChoiceSessionRegistry`, keyed by the
platform user id, and expire after ``CHOICE_SESSION_TTL_SECONDS`` of inactivity.
"""

import json
import logging
import threading
import time
from enum import Enum
from typing import Any, Dict, List, Optional

import requests
from choice_api import ChoiceClient

from engine.app.config import CHOICE_BASE_URLS, engine_settings
from engine.app.choice_gateway.errors import (
    ChoiceAuthError,
    ChoiceCredentialExpired,
    ChoiceNotConnected,
    ChoiceRateLimited,
    ChoiceUpstreamError,
)

# Choice's wording when the vendor is known and the API key has lapsed. Matched
# here, once, so every call is classified at the same point; callers branch on
# the exception type rather than repeating this comparison.
EXPIRED_MARKERS = ("token expired", "token has expired", "expired token")

logger = logging.getLogger("choice_gateway")

# Credentials that select the local sandbox instead of a real Choice login.
DEMO_KEYWORDS = {"DEMO", "SANDBOX", "TEST"}
DEFAULT_DEMO_MARGIN = 250000.00


class SessionMode(str, Enum):
    """How a session's requests are fulfilled.

    The mode is authoritative. Nothing infers it from connection state, and the
    two questions it answers are kept separate: where market data comes from,
    and whether an order actually leaves the building.

    ============  ==========================  ==========================
    Mode          Market data                 Orders
    ============  ==========================  ==========================
    DISCONNECTED  none                        rejected
    DEMO          local fixtures              simulated locally
    PAPER         real, from Choice           simulated locally
    LIVE          real, from Choice           sent to Choice
    ============  ==========================  ==========================

    PAPER is the mode to run a strategy against real market conditions without
    any risk to funds: the account is genuinely signed in, quotes, holdings and
    history are real, and orders are filled against the live traded price but
    never submitted.
    """

    DISCONNECTED = "DISCONNECTED"
    DEMO = "DEMO"
    PAPER = "PAPER"
    LIVE = "LIVE"

    @property
    def uses_broker_data(self) -> bool:
        return self in (SessionMode.PAPER, SessionMode.LIVE)

    @property
    def sends_real_orders(self) -> bool:
        return self is SessionMode.LIVE


class _RateLimiter:
    """Token bucket enforcing the SEBI/NSE 10-orders-per-second ceiling."""

    def __init__(self, rate_per_sec: float):
        self.rate = max(rate_per_sec, 0.001)
        self._allowance = self.rate
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        with self._lock:
            now = time.monotonic()
            self._allowance = min(
                self.rate, self._allowance + (now - self._last) * self.rate
            )
            self._last = now
            if self._allowance < 1.0:
                raise ChoiceRateLimited(
                    f"Order rate limit of {self.rate:g}/second exceeded. "
                    "Strategies above 10 orders/second require exchange registration."
                )
            self._allowance -= 1.0


class TimeoutChoiceClient(ChoiceClient):
    """``ChoiceClient`` with request timeouts and bounded retries.

    The upstream SDK calls ``requests.request`` with no timeout, so a stalled
    Choice endpoint would pin a worker thread indefinitely. It also raises bare
    ``Exception``; this override raises typed gateway errors and retries only
    idempotent failures (network errors and 5xx), never a 4xx.
    """

    # Set by ChoiceSession when it builds the client, so broker health can be
    # recorded from the one place every call passes through.
    owner_session: Optional[Any] = None

    def _stamp(self, exc: Exception) -> None:
        """Record which Choice server this failure came from.

        The environment is chosen per connection now, so a message naming the
        deployment default would tell a user connected to UAT to check their
        production credentials — or the reverse, which is worse.
        """
        session = getattr(self, "owner_session", None)
        if session is not None:
            exc.environment = getattr(session, "environment", None)
            exc.base_url = getattr(session, "base_url", None)

    def _note(self, ok: bool, exc: Exception = None) -> None:
        session = getattr(self, "owner_session", None)
        if session is None:
            return
        session.record_success() if ok else session.record_failure(exc)

    def request(
        self,
        method: str,
        endpoint: str,
        data: Optional[Dict[str, Any]] = None,
        require_auth: bool = True,
    ) -> Dict[str, Any]:
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        headers = self.get_headers(include_auth=require_auth)

        # Only GET is retried. A timeout is not "the request failed" — it is
        # "we do not know", and for POST that difference costs money: Choice's
        # POSTs carry no idempotency key, so a retried `NewOrder` is a second
        # order and a retried `ProcessPayout` is a second withdrawal. The
        # request may well have been booked before the response was lost.
        #
        # This costs a retry on the handful of read-only POSTs Choice defines
        # (MultipleTouchline, GetDISStatus, CheckVPA, OrderMessages). Losing a
        # retry on a quote is a refresh; gaining one on an order is a duplicate
        # position, so the trade is not close.
        retryable = str(method).strip().upper() == "GET"
        attempts = max(1, engine_settings.CHOICE_HTTP_RETRIES + 1) if retryable else 1
        last_error: Optional[Exception] = None

        for attempt in range(attempts):
            try:
                response = requests.request(
                    method,
                    url,
                    headers=headers,
                    json=data,
                    timeout=engine_settings.CHOICE_HTTP_TIMEOUT,
                )
            except requests.exceptions.RequestException as exc:
                last_error = exc
                if attempt < attempts - 1:
                    time.sleep(engine_settings.CHOICE_HTTP_BACKOFF * (2 ** attempt))
                    continue
                failure = ChoiceUpstreamError(
                    f"Could not reach Choice OpenAPI at {url}", str(exc)
                )
                self._stamp(failure)
                self._note(False, failure)
                raise failure from exc

            if response.status_code in (401, 403):
                body = response.text[:500]
                failure = (
                    ChoiceCredentialExpired("Your Choice API key has expired.", body)
                    if any(m in body.lower() for m in EXPIRED_MARKERS)
                    else ChoiceAuthError(
                        "Choice rejected the session. Sign in to Choice again.", body
                    )
                )
                self._stamp(failure)
                self._note(False, failure)
                raise failure
            if response.status_code >= 500 and attempt < attempts - 1:
                time.sleep(engine_settings.CHOICE_HTTP_BACKOFF * (2 ** attempt))
                continue
            if response.status_code >= 400:
                raise ChoiceUpstreamError(
                    f"Choice OpenAPI returned HTTP {response.status_code} for {endpoint}",
                    response.text[:500],
                )

            try:
                payload = response.json()
            except (ValueError, json.JSONDecodeError) as exc:
                raise ChoiceUpstreamError(
                    f"Choice OpenAPI returned a non-JSON response for {endpoint}",
                    response.text[:500],
                ) from exc
            self._note(True)
            return payload

        raise ChoiceUpstreamError(
            f"Could not reach Choice OpenAPI at {url}", str(last_error)
        )


def _demo_portfolio() -> List[Dict[str, Any]]:
    """Fixed sandbox holdings, per session so demo trades do not leak.

    A previous close is included on each so day-change works in the sandbox.
    Without it the sandbox reports "not priced" and the feature looks broken to
    anyone evaluating the app before connecting a real account. The mix is
    deliberate: one up on the day, one down, one flat.
    """
    return [
        {"symbol": "RELIANCE", "quantity": 50.0, "average_price": 2450.00,
         "current_price": 2504.50, "close_price": 2480.00},
        {"symbol": "INFY", "quantity": 100.0, "average_price": 1520.00,
         "current_price": 1540.20, "close_price": 1552.75},
        {"symbol": "TCS", "quantity": 25.0, "average_price": 4150.00,
         "current_price": 4120.00, "close_price": 4120.00},
    ]


class ChoiceSession:
    """One user's connection to Choice FINX."""

    def __init__(self, owner_key: str):
        self.owner_key = owner_key
        self.mode: SessionMode = SessionMode.DISCONNECTED
        self.vendor_id: Optional[str] = None
        self.api_key: Optional[str] = None
        self.mobile_no: Optional[str] = None
        self.session_id: Optional[str] = None
        self.access_token: Optional[str] = None
        self.base_url: str = engine_settings.choice_base_url
        # Which Choice server this session talks to. Defaults to the
        # deployment's setting; a connection may override it.
        self.environment: str = (engine_settings.CHOICE_ENV or "UAT").upper()
        self.client: Optional[TimeoutChoiceClient] = None
        self.created_at = time.time()
        self.last_used_at = time.time()
        self.order_limiter = _RateLimiter(engine_settings.ORDER_RATE_LIMIT_PER_SEC)

        # Sandbox state, only ever read when mode is DEMO.
        self.demo_margin: float = DEFAULT_DEMO_MARGIN
        self.demo_used_margin: float = 0.0
        self.demo_holdings: List[Dict[str, Any]] = _demo_portfolio()

        # Every paper run for this user shares this one session object, and the
        # scheduler gives each run its own thread — so several fills can land on
        # the position book at once, with a manual order from the API thread on
        # top. `pnl += booked` is a read-modify-write, and a lost update here is
        # money quietly going missing from the day's realised figure.
        self.book_lock = threading.Lock()

        # Orders filled locally rather than sent, in both DEMO and PAPER.
        self.simulated_orders: List[Dict[str, Any]] = []
        # symbol -> {"quantity", "average_price"} for simulated fills.
        self.paper_positions: Dict[str, Dict[str, float]] = {}
        self.paper_realized_pnl: float = 0.0

        # Market-data cache, per session and keyed by the tokens requested.
        self.quote_cache: Dict[str, Any] = {}

        # Broker health, so the interface can state credential status rather
        # than leaving the user to infer it from a failed call.
        self.last_success_at: Optional[float] = None
        self.last_failure_at: Optional[float] = None
        self.credential_state: str = "UNKNOWN"   # OK / EXPIRED / REJECTED / UNKNOWN
        self.last_error: Optional[str] = None
        # None = not yet determined. Set the first time a market-data call
        # either works or is refused, so "no entitlement" is stated, not guessed.
        self.market_data_ok: Optional[bool] = None
        # Whether the user asked for this session to survive a restart.
        self.remember: bool = False

    # -- state -------------------------------------------------------------

    @property
    def is_demo(self) -> bool:
        return self.mode is SessionMode.DEMO

    @property
    def is_paper(self) -> bool:
        return self.mode is SessionMode.PAPER and bool(self.session_id)

    @property
    def is_live(self) -> bool:
        return self.mode is SessionMode.LIVE and bool(self.session_id)

    def persist(self) -> None:
        """Write this session to the store, or clear it if not remembered.

        Called on login *and* on every mode change. Only login used to write,
        so switching LIVE to PAPER left "LIVE" on disk — and `restore()` put
        the session straight back into LIVE on the next restart, bypassing the
        confirmation that guards that direction. The user's last explicit
        instruction was "paper".
        """
        from engine.app.choice_gateway import session_store

        if self.remember and self.session_id:
            session_store.save(
                self.owner_key, self.session_id, self.environment,
                self.mode.value, vendor_id=self.vendor_id, base_url=self.base_url,
            )
        else:
            session_store.clear(self.owner_key)

    @property
    def uses_broker_data(self) -> bool:
        """True when market data comes from Choice rather than fixtures."""
        return self.mode.uses_broker_data and bool(self.session_id)

    @property
    def simulates_orders(self) -> bool:
        """True when an order is filled locally instead of being submitted."""
        return self.mode in (SessionMode.DEMO, SessionMode.PAPER)

    @property
    def is_connected(self) -> bool:
        return self.is_demo or self.uses_broker_data

    def touch(self) -> None:
        self.last_used_at = time.time()

    def is_expired(self, ttl_seconds: int) -> bool:
        return (time.time() - self.last_used_at) > ttl_seconds

    def require_client(self) -> TimeoutChoiceClient:
        """Return the authenticated SDK client used for market data.

        Available in PAPER as well as LIVE: paper trading is only meaningful
        against real prices. What PAPER does not do is submit orders.
        """
        if self.mode is SessionMode.DISCONNECTED:
            raise ChoiceNotConnected(
                "Not signed in to Choice FINX. Connect your Choice account first."
            )
        if self.is_demo:
            raise ChoiceNotConnected(
                "This is a sandbox session, which has no Choice connection. "
                "Reconnect in paper or live mode to use real market data."
            )
        if not self.client or not self.session_id:
            raise ChoiceAuthError("Choice session is no longer valid. Sign in again.")
        self.touch()
        return self.client

    def paper_pnl(self, prices: Optional[Dict[str, float]] = None) -> Dict[str, Any]:
        """Realised and open P&L for simulated fills.

        Open positions are marked against ``prices`` when the caller supplies
        current quotes, otherwise against the last price each position traded
        at. ``marked_to`` says which, because an unrealised figure marked to a
        stale fill is not the same claim as one marked to the market.
        """
        prices = prices or {}
        unrealized = 0.0
        marked_to_market = True

        for symbol, position in self.paper_positions.items():
            quote = prices.get(symbol)
            if quote is None:
                quote = position.get("last_price")
                marked_to_market = False
            if quote:
                unrealized += (quote - position["average_price"]) * position["quantity"]

        return {
            "realized_pnl": round(self.paper_realized_pnl, 2),
            "unrealized_pnl": round(unrealized, 2),
            "marked_to": "market" if (marked_to_market and self.paper_positions)
                         else "last_fill",
            "open_positions": len(self.paper_positions),
            "simulated_orders": len(self.simulated_orders),
        }

    def record_success(self) -> None:
        """A Choice call answered. Clears any credential complaint."""
        self.last_success_at = time.time()
        self.credential_state = "OK"
        self.last_error = None

    def record_failure(self, exc: Exception) -> None:
        """A Choice call failed. Classify it for display."""
        from engine.app.choice_gateway.errors import (
            ChoiceAuthError, ChoiceCredentialExpired,
        )
        self.last_failure_at = time.time()
        self.last_error = str(getattr(exc, "message", exc))[:300]
        if isinstance(exc, ChoiceCredentialExpired):
            self.credential_state = "EXPIRED"
        elif isinstance(exc, ChoiceAuthError):
            self.credential_state = "REJECTED"

    def describe(self) -> Dict[str, Any]:
        info = {
            "mode": self.mode.value,
            "vendor_id": self.vendor_id,
            # The session's own environment, not the deployment default: a
            # connection may pick the other one, and reporting the default
            # would tell the user they are on UAT while they trade on PROD.
            "environment": self.environment,
            "base_url": self.base_url,
            "connected_at": self.created_at,
            "sends_real_orders": self.mode.sends_real_orders,
            "uses_real_market_data": self.uses_broker_data,
            # How long this session has before it is purged for inactivity, so
            # the interface can warn ahead of an expiry rather than surfacing
            # it as a failed call after the fact.
            "expires_in_seconds": max(
                0,
                int(engine_settings.CHOICE_SESSION_TTL_SECONDS
                    - (time.time() - self.last_used_at))
            ),
            "session_ttl_seconds": engine_settings.CHOICE_SESSION_TTL_SECONDS,
            "credential_state": self.credential_state,
            "last_success_at": self.last_success_at,
            "last_failure_at": self.last_failure_at,
            "last_error": self.last_error,
            "market_data_ok": self.market_data_ok,
            "remembered": self.remember,
        }
        if self.simulates_orders:
            info["paper"] = self.paper_pnl()
        return info

    # -- authentication ----------------------------------------------------

    def _build_client(self, vendor_id: str, api_key: str) -> TimeoutChoiceClient:
        self.client = TimeoutChoiceClient(
            vendor_id=vendor_id, api_key=api_key, base_url=self.base_url
        )
        self.client.owner_session = self
        if self.session_id:
            self.client.session_id = self.session_id
        return self.client

    def start_demo(self, vendor_id: str, custom_margin: Optional[float] = None) -> None:
        self.mode = SessionMode.DEMO
        self.vendor_id = vendor_id
        self.api_key = None
        self.session_id = f"DEMO_SESS_{self.owner_key[:8]}"
        self.client = None
        self.demo_margin = float(custom_margin) if custom_margin else DEFAULT_DEMO_MARGIN
        self.demo_used_margin = 0.0
        self.demo_holdings = _demo_portfolio()
        self.simulated_orders = []
        self.paper_positions = {}
        self.paper_realized_pnl = 0.0
        self.touch()
        logger.info("Sandbox session started for user %s", self.owner_key)

    def login_totp(
        self, vendor_id: str, api_key: str, mobile_no: str, paper: bool = False,
        remember: bool = False, environment: Optional[str] = None,
    ) -> None:
        """Run the Choice TOTP flow (LoginTOTP -> GetClientLoginTOTP -> ValidateTOTP).

        ``paper=True`` signs in for real and reads real market data, but leaves
        the session in PAPER mode so orders are filled locally instead of being
        submitted.

        ``environment`` selects the Choice server for this session only. Each
        user brings their own credentials, and a production Client ID is
        rejected by the UAT sandbox — so which server to use is a property of
        the person connecting, not of the installation.
        """
        if environment:
            self.environment = str(environment).strip().upper()
            self.base_url = CHOICE_BASE_URLS[self.environment].rstrip("/")

        self.vendor_id = str(vendor_id).strip()
        self.api_key = str(api_key).strip()
        self.mobile_no = str(mobile_no).strip()
        self.session_id = None
        client = self._build_client(self.vendor_id, self.api_key)

        try:
            live_session_id = client.login(self.mobile_no)
        except (ChoiceAuthError, ChoiceUpstreamError):
            self.mode = SessionMode.DISCONNECTED
            raise
        except Exception as exc:  # SDK may still raise bare exceptions
            self.mode = SessionMode.DISCONNECTED
            raise ChoiceAuthError("Choice FINX login failed", str(exc)) from exc

        if not live_session_id:
            self.mode = SessionMode.DISCONNECTED
            raise ChoiceAuthError("Choice FINX login returned no session id")

        self.session_id = live_session_id
        self.access_token = getattr(client, "access_token", None)
        client.session_id = live_session_id
        self.mode = SessionMode.PAPER if paper else SessionMode.LIVE
        if paper:
            self.simulated_orders = []
            self.paper_positions = {}
            self.paper_realized_pnl = 0.0
        self.touch()
        self.record_success()

        # Opt-in only. Without this a restart silently drops the session while
        # the platform login survives, which reads as a bug rather than a
        # security boundary.
        self.remember = bool(remember)
        self.persist()

        logger.info(
            "Choice session established for user %s in %s mode (%s)%s",
            self.owner_key, self.mode.value, self.environment,
            " [remembered]" if self.remember else "",
        )

    def record_simulated_fill(
        self, symbol: str, side: str, quantity: int, price: float
    ) -> Dict[str, Any]:
        """Apply a locally filled order to the paper position book.

        Signed throughout, because a position can be short. Strategies never go
        short — the runner sells only what it holds — but the order ticket will
        happily submit a SELL for something the account does not have, and the
        previous arithmetic handled that case by inventing money: ``min(qty,
        held)`` is negative once ``held`` is, which is truthy, so *adding* to a
        short booked a fabricated loss, while covering one booked nothing at
        all. A short taken at 100 and covered at 80 came out as -1000 realised
        instead of +300 — and that figure feeds the daily loss cap, so a
        profitable trade could halt trading for the day.
        """
        with self.book_lock:
            return self._apply_fill(symbol, side, quantity, price)

    def _apply_fill(
        self, symbol: str, side: str, quantity: int, price: float
    ) -> Dict[str, Any]:
        """The fill arithmetic itself. Call under ``book_lock``."""
        position = self.paper_positions.get(
            symbol, {"quantity": 0.0, "average_price": 0.0, "last_price": price}
        )
        held = position["quantity"]
        signed = quantity if side == "BUY" else -quantity

        # The part of this fill that reduces the existing position, if any.
        # Nothing is realised while a position is being opened or extended.
        closing = 0.0
        if held and (held > 0) != (signed > 0):
            closing = min(abs(signed), abs(held))
            # A long earns when it sells above its average; a short earns when
            # it buys back below. Same expression, opposite sign.
            booked = (price - position["average_price"]) * closing * (1 if held > 0 else -1)
            self.paper_realized_pnl += booked
            # Feed the risk manager so the daily loss cap sees paper losses
            # too. Without this the cap only ever reads zero.
            from engine.app.strategy_engine.risk_manager import risk_manager
            # Into the simulated ledger. Recorded against the real one, a
            # losing paper strategy halted the user's actual trading for the
            # day — with a message that gave no hint none of it was real.
            risk_manager.record_realized_pnl(self.owner_key, booked, simulated=True)

        opening = abs(signed) - closing
        held = held + signed

        if opening > 0:
            # Averaged over the part that opened, plus whatever of the old
            # position survives on the same side. A flip leaves `remaining` at
            # zero, so the new side starts at this fill's price.
            remaining = abs(position["quantity"]) - closing
            position["average_price"] = (
                position["average_price"] * remaining + price * opening
            ) / (remaining + opening)
        elif held == 0:
            position["average_price"] = 0.0
        # A partial close leaves the average where it was, by definition.

        position["quantity"] = held
        position["last_price"] = price

        if held == 0:
            self.paper_positions.pop(symbol, None)
        else:
            self.paper_positions[symbol] = position

        return {
            "symbol": symbol,
            "position_quantity": held,
            "average_price": round(position["average_price"], 2),
            "realized_pnl": round(self.paper_realized_pnl, 2),
        }

    def request_totp(self, vendor_id: str, api_key: str, mobile_no: str) -> Optional[str]:
        """Trigger the OTP and return it when Choice echoes it back."""
        self.vendor_id = str(vendor_id).strip()
        self.api_key = str(api_key).strip()
        self.mobile_no = str(mobile_no).strip()
        client = self._build_client(self.vendor_id, self.api_key)

        encoded_mobile = client._get_encoded_mobile(self.mobile_no)
        client.request(
            "POST", "api/OpenAPIV1/LoginTOTP",
            {"MobileNo": encoded_mobile}, require_auth=False,
        )
        resp = client.request(
            "POST", "api/OpenAPIV1/GetClientLoginTOTP",
            {"MobileNo": encoded_mobile}, require_auth=False,
        )
        if resp.get("Status") == "Success":
            otp = resp.get("Response")
            if isinstance(otp, (str, int)):
                return str(otp).strip()
        return None

    def validate_totp(
        self, vendor_id: str, api_key: str, mobile_no: str, otp: str
    ) -> None:
        client = self.client or self._build_client(
            str(vendor_id).strip(), str(api_key).strip()
        )
        encoded_mobile = client._get_encoded_mobile(str(mobile_no).strip())
        resp = client.request(
            "POST", "api/OpenAPIV1/ValidateTOTP",
            {"MobileNo": encoded_mobile, "OTP": str(otp).strip()},
            require_auth=False,
        )

        if resp.get("Status") != "Success":
            self.mode = SessionMode.DISCONNECTED
            raise ChoiceAuthError(str(resp.get("Reason") or "Invalid OTP"))

        payload = resp.get("Response", {})
        if isinstance(payload, str):
            self.session_id = payload
        elif isinstance(payload, dict):
            self.session_id = payload.get("SessionId") or payload.get("session_id")
            self.access_token = payload.get("AccessToken")

        if not self.session_id:
            self.mode = SessionMode.DISCONNECTED
            raise ChoiceAuthError("Choice did not return a session id")

        client.session_id = self.session_id
        self.vendor_id = str(vendor_id).strip()
        self.mode = SessionMode.LIVE
        self.touch()

    def attach_partner_session(
        self,
        cid: str,
        sid: str,
        access_token: Optional[str] = None,
        base_url: Optional[str] = None,
    ) -> None:
        """Adopt a session handed over by the verified Choice partner OAuth flow."""
        self.vendor_id = str(cid).strip()
        self.session_id = str(sid).strip()
        self.access_token = access_token
        if base_url:
            self.base_url = base_url.rstrip("/")
        self._build_client(self.vendor_id, self.session_id)
        self.mode = SessionMode.LIVE
        self.touch()
        self.record_success()
        logger.info("Partner OAuth session attached for user %s", self.owner_key)

    def restore(self, data: Dict[str, Any]) -> bool:
        """Rehydrate a persisted session, but only if Choice still honours it.

        A stored session id proves nothing about whether Choice still accepts
        it — the API key it was issued against expires roughly daily. So the
        id is verified with one cheap call before the session is reported as
        connected; a stale id must never present as live.
        """
        from engine.app.choice_gateway import session_store

        # The environment is a property of the session, not of the install, so
        # a stored UAT session is restored as UAT even where the default is
        # PROD. What is not acceptable is a stored environment this build does
        # not recognise: that would leave base_url and the reported environment
        # disagreeing, which is the one thing this must never do.
        stored_env = str(data.get("environment") or "").upper()
        if stored_env not in CHOICE_BASE_URLS:
            session_store.clear(self.owner_key)
            return False

        self.environment = stored_env
        self.vendor_id = data.get("vendor_id") or self.vendor_id
        self.session_id = data.get("session_id")
        # Trust the environment over a stored URL: they are written together,
        # but only one of them is validated above.
        self.base_url = CHOICE_BASE_URLS[stored_env].rstrip("/")
        self._build_client(self.vendor_id or "", self.session_id or "")
        self.mode = (
            SessionMode.LIVE if data.get("mode") == "LIVE" else SessionMode.PAPER
        )

        try:
            self.client.funds.get_funds_view_new()
        except Exception as exc:
            logger.info(
                "Stored broker session for %s is no longer valid (%s); discarding.",
                self.owner_key, exc,
            )
            session_store.clear(self.owner_key)
            self.mode = SessionMode.DISCONNECTED
            self.session_id = None
            self.client = None
            return False

        self.remember = True
        self.touch()
        self.record_success()
        logger.info("Restored broker session for %s (%s)", self.owner_key, self.mode.value)
        return True

    def forget(self) -> None:
        """Drop any persisted session for this user."""
        from engine.app.choice_gateway import session_store
        session_store.clear(self.owner_key)
        self.remember = False


class ChoiceSessionRegistry:
    """Thread-safe store of per-user Choice sessions."""

    def __init__(self):
        self._sessions: Dict[str, ChoiceSession] = {}
        self._lock = threading.RLock()

    def _purge_locked(self) -> None:
        ttl = engine_settings.CHOICE_SESSION_TTL_SECONDS
        for key in [k for k, s in self._sessions.items() if s.is_expired(ttl)]:
            logger.info("Choice session for user %s expired", key)
            self._sessions.pop(key, None)

    def get(self, owner_key: str) -> Optional[ChoiceSession]:
        with self._lock:
            self._purge_locked()
            session = self._sessions.get(owner_key)
            if session:
                session.touch()
            return session

    def get_or_create(self, owner_key: str) -> ChoiceSession:
        with self._lock:
            self._purge_locked()
            session = self._sessions.get(owner_key)
            if session is None:
                session = ChoiceSession(owner_key)
                self._sessions[owner_key] = session
            session.touch()
            return session

    def require(self, owner_key: str) -> ChoiceSession:
        session = self.get(owner_key)
        if session is None or not session.is_connected:
            raise ChoiceNotConnected(
                "Not signed in to Choice FINX. Connect your Choice account first."
            )
        return session

    def drop(self, owner_key: str) -> None:
        with self._lock:
            self._sessions.pop(owner_key, None)

    def active_count(self) -> int:
        with self._lock:
            self._purge_locked()
            return len(self._sessions)

    def owner_keys(self) -> List[str]:
        """Ids of the users with a live session, for admin-level actions."""
        with self._lock:
            self._purge_locked()
            return list(self._sessions)

    def paper_pnl_for(self, owner_keys: List[str]) -> float:
        """Realised paper P&L across the given users' live sessions.

        In-memory only: paper fills are not persisted, so this covers sessions
        that are currently connected and nothing else. Reported as such rather
        than presented as a complete figure.
        """
        with self._lock:
            self._purge_locked()
            return round(sum(
                s.paper_realized_pnl for key, s in self._sessions.items()
                if key in set(owner_keys)
            ), 2)

    def live_count(self) -> int:
        with self._lock:
            self._purge_locked()
            return sum(1 for s in self._sessions.values() if s.is_live)


choice_sessions = ChoiceSessionRegistry()


def is_demo_credential(vendor_id: str, api_key: str) -> bool:
    """True when the supplied credentials select the local sandbox."""
    return (
        str(vendor_id or "").strip().upper() in DEMO_KEYWORDS
        or str(api_key or "").strip().upper() in DEMO_KEYWORDS | {"DEMO_KEY"}
    )
