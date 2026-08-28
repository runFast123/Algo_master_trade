"""Schema reconciliation for the bundled SQLite database.

The desktop build ships without a migration tool, so a database created by an
older version can lag the models. Two cases are handled, and only two:

* a table that does not exist yet is created;
* a table whose NOT NULL constraints are stricter than the model is rebuilt,
  but only while it is empty — SQLite cannot relax a constraint in place, and
  discarding rows to do so is never the right trade.

Anything else is reported rather than guessed at. A desktop app should not
carry a general-purpose migration engine to solve a problem it has had once.
"""

import logging
from typing import Dict, List

from sqlalchemy import inspect, text

from app.database import Base, engine

logger = logging.getLogger("db_migrate")


def sync_schema() -> Dict[str, List[str]]:
    """Create missing tables and rebuild empty ones whose constraints drifted."""
    Base.metadata.create_all(bind=engine)

    inspector = inspect(engine)
    changes: Dict[str, List[str]] = {}

    for table_name, table in Base.metadata.tables.items():
        if not inspector.has_table(table_name):
            continue

        model_nullable = {c.name: c.nullable for c in table.columns}
        db_columns = inspector.get_columns(table_name)

        missing = [c.name for c in table.columns
                   if c.name not in {col["name"] for col in db_columns}]
        drifted = [col["name"] for col in db_columns
                   if col["name"] in model_nullable
                   and not col["nullable"] and model_nullable[col["name"]]]

        if not missing and not drifted:
            continue

        with engine.begin() as conn:
            rows = conn.execute(text(f'SELECT COUNT(*) FROM "{table_name}"')).scalar_one()

        if rows:
            logger.warning(
                "Table %s has drifted from the model (missing: %s, stricter: %s) and "
                "holds %s rows. Migrate it manually; leaving it untouched.",
                table_name, missing or "none", drifted or "none", rows,
            )
            continue

        logger.info("Rebuilding empty table %s to match the model", table_name)
        table.drop(bind=engine, checkfirst=True)
        table.create(bind=engine)
        changes[table_name] = ["rebuilt"]

    return changes
