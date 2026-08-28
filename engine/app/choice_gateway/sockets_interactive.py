"""Interactive socket wrapper: order and trade push updates.

Wraps ``choice_api.InteractiveSocketClient``, which connects to
``wss://finxsocket.choiceindia.com/ws/?token=<JWT>`` and emits MKT_STAT,
ORD_NRML and TRD_MSG messages (FINX Interactive Socket API Reference v1.0).

The JWT is the 2FA access token from the logon response - it is not generated
locally - so a session must be authenticated before a connection is possible.
"""

import asyncio
import logging
import threading
from typing import Any, Callable, Dict, List, Optional

from engine.app.choice_gateway.client_manager import ChoiceSession
from engine.app.choice_gateway.errors import ChoiceAuthError, ChoiceNotConnected

logger = logging.getLogger("choice_sockets")

MESSAGE_TYPES = ("MKT_STAT", "ORD_NRML", "TRD_MSG")

# ORD_NRML status codes, FINX Interactive Socket API Reference s6.2. Code 5 is
# the normal resting state for a working order and 7 is emitted on modify.
ORDER_STATUS = {
    1: "PLACED",
    2: "PARTIALLY_TRADED",
    3: "FULLY_TRADED",
    4: "CANCELLED",
    5: "OPEN_PENDING",
    6: "REJECTED",
    7: "MODIFIED",
}

TERMINAL_STATUSES = {"FULLY_TRADED", "CANCELLED", "REJECTED"}


def describe_order_status(code: Any) -> str:
    """Map a raw ORD_NRML status code to a name, or UNKNOWN_<code>."""
    try:
        return ORDER_STATUS[int(float(code))]
    except (TypeError, ValueError, KeyError):
        return f"UNKNOWN_{code}"


class InteractiveSocketConnection:
    """One user's interactive socket, running on its own event loop thread."""

    def __init__(self, session: ChoiceSession):
        self.session = session
        self._client = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._handlers: Dict[str, List[Callable]] = {t: [] for t in MESSAGE_TYPES}
        self._running = False

    def on(self, message_type: str, handler: Callable) -> None:
        if message_type not in self._handlers:
            raise ValueError(
                f"message_type must be one of {MESSAGE_TYPES}, got {message_type!r}"
            )
        self._handlers[message_type].append(handler)

    def start(self) -> None:
        if self._running:
            return
        if not self.session.is_live:
            raise ChoiceNotConnected(
                "A live Choice session is required for the interactive socket."
            )
        if not self.session.access_token:
            raise ChoiceAuthError(
                "No 2FA access token on this session. The interactive socket token "
                "comes from the logon response and cannot be generated locally."
            )

        from choice_api import InteractiveSocketClient

        self._client = InteractiveSocketClient(token=self.session.access_token)
        for message_type, handlers in self._handlers.items():
            for handler in handlers:
                self._client.on(message_type, handler)

        self._running = True
        self._thread = threading.Thread(
            target=self._run_loop, name=f"finx-interactive-{self.session.owner_key[:8]}",
            daemon=True,
        )
        self._thread.start()
        logger.info("Interactive socket started for user %s", self.session.owner_key)

    def _run_loop(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._client.connect())
        except Exception as exc:  # pragma: no cover - network dependent
            logger.error("Interactive socket failed for user %s: %s",
                         self.session.owner_key, exc)
        finally:
            self._running = False

    def stop(self) -> None:
        if not self._running or not self._loop or not self._client:
            return
        self._running = False
        asyncio.run_coroutine_threadsafe(self._client.disconnect(), self._loop)
        logger.info("Interactive socket stopped for user %s", self.session.owner_key)

    @property
    def is_running(self) -> bool:
        return self._running


_connections: Dict[str, InteractiveSocketConnection] = {}
_lock = threading.Lock()


def get_connection(session: ChoiceSession) -> InteractiveSocketConnection:
    """Return this user's interactive socket, creating it if needed."""
    with _lock:
        conn = _connections.get(session.owner_key)
        if conn is None or conn.session is not session:
            conn = InteractiveSocketConnection(session)
            _connections[session.owner_key] = conn
        return conn


def close_connection(owner_key: str) -> None:
    with _lock:
        conn = _connections.pop(owner_key, None)
    if conn:
        conn.stop()
