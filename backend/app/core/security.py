import bcrypt
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from jose import jwt

from app.config import settings

# bcrypt hashes at most 72 bytes; anything longer is silently ignored by the
# algorithm, so it is truncated explicitly and consistently on both paths.
BCRYPT_MAX_BYTES = 72


def _prepare(password: str) -> bytes:
    return password.encode("utf-8")[:BCRYPT_MAX_BYTES]


def verify_password(plain_password: str, hashed_password: str) -> bool:
    try:
        return bcrypt.checkpw(_prepare(plain_password), hashed_password.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def get_password_hash(password: str) -> str:
    return bcrypt.hashpw(_prepare(password), bcrypt.gensalt()).decode("utf-8")


def create_access_token(
    subject: Any,
    tenant_id: str,
    role: str,
    expires_delta: Optional[timedelta] = None,
) -> str:
    expire = datetime.now(timezone.utc) + (
        expires_delta or timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    payload = {
        "exp": expire,
        "iat": datetime.now(timezone.utc),
        "sub": str(subject),
        "tenant_id": tenant_id,
        "role": role,
    }
    return jwt.encode(
        payload, settings.resolved_secret_key(), algorithm=settings.ALGORITHM
    )
