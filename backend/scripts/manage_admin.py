r"""Grant or revoke the admin role.

Registration always creates a trader, so the first admin has to be promoted
deliberately from the machine that owns the database. There is no self-service
path to admin, and no default admin account with a known password.

    python backend/scripts/manage_admin.py list
    python backend/scripts/manage_admin.py grant you@example.com
    python backend/scripts/manage_admin.py revoke you@example.com

The executable and a source checkout deliberately use different databases —
``%LOCALAPPDATA%\ChoiceFinxTrader\algo.db`` and ``algo.db`` in the repository
respectively — so an account created before the data directory moved exists in
only one of them and appears as "incorrect password" in the other. ``copy-user``
moves one across, password hash intact:

    python backend/scripts/manage_admin.py databases
    python backend/scripts/manage_admin.py copy-user you@example.com --to desktop
"""

import argparse
import os
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "backend"))

from app.database import SessionLocal, db_url  # noqa: E402
from app.db_migrate import sync_schema  # noqa: E402
from app.models.user import User  # noqa: E402
from app.repositories.audit_repo import audit_repo  # noqa: E402


def set_role(email: str, role: str) -> int:
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.email == email).first()
        if user is None:
            print(f"No account found for {email}")
            return 1
        if user.role == role:
            print(f"{email} is already {role}")
            return 0

        previous = user.role
        user.role = role
        db.commit()
        audit_repo.log(
            db, actor_id=user.id, tenant_id=user.tenant_id, action="ROLE_CHANGED",
            entity_type="user", entity_id=user.id,
            details={"from": previous, "to": role, "via": "manage_admin.py"},
        )
        print(f"{email}: {previous} -> {role}")
        return 0
    finally:
        db.close()


def list_users() -> int:
    db = SessionLocal()
    try:
        users = db.query(User).order_by(User.created_at).all()
        if not users:
            print("No accounts yet.")
            return 0
        width = max(len(u.email) for u in users)
        print(f"{'EMAIL'.ljust(width)}  ROLE     ACTIVE")
        for user in users:
            print(f"{user.email.ljust(width)}  {user.role:<8} {user.is_active}")
        return 0
    finally:
        db.close()


def _database_paths() -> dict:
    """The two databases this project can be pointed at."""
    local = os.environ.get("LOCALAPPDATA", "")
    return {
        "source": REPO_ROOT / "algo.db",
        "desktop": Path(local) / "ChoiceFinxTrader" / "algo.db" if local else None,
    }


def show_databases() -> int:
    for name, path in _database_paths().items():
        if path is None:
            print(f"{name:8} (LOCALAPPDATA not set)")
            continue
        if not path.exists():
            print(f"{name:8} {path}  (does not exist)")
            continue
        try:
            con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            n = con.execute("select count(*) from users").fetchone()[0]
            emails = [r[0] for r in con.execute(
                "select email from users order by created_at limit 6")]
            con.close()
            listed = ", ".join(emails) + (" ..." if n > len(emails) else "")
            print(f"{name:8} {path}")
            print(f"         {n} account(s): {listed}")
        except sqlite3.Error as exc:
            print(f"{name:8} {path}  (unreadable: {exc})")
    return 0


def copy_user(email: str, target: str) -> int:
    """Copy one account, with its tenant, between the two databases.

    The password hash is carried across unchanged, so the same password keeps
    working. An account that already exists in the target is left alone rather
    than overwritten — this is a rescue tool, not a sync.
    """
    paths = _database_paths()
    source_name = "desktop" if target == "source" else "source"
    src, dst = paths[source_name], paths[target]

    if src is None or not src.exists():
        print(f"Source database ({source_name}) not found: {src}")
        return 1
    if dst is None:
        print("Target database path unknown (LOCALAPPDATA not set).")
        return 1

    dst.parent.mkdir(parents=True, exist_ok=True)
    # Create the schema in the target if it does not exist yet.
    os.environ["DATABASE_URL"] = f"sqlite:///{dst.as_posix()}"
    from importlib import reload
    import app.database as database_module
    reload(database_module)
    from app.db_migrate import sync_schema as sync_target
    sync_target()

    scon = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    scon.row_factory = sqlite3.Row
    row = scon.execute("select * from users where email = ?", (email,)).fetchone()
    if row is None:
        print(f"No account for {email} in the {source_name} database.")
        scon.close()
        return 1
    tenant = scon.execute("select * from tenants where id = ?",
                          (row["tenant_id"],)).fetchone()
    scon.close()

    dcon = sqlite3.connect(dst)
    dcon.row_factory = sqlite3.Row
    if dcon.execute("select 1 from users where email = ?", (email,)).fetchone():
        print(f"{email} already exists in the {target} database; nothing copied.")
        dcon.close()
        return 0

    def insert(table, record):
        cols = ", ".join(record.keys())
        marks = ", ".join("?" for _ in record)
        dcon.execute(f"insert or ignore into {table} ({cols}) values ({marks})",
                     tuple(record.values()))

    if tenant is not None:
        insert("tenants", dict(tenant))
    insert("users", dict(row))
    dcon.commit()
    dcon.close()
    print(f"Copied {email} ({source_name} -> {target}). "
          "The same password still works.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="List accounts and roles")
    grant = sub.add_parser("grant", help="Give an account the admin role")
    grant.add_argument("email")
    revoke = sub.add_parser("revoke", help="Return an account to the trader role")
    revoke.add_argument("email")
    sub.add_parser("databases", help="Show both databases and who is in each")
    copy = sub.add_parser("copy-user", help="Copy an account between databases")
    copy.add_argument("email")
    copy.add_argument("--to", choices=("desktop", "source"), required=True,
                      help="Which database to copy the account into")

    args = parser.parse_args()

    # These two span both databases, so the single-database banner and schema
    # sync below would name the wrong one.
    if args.command == "databases":
        return show_databases()
    if args.command == "copy-user":
        return copy_user(args.email, args.to)

    print(f"Database: {db_url}\n")
    sync_schema()

    if args.command == "list":
        return list_users()
    if args.command == "grant":
        return set_role(args.email, "admin")
    return set_role(args.email, "trader")


if __name__ == "__main__":
    raise SystemExit(main())
