"""Initialize shared storage once before starting application replicas."""

import hashlib
import json

from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.catalogue import ACTIVITIES, VENUES, seed_catalogue
from app.database import Base, make_database
from app.models import DatabaseState

SCHEMA_VERSION = 1
SEED_REVISION = 2  # Reconcile legacy catalogues once when retirement rules change.
STATE_ID = "offscript"
BOOTSTRAP_LOCK = 683407928114  # One stable lock shared by every app/bootstrap process.


def catalogue_digest():
    payload = json.dumps([SEED_REVISION, VENUES, ACTIVITIES], sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def initialize_database(engine):
    """Serialize DDL and seeding, and commit the completion marker atomically."""
    digest = catalogue_digest()
    with engine.begin() as connection:
        if engine.dialect.name == "postgresql":
            connection.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": BOOTSTRAP_LOCK})
        elif engine.dialect.name == "sqlite":
            # sqlite3's legacy transaction mode does not begin a DDL transaction.
            connection.exec_driver_sql("BEGIN IMMEDIATE")
        Base.metadata.create_all(connection)
        with Session(bind=connection) as session:
            state = session.get(DatabaseState, STATE_ID)
            if state and state.schema_version != SCHEMA_VERSION:
                raise RuntimeError("Database schema version requires an explicit migration.")
            if state and state.catalogue_digest == digest:
                return False
            seed_catalogue(session)
            if state is None:
                session.add(DatabaseState(id=STATE_ID, schema_version=SCHEMA_VERSION, catalogue_digest=digest))
            else:
                state.catalogue_digest = digest
            session.flush()
    return True


def require_initialized_database(engine):
    """Replica startup reads state; it never creates tables or rewrites data."""
    try:
        with engine.connect() as connection:
            version = connection.scalar(select(DatabaseState.schema_version).where(DatabaseState.id == STATE_ID))
    except SQLAlchemyError:
        raise RuntimeError("Database is unavailable or uninitialized. Run python -m app.bootstrap before starting the app.") from None
    if version != SCHEMA_VERSION:
        raise RuntimeError("Database initialization or schema migration is required before starting the app.")


if __name__ == "__main__":
    engine, _ = make_database()
    try:
        updated = initialize_database(engine)
        print("Database initialized." if updated else "Database already initialized; catalogue unchanged.")
    finally:
        engine.dispose()
