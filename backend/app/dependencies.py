from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models.user import User
from app.schemas.auth import TokenData
from engine.app.choice_gateway.client_manager import ChoiceSession, choice_sessions

oauth2_scheme = OAuth2PasswordBearer(tokenUrl=f"{settings.API_V1_STR}/auth/token")

CREDENTIALS_EXCEPTION = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Could not validate credentials",
    headers={"WWW-Authenticate": "Bearer"},
)


def get_current_user(
    token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)
) -> User:
    try:
        payload = jwt.decode(
            token, settings.resolved_secret_key(), algorithms=[settings.ALGORITHM]
        )
        user_id = payload.get("sub")
        tenant_id = payload.get("tenant_id")
        if not user_id or not tenant_id:
            raise CREDENTIALS_EXCEPTION
        token_data = TokenData(
            user_id=user_id, tenant_id=tenant_id, role=payload.get("role")
        )
    except JWTError:
        raise CREDENTIALS_EXCEPTION

    user = (
        db.query(User)
        .filter(User.id == token_data.user_id, User.is_active.is_(True))
        .first()
    )
    if user is None:
        raise CREDENTIALS_EXCEPTION

    # The tenant is re-read from the database rather than trusted from the
    # token, so a stale or tampered claim cannot widen access.
    if user.tenant_id != token_data.tenant_id:
        raise CREDENTIALS_EXCEPTION
    return user


def get_current_admin(current_user: User = Depends(get_current_user)) -> User:
    if current_user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin privileges required",
        )
    return current_user


def get_choice_session(
    current_user: User = Depends(get_current_user),
) -> ChoiceSession:
    """This user's Choice session.

    Sessions are per user; there is no shared broker connection. A user who has
    not connected their Choice account gets 409 rather than another user's data.
    """
    session = choice_sessions.get(str(current_user.id))
    if session is None or not session.is_connected:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Not connected to Choice FINX. Sign in to your Choice account first.",
        )
    return session
