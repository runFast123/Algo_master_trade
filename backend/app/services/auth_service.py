import logging
import secrets
from typing import Any, Dict, Optional

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.core.security import create_access_token, get_password_hash, verify_password
from app.database import db_url
from app.models.tenant import Tenant
from app.models.user import User
from app.repositories.audit_repo import audit_repo
from app.schemas.auth import (
    ChoiceTotpRequest,
    ChoiceValidateTotpRequest,
    LoginRequest,
    RegisterRequest,
    TokenResponse,
)
from engine.app.choice_gateway.client_manager import (
    ChoiceSession,
    SessionMode,
    choice_sessions,
    is_demo_credential,
)

def _database_file() -> str:
    """The database file in use, for a sign-in failure that would otherwise be
    indistinguishable from a wrong password."""
    return db_url.rsplit("/", 1)[-1] or db_url


def _database_label() -> str:
    """"desktop" when running from the executable's data directory, else
    "source" — the two are separate and an account exists in only one."""
    return "desktop" if "ChoiceFinxTrader" in db_url else "source"


logger = logging.getLogger("auth")

MIN_PASSWORD_LENGTH = 10


class AuthService:
    # -- platform accounts -------------------------------------------------

    @staticmethod
    def _issue(user: User) -> TokenResponse:
        return TokenResponse(
            access_token=create_access_token(
                subject=user.id, tenant_id=user.tenant_id, role=user.role
            ),
            user_id=user.id,
            tenant_id=user.tenant_id,
            role=user.role,
        )

    @staticmethod
    def register_user(db: Session, req: RegisterRequest) -> TokenResponse:
        if len(req.password) < MIN_PASSWORD_LENGTH:
            raise HTTPException(
                status_code=400,
                detail=f"Password must be at least {MIN_PASSWORD_LENGTH} characters",
            )
        if db.query(User).filter(User.email == req.email).first():
            raise HTTPException(
                status_code=400, detail="An account with this email already exists"
            )

        tenant = Tenant(name=req.tenant_name, plan="STANDARD")
        db.add(tenant)
        db.flush()

        user = User(
            email=req.email,
            hashed_password=get_password_hash(req.password),
            full_name=req.full_name,
            role="trader",
            tenant_id=tenant.id,
        )
        db.add(user)
        db.commit()
        db.refresh(user)

        audit_repo.log(
            db, actor_id=user.id, tenant_id=user.tenant_id,
            action="USER_REGISTERED", entity_type="user", entity_id=user.id,
        )
        return AuthService._issue(user)

    @staticmethod
    def authenticate_user(db: Session, req: LoginRequest) -> TokenResponse:
        user = db.query(User).filter(User.email == req.email).first()
        # Hash even when the user is missing so a failed lookup and a wrong
        # password take the same time.
        hashed = user.hashed_password if user else get_password_hash(secrets.token_hex(8))
        if not verify_password(req.password, hashed) or not user:
            # Name the database. The executable and a source checkout use
            # different files, so an account created in one is genuinely absent
            # from the other and "incorrect password" is misleading on its own.
            # The message stays identical whether the account is missing or the
            # password is wrong, so it still discloses nothing about who exists.
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=(
                    "Incorrect email or password. "
                    f"This app is using its {_database_label()} database "
                    f"({_database_file()}); an account created against the other "
                    "one will not be found here — see README troubleshooting."
                ),
            )
        if not user.is_active:
            raise HTTPException(status_code=403, detail="This account is disabled")

        audit_repo.log(
            db, actor_id=user.id, tenant_id=user.tenant_id,
            action="USER_LOGIN", entity_type="user", entity_id=user.id,
        )
        return AuthService._issue(user)

    # -- Choice broker connection -----------------------------------------
    #
    # Connecting a Choice account is a separate action from signing in to the
    # platform. The broker session is attached to the already-authenticated
    # user, and a Choice API key is never used as platform credentials.

    @staticmethod
    def connect_choice(
        db: Session, user: User, req: ChoiceTotpRequest
    ) -> Dict[str, Any]:
        session = choice_sessions.get_or_create(str(user.id))

        # DEMO credentials always mean sandbox, whatever mode was requested.
        if req.mode == "sandbox" or is_demo_credential(req.vendor_id, req.api_key):
            session.start_demo(req.vendor_id, custom_margin=req.custom_margin)
            action = "CHOICE_SANDBOX_CONNECTED"
        else:
            # Anything other than an explicit "live" stays in paper mode, so a
            # missing or malformed mode can never route real orders.
            paper = req.mode != "live"
            session.login_totp(
                req.vendor_id, req.api_key, req.mobile_no, paper=paper,
                remember=bool(getattr(req, "remember", False)),
                environment=getattr(req, "environment", None),
            )
            action = (
                "CHOICE_PAPER_CONNECTED" if paper else "CHOICE_LIVE_CONNECTED"
            )

        audit_repo.log(
            db, actor_id=user.id, tenant_id=user.tenant_id, action=action,
            entity_type="choice_session", entity_id=str(user.id),
            details={
                "vendor_id": req.vendor_id,
                "mode": session.mode.value,
                "environment": session.environment,
                "sends_real_orders": session.mode.sends_real_orders,
            },
        )
        return {"status": "SUCCESS", **session.describe()}

    @staticmethod
    def request_choice_totp(user: User, req: ChoiceTotpRequest) -> Dict[str, Any]:
        session = choice_sessions.get_or_create(str(user.id))
        otp = session.request_totp(req.vendor_id, req.api_key, req.mobile_no)
        return {
            "status": "SUCCESS",
            "message": "OTP requested",
            "automatic_otp": otp,
        }

    @staticmethod
    def validate_choice_totp(
        db: Session, user: User, req: ChoiceValidateTotpRequest
    ) -> Dict[str, Any]:
        session = choice_sessions.get_or_create(str(user.id))
        session.validate_totp(req.vendor_id, req.api_key, req.mobile_no, req.otp)
        audit_repo.log(
            db, actor_id=user.id, tenant_id=user.tenant_id,
            action="CHOICE_LIVE_CONNECTED", entity_type="choice_session",
            entity_id=str(user.id), details={"vendor_id": req.vendor_id},
        )
        return {"status": "SUCCESS", **session.describe()}

    @staticmethod
    def attach_partner_session(
        db: Session,
        user: User,
        cid: str,
        sid: str,
        access_token: Optional[str],
        base_url: Optional[str],
    ) -> Dict[str, Any]:
        session = choice_sessions.get_or_create(str(user.id))
        session.attach_partner_session(
            cid=cid, sid=sid, access_token=access_token, base_url=base_url
        )
        audit_repo.log(
            db, actor_id=user.id, tenant_id=user.tenant_id,
            action="CHOICE_OAUTH_CONNECTED", entity_type="choice_session",
            entity_id=str(user.id), details={"vendor_id": cid},
        )
        return {"status": "SUCCESS", **session.describe()}

    @staticmethod
    def set_mode(db: Session, user: User, mode: str,
                 confirm: bool = False) -> Dict[str, Any]:
        """Switch a connected session between paper and live.

        Both directions, because a user who signed in with their own
        credentials is entitled to decide whether their own orders are real.
        The two directions are not symmetric though:

        * **To paper** — immediate. Reducing what a session may do proves
          nothing by being slow, and someone who wants to stop risking money
          should never have to find a credential first.
        * **To live** — requires an explicit acknowledgement in the request.
          Not a password prompt: the user authenticated minutes ago and the
          session already holds their key. What it guards against is a
          mis-click turning simulated orders into real ones, which is why the
          confirmation states the consequence rather than asking "are you
          sure".

        A sandbox session can never become live whatever is sent. It has no
        Choice login behind it, so "live" would mean orders with nowhere to go.
        """
        wanted = str(mode or "").strip().upper()
        if wanted not in {"PAPER", "LIVE"}:
            raise HTTPException(
                status_code=422,
                detail="mode must be either 'paper' or 'live'.",
            )

        session = choice_sessions.get(str(user.id))
        if session is None or not session.is_connected:
            raise HTTPException(status_code=409, detail="No Choice session is connected.")

        if session.is_demo:
            raise HTTPException(
                status_code=409,
                detail="This is a sandbox session with no Choice login behind it. "
                       "Connect with your Choice credentials to trade for real.",
            )

        target = SessionMode.LIVE if wanted == "LIVE" else SessionMode.PAPER
        if session.mode is target:
            return {"status": "SUCCESS", "changed": False, **session.describe()}

        if target is SessionMode.LIVE and not confirm:
            raise HTTPException(
                status_code=400,
                detail="Switching to live sends real orders with real money. "
                       "Confirm the switch to proceed.",
            )

        previous = session.mode.value
        session.mode = target
        # Persist the change, or a restart resurrects the mode this session was
        # switched *away* from — and `restore()` is reached automatically from
        # /auth/choice/status, so a user who chose paper could be silently put
        # back into live without the confirmation that guards that direction.
        session.persist()
        session.touch()
        audit_repo.log(
            db, actor_id=user.id, tenant_id=user.tenant_id,
            action=("CHOICE_MODE_TO_LIVE" if target is SessionMode.LIVE
                    else "CHOICE_MODE_TO_PAPER"),
            entity_type="choice_session", entity_id=str(user.id),
            details={"from": previous, "to": target.value,
                     "environment": session.environment,
                     "sends_real_orders": target.sends_real_orders},
        )
        logger.warning(
            "User %s switched Choice mode %s -> %s on %s; real orders: %s",
            user.id, previous, target.value, session.environment,
            target.sends_real_orders,
        )
        return {"status": "SUCCESS", "changed": True, **session.describe()}


    @staticmethod
    def disconnect_choice(db: Session, user: User) -> Dict[str, Any]:
        # Disconnecting is an explicit instruction to forget, so the stored
        # session goes with the live one.
        existing = choice_sessions.get(str(user.id))
        if existing is not None:
            existing.forget()
        else:
            from engine.app.choice_gateway import session_store
            session_store.clear(str(user.id))
        choice_sessions.drop(str(user.id))
        audit_repo.log(
            db, actor_id=user.id, tenant_id=user.tenant_id,
            action="CHOICE_DISCONNECTED", entity_type="choice_session",
            entity_id=str(user.id),
        )
        return {"status": "SUCCESS", "mode": "DISCONNECTED"}

    @staticmethod
    def choice_status(user: User) -> Dict[str, Any]:
        session: Optional[ChoiceSession] = choice_sessions.get(str(user.id))
        if session is not None and session.is_connected:
            return {"connected": True, **session.describe()}

        # Nothing live. If this user opted in to being remembered, try the
        # stored session — validated against Choice inside restore(), so a
        # stale id is discarded rather than reported as connected.
        from engine.app.choice_gateway import session_store

        stored = session_store.load(str(user.id))
        if stored:
            candidate = choice_sessions.get_or_create(str(user.id))
            try:
                if candidate.restore(stored):
                    return {"connected": True, "restored": True,
                            **candidate.describe()}
            except Exception as exc:      # never let a restore break sign-in
                logger.warning("Broker session restore failed: %s", exc)

        return {"connected": False, "mode": "DISCONNECTED"}
