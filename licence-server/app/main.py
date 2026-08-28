"""Licence service: who may run the desktop app, and who is running it.

Deliberately separate from the trading platform, and deliberately small. It
holds no credentials, sees no market data, and never touches an order. Its whole
job is to answer two questions:

* may this installation run?
* which installations exist, and when was each last seen?

**What it stores.** A licence per client, and one row per installation: a random
id the app generates for itself, the version, the Choice environment, and
timestamps. No hostname, no username, no positions, no P&L, no strategies. Not
out of squeamishness — holding other people's trading data changes what
obligations the operator is under, and none of it is needed to answer the two
questions above.

**Why the client can run offline.** A desktop app that stops the moment a
network hiccups is worse than no licensing: someone could be managing a live
position. The app keeps working on its last successful check for
``GRACE_DAYS``; only a reachable server saying "revoked", or a grace period
that has run out, stops it.
"""

import os
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import (Boolean, Column, DateTime, Integer, String, create_engine,
                        func)
from sqlalchemy.orm import Session, declarative_base, sessionmaker

# How long an installation may run without reaching this service. Long enough
# that a weekend outage or a client's flaky connection is a non-event; short
# enough that a revoked licence stops mattering within a working week.
GRACE_DAYS = int(os.environ.get("LICENCE_GRACE_DAYS", "7"))

DB_URL = os.environ.get("LICENCE_DATABASE_URL") or (
    "sqlite:///" + str(Path(__file__).resolve().parent.parent / "licences.db")
)

Base = declarative_base()
engine = create_engine(DB_URL, connect_args={"check_same_thread": False}
                       if DB_URL.startswith("sqlite") else {})
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Licence(Base):
    __tablename__ = "licences"
    key = Column(String, primary_key=True)
    client_name = Column(String, nullable=False)
    notes = Column(String, default="")
    revoked = Column(Boolean, default=False, nullable=False)
    # None means unlimited. A seat count stops one key being shared across an
    # office without the operator agreeing to it.
    max_installs = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=now, nullable=False)


class Install(Base):
    __tablename__ = "installs"
    install_id = Column(String, primary_key=True)
    licence_key = Column(String, nullable=False, index=True)
    app_version = Column(String, default="")
    environment = Column(String, default="")
    label = Column(String, default="")
    first_seen = Column(DateTime, default=now, nullable=False)
    last_seen = Column(DateTime, default=now, nullable=False)


Base.metadata.create_all(engine)

app = FastAPI(title="Choice FINX licence service", version="1.0.0")
STATIC = Path(__file__).resolve().parent.parent / "static"


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def operator(authorization: str = Header(default="")) -> None:
    """Guard the operator endpoints with one shared token.

    One person administers this, so a user table would be ceremony. The token
    is required, never defaulted — an unset token must fail closed rather than
    leave the registry world-writable.
    """
    expected = os.environ.get("LICENCE_ADMIN_TOKEN", "")
    if not expected:
        raise HTTPException(
            status_code=503,
            detail="LICENCE_ADMIN_TOKEN is not set on the server; operator "
                   "endpoints are disabled until it is.",
        )
    supplied = authorization.removeprefix("Bearer ").strip()
    # Constant-time: a token guessable by timing is not a token.
    if not secrets.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="Not authorised")


# -- what the app sends ----------------------------------------------------

class ActivateRequest(BaseModel):
    key: str = Field(min_length=8, max_length=128)
    install_id: str = Field(min_length=8, max_length=64)
    app_version: str = Field(default="", max_length=32)
    environment: str = Field(default="", max_length=16)
    # Optional, set by the client's operator: "Rahul's laptop". Never derived
    # from the machine, so nothing is collected that was not typed in.
    label: str = Field(default="", max_length=64)


class IssueRequest(BaseModel):
    client_name: str = Field(min_length=1, max_length=128)
    notes: str = Field(default="", max_length=512)
    max_installs: Optional[int] = Field(default=None, ge=1, le=1000)


def _verdict(licence: Optional[Licence]) -> str:
    if licence is None:
        return "unknown"
    return "revoked" if licence.revoked else "active"


# -- the two endpoints the desktop app calls -------------------------------

