"""Database engine and session factory.

The desktop build must not write beside its own executable: that directory is
read-only under Program Files and is shared between Windows accounts. User
data goes to the per-user application data directory instead.
"""

import logging
import os
import sys
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from app.config import settings

logger = logging.getLogger("database")

APP_DIR_NAME = "ChoiceFinxTrader"


def user_data_dir() -> Path:
    """Per-user, writable directory for application data."""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    elif sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    path = Path(base) / APP_DIR_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def resolve_database_url() -> str:
    if settings.DATABASE_URL:
        return settings.DATABASE_URL

    if getattr(sys, "frozen", False):
        db_path = user_data_dir() / "algo.db"
    else:
        repo_root = Path(__file__).resolve().parents[2]
        db_path = repo_root / "algo.db"

    return f"sqlite:///{db_path.as_posix()}"


db_url = resolve_database_url()

# check_same_thread is required because FastAPI serves requests on a threadpool.
connect_args = {"check_same_thread": False} if db_url.startswith("sqlite") else {}

engine = create_engine(db_url, connect_args=connect_args, echo=False, future=True)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
