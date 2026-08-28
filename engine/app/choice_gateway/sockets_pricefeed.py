"""Price feed socket wrapper: Level 1 touchline and Level 2 depth.

Wraps ``choice_api.PriceFeedSocketClient``, which speaks the FIX3.0 pipe
delimited protocol over a zlib-compressed WebSocket (Live Data Feed
Specification).

Per that specification the feed handler address is *not* a fixed URL: the
logon response carries ``OdinBcastIP`` and ``OdinBcastPort``, and those are
what a client must connect to. This wrapper prefers the values the session
received at login and only falls back to the vendor default when the logon
response did not supply them.
"""

import logging
import threading
from typing import Any, Callable, Dict, List

from engine.app.choice_gateway.client_manager import ChoiceSession
from engine.app.choice_gateway.errors import ChoiceNotConnected

logger = logging.getLogger("choice_sockets")

# Used only when the logon response carries no OdinBcastIP/OdinBcastPort.
FALLBACK_FEED_HOST = "wss://brd.choiceindia.co.in"
FALLBACK_FEED_PORT = 4520

# Message codes, Live Data Feed Specification "Summary of Messages".
MSG_LOGIN_REQUEST = 101
MSG_LOGIN_RESPONSE = 102
MSG_BEST_FIVE_REQUEST = 127
MSG_BEST_FIVE_INFO = 128
MSG_TOUCHLINE_REQUEST = 206
MSG_TOUCHLINE_INFO = 209

LOGON_STATUS_SUCCESS = 10000
LOGON_STATUS_EXPIRING = 10004


def feed_endpoint(session: ChoiceSession) -> Dict[str, Any]:
    """Resolve the feed handler address for this session."""
    client = session.client
    bcast_ip = getattr(client, "bcast_ip", None) if client else None
    bcast_port = getattr(client, "bcast_port", None) if client else None

    if bcast_ip:
        host = str(bcast_ip)
        if not host.startswith(("ws://", "wss://")):
            host = f"wss://{host}"
        return {"host": host, "port": int(bcast_port) if bcast_port else None,
                "source": "logon_response"}

    return {"host": FALLBACK_FEED_HOST, "port": FALLBACK_FEED_PORT,
            "source": "vendor_default"}


class PriceFeedConnection:
    """One user's price feed subscription."""

    def __init__(self, session: ChoiceSession):
        self.session = session
        self._client = None
        self._handlers: List[Callable] = []
        self._subscribed: set = set()
        self._running = False

    def on_tick(self, handler: Callable) -> None:
        self._handlers.append(handler)
        if self._client:
            self._client.on_message(handler)

    def start(self) -> None:
        if self._running:
            return
        # `uses_broker_data`, not `is_live`. `is_live` means "this session sends
        # real orders", which is a statement about money, not about market
        # data. Gating the feed on it refused to start in PAPER — the mode that
        # most needs a quote source, and the one currently blocked by the
        # missing MultipleTouchline REST entitlement.
        if not self.session.uses_broker_data:
            raise ChoiceNotConnected(
                "Connect a Choice account to use the price feed. A sandbox "
                "session has no broker feed to subscribe to."
            )

        from choice_api import PriceFeedSocketClient

        endpoint = feed_endpoint(self.session)
        self._client = PriceFeedSocketClient(
            vendor_id=self.session.vendor_id or "",
            access_token=self.session.access_token or "",
            host=endpoint["host"],
            port=endpoint["port"],
        )
        for handler in self._handlers:
            self._client.on_message(handler)

        self._client.start_websocket()
        self._running = True
        logger.info(
            "Price feed started for user %s via %s (%s)",
            self.session.owner_key, endpoint["host"], endpoint["source"],
        )

    def subscribe(
        self, instruments: List[Dict[str, Any]], depth: bool = False
    ) -> List[str]:
        """Subscribe to ``[{"segment_id": 1, "token": 2885}, ...]``.

        ``depth`` selects Best Five (message 127) instead of touchline (206):
        the five-level bid/ask ladder rather than last trade alone. The two are
        separate subscriptions for the same instrument, so they are tracked
        under separate keys — subscribing to depth must not look like the
        touchline subscription already exists.
        """
        if not self._running:
            self.start()
        if not self.session.session_id:
            raise ChoiceNotConnected("No Choice session id available for subscription.")

        send = (self._client.subscribe_best_five if depth
                else self._client.subscribe_touchline)
        added = []
        for item in instruments:
            segment_id = int(item["segment_id"])
            token = int(item["token"])
            key = f"{segment_id}_{token}" + (":depth" if depth else "")
            if key in self._subscribed:
                continue
            send(
                session_id=self.session.session_id,
                segment_id=segment_id,
                token=token,
            )
            self._subscribed.add(key)
            added.append(key)
        return added

    def stop(self) -> None:
        if self._client and self._running:
            self._client.stop_websocket()
        self._running = False
        self._subscribed.clear()

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def subscriptions(self) -> List[str]:
        return sorted(self._subscribed)


_connections: Dict[str, PriceFeedConnection] = {}
_lock = threading.Lock()


def get_connection(session: ChoiceSession) -> PriceFeedConnection:
    with _lock:
        conn = _connections.get(session.owner_key)
        if conn is None or conn.session is not session:
            conn = PriceFeedConnection(session)
            _connections[session.owner_key] = conn
        return conn


def close_connection(owner_key: str) -> None:
    with _lock:
        conn = _connections.pop(owner_key, None)
    if conn:
        conn.stop()