@app.post("/api/activate")
def activate(req: ActivateRequest, db: Session = Depends(get_db)):
    """First run on a machine. Records the installation against the licence."""
    licence = db.get(Licence, req.key)
    if licence is None:
        raise HTTPException(status_code=404, detail="That licence key is not recognised.")
    if licence.revoked:
        raise HTTPException(status_code=403, detail="That licence has been revoked.")

    install = db.get(Install, req.install_id)
    if install is None:
        if licence.max_installs is not None:
            used = db.query(func.count(Install.install_id)).filter(
                Install.licence_key == req.key).scalar()
            if used >= licence.max_installs:
                raise HTTPException(
                    status_code=409,
                    detail=f"This licence allows {licence.max_installs} "
                           f"installation(s) and they are all in use.",
                )
        install = Install(install_id=req.install_id, licence_key=req.key,
                          first_seen=now())
        db.add(install)

    install.licence_key = req.key
    install.app_version = req.app_version
    install.environment = req.environment
    install.label = req.label or install.label
    install.last_seen = now()
    db.commit()

    return {"status": "active", "client_name": licence.client_name,
            "grace_days": GRACE_DAYS}


@app.post("/api/heartbeat")
def heartbeat(req: ActivateRequest, db: Session = Depends(get_db)):
    """Periodic check-in. Answers whether the app may keep running.

    Returns a verdict rather than an error for a revoked or unknown licence:
    the app needs to distinguish "the server said stop" from "the server could
    not be reached", and an exception makes those look identical.
    """
    licence = db.get(Licence, req.key)
    install = db.get(Install, req.install_id)
    if install is not None:
        install.app_version = req.app_version or install.app_version
        install.environment = req.environment or install.environment
        install.last_seen = now()
        db.commit()

    return {"status": _verdict(licence), "grace_days": GRACE_DAYS}


# -- what the operator uses ------------------------------------------------

@app.post("/api/licences", dependencies=[Depends(operator)])
def issue(req: IssueRequest, db: Session = Depends(get_db)):
    key = "CFX-" + "-".join(secrets.token_hex(3).upper() for _ in range(3))
    licence = Licence(key=key, client_name=req.client_name, notes=req.notes,
                      max_installs=req.max_installs)
    db.add(licence)
    db.commit()
    return {"key": key, "client_name": req.client_name,
            "max_installs": req.max_installs}


@app.get("/api/licences", dependencies=[Depends(operator)])
def list_licences(db: Session = Depends(get_db)):
    """Every licence with its installations — the "who and how many" view."""
    cutoff = now() - timedelta(days=GRACE_DAYS)
    out = []
    for licence in db.query(Licence).order_by(Licence.created_at.desc()).all():
        installs = db.query(Install).filter(
            Install.licence_key == licence.key).order_by(Install.last_seen.desc()).all()
        out.append({
            "key": licence.key,
            "client_name": licence.client_name,
            "notes": licence.notes,
            "revoked": licence.revoked,
            "max_installs": licence.max_installs,
            "created_at": licence.created_at.isoformat(),
            "installs": [{
                "install_id": i.install_id,
                "label": i.label,
                "app_version": i.app_version,
                "environment": i.environment,
                "first_seen": i.first_seen.isoformat(),
                "last_seen": i.last_seen.isoformat(),
                # "Has this one gone quiet?" is the question a list of
                # timestamps makes the reader compute for themselves.
                "stale": i.last_seen < cutoff,
            } for i in installs],
        })
    return {"grace_days": GRACE_DAYS, "licences": out}


@app.post("/api/licences/{key}/revoke", dependencies=[Depends(operator)])
def revoke(key: str, db: Session = Depends(get_db)):
    licence = db.get(Licence, key)
    if licence is None:
        raise HTTPException(status_code=404, detail="No such licence")
    licence.revoked = True
    db.commit()
    return {"key": key, "revoked": True,
            "takes_effect": f"within {GRACE_DAYS} days, or at the next check-in"}


@app.post("/api/licences/{key}/restore", dependencies=[Depends(operator)])
def restore(key: str, db: Session = Depends(get_db)):
    licence = db.get(Licence, key)
    if licence is None:
        raise HTTPException(status_code=404, detail="No such licence")
    licence.revoked = False
    db.commit()
    return {"key": key, "revoked": False}


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/")
def dashboard():
    page = STATIC / "index.html"
    if not page.is_file():
        return {"detail": "Dashboard not installed"}
    return FileResponse(page)
