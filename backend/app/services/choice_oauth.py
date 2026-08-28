"""Choice partner OAuth callback handling.

Two things make the callback safe, and both were missing before:

1. **Binding.** The callback is a browser redirect and carries no Authorization
   header, so it is tied to the user who started the flow by a signed,
   single-use ``state`` token. Without it, anyone who could reach the URL could
   mint a platform session and overwrite a broker connection.

2. **Decryption.** Per the Partner Product Integration Guide s6, every callback
   parameter except ``baseUrl`` arrives AES-encrypted under a vendor-specific
   key issued by the Choice IT team. When that key is not configured the flow
   is disabled rather than falling back to trusting plaintext input.

The AES mode below (CBC + PKCS#7, base64 ciphertext) is the common Choice
partner configuration; confirm it against the key material Choice issues during
integration and adjust ``PARTNER_AES_MODE`` if theirs differs.
"""

import base64
import binascii
import hmac
import logging
import secrets
import threading
import time
from hashlib import sha256
from typing import Any, Dict, Optional
from urllib.parse import quote

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from app.config import settings
from engine.app.choice_gateway.errors import ChoiceAuthError
from engine.app.config import engine_settings

logger = logging.getLogger("choice_oauth")

PARTNER_LOGIN_URL = "https://partner.choiceindia.com/auth/login"
PARTNER_AES_MODE = "CBC"
STATE_TTL_SECONDS = 600
# Parameters Choice encrypts. baseUrl is documented as plain text.
ENCRYPTED_PARAMS = ("cid", "sid", "accessToken")


class _StateStore:
    """Single-use, expiring state tokens bound to a platform user."""

    def __init__(self):
        self._states: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()

    def issue(self, user_id: str) -> str:
        nonce = secrets.token_urlsafe(24)
        signature = hmac.new(
            settings.resolved_secret_key().encode(),
            f"{user_id}:{nonce}".encode(),
            sha256,
        ).hexdigest()[:32]
        state = f"{nonce}.{signature}"
        with self._lock:
            self._purge_locked()
            self._states[state] = {"user_id": user_id, "issued_at": time.time()}
        return state

    def _purge_locked(self) -> None:
        now = time.time()
        for key in [
            k for k, v in self._states.items()
            if now - v["issued_at"] > STATE_TTL_SECONDS
        ]:
            self._states.pop(key, None)

    def consume(self, state: str) -> str:
        """Return the bound user id and invalidate the token."""
        with self._lock:
            self._purge_locked()
            entry = self._states.pop(state, None)
        if not entry:
            raise ChoiceAuthError(
                "This Choice sign-in link has expired or was already used. "
                "Start the connection again."
            )

        nonce, _, signature = state.partition(".")
        expected = hmac.new(
            settings.resolved_secret_key().encode(),
            f"{entry['user_id']}:{nonce}".encode(),
            sha256,
        ).hexdigest()[:32]
        if not hmac.compare_digest(signature, expected):
            raise ChoiceAuthError("Invalid Choice sign-in state.")
        return entry["user_id"]


state_store = _StateStore()


def _decode_key(material: str) -> bytes:
    """Accept a base64 or hex encoded AES key."""
    cleaned = material.strip()
    for decoder in (base64.b64decode, bytes.fromhex):
        try:
            key = decoder(cleaned)
        except (binascii.Error, ValueError):
            continue
        if len(key) in (16, 24, 32):
            return key
    raw = cleaned.encode()
    if len(raw) in (16, 24, 32):
        return raw
    raise ChoiceAuthError(
        "CHOICE_OAUTH_AES_KEY must decode to 16, 24 or 32 bytes "
        "(base64, hex, or raw)."
    )


def is_configured() -> bool:
    return bool(engine_settings.CHOICE_OAUTH_AES_KEY)


def decrypt_param(value: str) -> str:
    """Decrypt one AES-encrypted callback parameter."""
    if not is_configured():
        raise ChoiceAuthError(
            "Choice partner OAuth is not configured on this deployment. "
            "Set CHOICE_OAUTH_AES_KEY to the vendor key issued by Choice."
        )

    key = _decode_key(engine_settings.CHOICE_OAUTH_AES_KEY)
    try:
        ciphertext = base64.b64decode(value)
    except (binascii.Error, ValueError) as exc:
        raise ChoiceAuthError("Callback parameter is not valid base64.") from exc

    if engine_settings.CHOICE_OAUTH_AES_IV:
        iv = _decode_key(engine_settings.CHOICE_OAUTH_AES_IV)[:16]
        body = ciphertext
    else:
        # No configured IV: assume it is prefixed to the ciphertext.
        iv, body = ciphertext[:16], ciphertext[16:]

    if len(body) % 16 != 0 or not body:
        raise ChoiceAuthError("Callback ciphertext has an unexpected length.")

    try:
        decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
        padded = decryptor.update(body) + decryptor.finalize()
        unpadder = padding.PKCS7(algorithms.AES.block_size).unpadder()
        plaintext = unpadder.update(padded) + unpadder.finalize()
    except Exception as exc:
        raise ChoiceAuthError(
            "Could not decrypt the Choice callback parameters. Check that "
            "CHOICE_OAUTH_AES_KEY matches the key Choice issued for this vendor."
        ) from exc

    return plaintext.decode("utf-8", errors="strict").strip()


def decrypt_callback(params: Dict[str, Optional[str]]) -> Dict[str, Optional[str]]:
    """Decrypt the encrypted callback parameters, leaving baseUrl as-is."""
    result: Dict[str, Optional[str]] = {}
    for name in ENCRYPTED_PARAMS:
        raw = params.get(name)
        result[name] = decrypt_param(raw) if raw else None
    result["baseUrl"] = params.get("baseUrl")
    return result


def build_login_url(redirect_url: str, state: str) -> str:
    """Partner login URL to send the user to."""
    separator = "&" if "?" in redirect_url else "?"
    callback = f"{redirect_url}{separator}state={quote(state)}"
    return f"{PARTNER_LOGIN_URL}?redirectUrl={quote(callback, safe='')}"
