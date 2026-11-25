from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from sqlalchemy import event, inspect, select
from sqlalchemy.orm import Session

from app import bootstrap
from app.database import make_database
from app.models import Activity, DatabaseState


def test_concurrent_initializers_share_one_transactional_catalogue(tmp_path):
    url = f"sqlite:///{tmp_path / 'shared.db'}"
    barrier = Barrier(2)

    def initialize(_):
        engine, _ = make_database(url)
        try:
            barrier.wait(timeout=5)
            return bootstrap.initialize_database(engine)
        finally:
            engine.dispose()

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(initialize, range(2))) == [False, True]
    engine, _ = make_database(url)
    try:
        bootstrap.require_initialized_database(engine)
        with Session(engine) as db:
            assert len(list(db.scalars(select(Activity)))) == 172
    finally:
        engine.dispose()


def test_failed_initialization_rolls_back_schema_catalogue_and_marker(tmp_path, monkeypatch):
    engine, _ = make_database(f"sqlite:///{tmp_path / 'failed.db'}")
    original = bootstrap.seed_catalogue

    def fail_after_seed(db):
        original(db)
        raise RuntimeError("Interrupted bootstrap")

    try:
        with monkeypatch.context() as patch:
            patch.setattr(bootstrap, "seed_catalogue", fail_after_seed)
            with pytest.raises(RuntimeError, match="Interrupted"):
                bootstrap.initialize_database(engine)
        assert inspect(engine).get_table_names() == []
        assert bootstrap.initialize_database(engine)
        bootstrap.require_initialized_database(engine)
    finally:
        engine.dispose()


def test_repeated_bootstrap_and_replica_checks_do_not_rewrite_catalogue(tmp_path):
    engine, _ = make_database(f"sqlite:///{tmp_path / 'repeat.db'}")
    try:
        bootstrap.initialize_database(engine)
        with Session(engine) as db:
            db.get(Activity, "mecca-day").title = "Retain the stored title"
            db.commit()
        writes = []

        def record(conn, cursor, statement, parameters, context, executemany):
            if statement.lstrip().split()[0].upper() in {"CREATE", "INSERT", "UPDATE", "DELETE", "ALTER"}:
                writes.append(statement)

        event.listen(engine, "before_cursor_execute", record)
        assert not bootstrap.initialize_database(engine)
        bootstrap.require_initialized_database(engine)
        event.remove(engine, "before_cursor_execute", record)
        assert writes == []
        with Session(engine) as db:
            assert db.get(Activity, "mecca-day").title == "Retain the stored title"
    finally:
        engine.dispose()


def test_uninitialized_or_incompatible_storage_is_not_accepted(tmp_path):
    engine, _ = make_database(f"sqlite:///{tmp_path / 'state.db'}")
    try:
        with pytest.raises(RuntimeError, match="uninitialized"):
            bootstrap.require_initialized_database(engine)
        assert inspect(engine).get_table_names() == []
        bootstrap.initialize_database(engine)
        with Session(engine) as db:
            db.get(DatabaseState, bootstrap.STATE_ID).schema_version = 999
            db.commit()
        with pytest.raises(RuntimeError, match="migration"):
            bootstrap.initialize_database(engine)
        with pytest.raises(RuntimeError, match="migration"):
            bootstrap.require_initialized_database(engine)
    finally:
        engine.dispose()
