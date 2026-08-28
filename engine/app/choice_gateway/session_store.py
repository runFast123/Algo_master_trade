"""Optional on-disk persistence for broker sessions.

Broker sessions live in memory, so restarting the app drops them while the
platform sign-in — a signed token in the browser — survives. The result is a
user who is still logged in but disconnected from Choice, with nothing saying
why. This module lets a user opt in to keeping the broker session across a
restart on their own machine.

Encryption is Fernet from ``cryptography`` — AES-128-CBC with an HMAC-SHA256
signature — which was already a dependency. An earlier version hand-rolled a
SHA256 keystream and a separate HMAC; writing your own cipher when a real one
is installed is a way to get something weaker in more lines.

The key comes from ``SECRET_KEY`` where a deployment sets one, and otherwise
from a generated key file beside the store. Requiring ``SECRET_KEY`` looked
like the safer rule and was not: the desktop app never sets it, so the feature
silently did nothing on the one deployment with no other way to persist.

**What this protects against, honestly.** Either way the key lives on the same
machine as the ciphertext, so this defeats a casual file read or a backup
scraper — not someone at the keyboard. The upgrade for a real threat model is
OS-level key storage (DPAPI on Windows), which is why the cipher is isolated
here rather than spread through the session code.

What is stored: the Choice ``SessionId``, the environment, and the mode.
What is never stored: the API key, the password, or anything about orders.
"""

import base64
import hashlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger("choice_gateway")

# A stored session is worthless once Choice's own session has lapsed, so it is
# not kept longer than the app's own inactivity window.
MAX_AGE_SECONDS = 8 * 60 * 60


def _store_path() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return Path(base) / "ChoiceFinxTrader" / "sessions.json"


def _cipher() -> Fernet:
    """A Fernet cipher, keyed from ``SECRET_KEY`` where one is configured."""
    from engine.app.config import engine_settings

    secret = getattr(engine_settings, "SECRET_KEY", None) or os.environ.get(
        "SECRET_KEY", ""
    )
    if secret:
        digest = hashlib.sha256(f"choice-session-store::{secret}".encode()).digest()
        return Fernet(base64.urlsafe_b64encode(digest))
    return Fernet(_local_key())


def _local_key() -> bytes:
    """A machine-local key, for a desktop install with no ``SECRET_KEY``.

    A server deployment configures one and every process shares it. The desktop
    app does not: ``backend/app/config.py`` generates a *random* signing key per
    process when none is set, so deriving from it would leave each restart
    unable to read what the last one wrote — silently identical to not
    persisting at all, which is the problem this module exists to solve.

    A key file beside the store fits the threat model already stated above: it
    stops a file read or a backup scraper, not someone at the keyboard.
    """
    path = _store_path().with_name("session.key")
    try:
        # Validate before returning it. A truncated or hand-edited file would
        # otherwise raise out of save(), failing a login that had already
        # succeeded — persistence must never break the live session. A fresh
        # key just makes existing records undecryptable, which load() already
        # handles by discarding them.
        key = path.read_bytes()
        Fernet(key)
        return key
    except (OSError, ValueError, TypeError):
        pass

    try:
        key = Fernet.generate_key()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(key)
        return key
    except OSError as exc:
        # Nowhere to keep a key means nothing can be persisted. Say so rather
        # than inventing a constant, which would make the ciphertext portable
        # between machines and worse than storing nothing.
        raise RuntimeError(f"no SECRET_KEY and no writable key file: {exc}") from exc


def _read_all() -> Dict[str, Any]:
    path = _store_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("Could not read the session store (%s); ignoring it.", exc)
        return {}


def _write_all(payload: Dict[str, Any]) -> None:
    path = _store_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")
    except OSError as exc:
        # Failing to persist must never break the live session.
        logger.warning("Could not write the session store: %s", exc)


def save(owner_key: str, session_id: str, environment: str, mode: str,
         vendor_id: str = "", base_url: str = "") -> None:
    """Persist one user's broker session. Never stores the API key."""
    if not session_id:
        return
    try:
        cipher = _cipher()
    except RuntimeError as exc:
        logger.warning("Not persisting the broker session: %s", exc)
        return

    body = json.dumps({
        "session_id": session_id,
        "environment": environment,
        "mode": mode,
        "vendor_id": vendor_id,
        "base_url": base_url,
        "saved_at": time.time(),
    }).encode("utf-8")

    # Fernet signs as well as encrypts, so a tampered record fails to decrypt
    # rather than yielding nonsense that gets used as a live session id.
    store = _read_all()
    store[owner_key] = cipher.encrypt(body).decode()
    _write_all(store)


def load(owner_key: str) -> Optional[Dict[str, Any]]:
    """Return a previously saved session, or None if absent, stale or tampered."""
    record = _read_all().get(owner_key)
    if not record:
        return None
    try:
        cipher = _cipher()
    except RuntimeError:
        return None

    try:
        data = json.loads(cipher.decrypt(str(record).encode()).decode("utf-8"))
    except (InvalidToken, ValueError, TypeError) as exc:
        # Tampered, truncated, or written under a different SECRET_KEY.
        logger.warning("Stored broker session was unreadable (%s); discarding.", exc)
        clear(owner_key)
        return None

    if time.time() - data.get("saved_at", 0) > MAX_AGE_SECONDS:
        clear(owner_key)
        return None
    return data


def clear(owner_key: str) -> None:
    """Forget one user's stored session — sign-out, disconnect, env change."""
    store = _read_all()
    if store.pop(owner_key, None) is not None:
        _write_all(store)
