from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Query, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import get_current_user
from app.models.user import User
from app.schemas.auth import (
    ChoiceSessionStatus,
    ChoiceTotpRequest,
    ChoiceValidateTotpRequest,
    LoginRequest,
    RegisterRequest,
    TokenResponse,
)
from app.schemas.user import UserResponse
from app.services import choice_oauth
from app.services.auth_service import AuthService
from pydantic import BaseModel, Field

router = APIRouter()


# -- platform accounts -----------------------------------------------------

@router.post("/register", response_model=TokenResponse,
             status_code=status.HTTP_201_CREATED)
def register(req: RegisterRequest, db: Session = Depends(get_db)):
    return AuthService.register_user(db, req)


@router.post("/login", response_model=TokenResponse)
def login(req: LoginRequest, db: Session = Depends(get_db)):
    return AuthService.authenticate_user(db, req)


@router.post("/token", response_model=TokenResponse)
def login_for_access_token(
    form_data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)
):
    return AuthService.authenticate_user(
        db, LoginRequest(email=form_data.username, password=form_data.password)
    )


@router.get("/me", response_model=UserResponse)
def read_current_user(current_user: User = Depends(get_current_user)):
    return current_user


# -- Choice broker connection ---------------------------------------------
#
# Every route below requires an authenticated platform user. The broker session
# is attached to that user and is not shared with anyone else.

@router.get("/choice/status", response_model=ChoiceSessionStatus)
def choice_status(current_user: User = Depends(get_current_user)):
    return AuthService.choice_status(current_user)


@router.post("/choice/connect")
def choice_connect(
    req: ChoiceTotpRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """Connect a Choice account via the TOTP flow, or start a sandbox session."""
    return AuthService.connect_choice(db, current_user, req)


@router.post("/choice/request-otp")
def choice_request_otp(
    req: ChoiceTotpRequest, current_user: User = Depends(get_current_user)
) -> Dict[str, Any]:
    return AuthService.request_choice_totp(current_user, req)


@router.post("/choice/validate-otp")
def choice_validate_otp(
    req: ChoiceValidateTotpRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    return AuthService.validate_choice_totp(db, current_user, req)


class ChoiceModeRequest(BaseModel):
    """Switch a connected session between paper and live."""

    mode: str = Field(description="paper or live")
    # Required to go live. Not a second password — the user authenticated
    # minutes ago — but a deliberate act, so a mis-click cannot turn simulated
    # orders into real ones.
    confirm: bool = Field(default=False)


@router.post("/choice/mode")
def choice_set_mode(
    req: ChoiceModeRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """Choose whether this session's orders are simulated or real.

    Both directions are available to a session that signed in with real
    credentials: someone using their own account is entitled to decide whether
    their own orders are real. They are not symmetric, though — switching to
    paper is immediate, switching to live must be confirmed.
    """
    return AuthService.set_mode(db, current_user, req.mode, req.confirm)


@router.post("/choice/disconnect")
def choice_disconnect(
    current_user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> Dict[str, Any]:
    return AuthService.disconnect_choice(db, current_user)


# -- Choice partner OAuth --------------------------------------------------

@router.post("/choice/oauth/start")
def choice_oauth_start(
    redirect_url: str = Query(..., description="Callback URL registered with Choice"),
    current_user: User = Depends(get_current_user),
) -> Dict[str, Any]:
    """Begin the partner OAuth flow.

    Returns the Choice login URL carrying a single-use ``state`` that binds the
    callback to this user. Without it the callback could not tell which account
    a redirect belongs to.
    """
    if not choice_oauth.is_configured():
        return {
            "enabled": False,
            "detail": "Partner OAuth is not configured. Set CHOICE_OAUTH_AES_KEY "
                      "to the vendor key issued by the Choice IT team.",
        }
    state = choice_oauth.state_store.issue(str(current_user.id))
    return {
        "enabled": True,
        "login_url": choice_oauth.build_login_url(redirect_url, state),
        "expires_in": choice_oauth.STATE_TTL_SECONDS,
    }


@router.get("/choice/oauth/callback")
def choice_oauth_callback(
    state: str = Query(..., description="Single-use state issued by /oauth/start"),
    cid: str = Query(..., description="AES-encrypted client id"),
    sid: str = Query(..., description="AES-encrypted session id"),
    accessToken: Optional[str] = Query(None, description="AES-encrypted 2FA token"),
    baseUrl: Optional[str] = Query(None, description="API base URL (plain text)"),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """Handle the Choice partner redirect.

    The state must match one issued to a signed-in user, and the parameters are
    AES-decrypted with the vendor key before use.
    """
    user_id = choice_oauth.state_store.consume(state)
    decrypted = choice_oauth.decrypt_callback(
        {"cid": cid, "sid": sid, "accessToken": accessToken, "baseUrl": baseUrl}
    )

    user = db.query(User).filter(User.id == user_id, User.is_active.is_(True)).first()
    if user is None:
        from fastapi import HTTPException
        raise HTTPException(status_code=401, detail="Could not validate credentials")

    return AuthService.attach_partner_session(
        db,
        user,
        cid=decrypted["cid"],
        sid=decrypted["sid"],
        access_token=decrypted["accessToken"],
        base_url=decrypted["baseUrl"],
    )
