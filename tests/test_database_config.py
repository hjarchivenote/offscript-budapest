import os
from pathlib import Path

import pytest
from sqlalchemy import text

from app.database import configured_url, make_database
from scripts.prepare_local import prepare


@pytest.fixture(autouse=True)
def clean_database_settings(monkeypatch):
    for name in (
        "DATABASE_URL", "DATABASE_PASSWORD_FILE", "DATABASE_HOST", "DATABASE_PORT",
        "DATABASE_USER", "DATABASE_NAME", "APP_ENV",
    ):
        monkeypatch.delenv(name, raising=False)


def test_secret_password_keeps_special_characters_without_url_parsing(tmp_path, monkeypatch):
    password = " p@ss:/?#%'with spaces "
    secret = tmp_path / "password"
    secret.write_text(password + "\n")
    monkeypatch.setenv("DATABASE_PASSWORD_FILE", str(secret))
    monkeypatch.setenv("DATABASE_HOST", "database")
    monkeypatch.setenv("DATABASE_PORT", "5433")
    monkeypatch.setenv("APP_ENV", "production")
    engine, _ = make_database()
    try:
        assert engine.url.password == password
        assert engine.url.host == "database" and engine.url.port == 5433
        assert engine.url.username == engine.url.database == "offscript"
        assert engine.url.drivername == "postgresql+psycopg"
        assert password not in str(engine.url)
    finally:
        engine.dispose()


@pytest.mark.parametrize("contents", ["", "\n", "  \r\n"])
def test_empty_secret_does_not_fall_back_to_local_database(tmp_path, monkeypatch, contents):
    monkeypatch.chdir(tmp_path)
    secret = tmp_path / "password"
    secret.write_text(contents)
    monkeypatch.setenv("DATABASE_PASSWORD_FILE", str(secret))
    with pytest.raises(RuntimeError, match="password file is empty"):
        make_database()
    assert not Path(".local").exists()


def test_missing_secret_and_ambiguous_settings_fail_without_exposing_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PASSWORD_FILE", str(tmp_path / "missing"))
    with pytest.raises(RuntimeError, match="could not be read"):
        configured_url()
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:secret@db/app")
    with pytest.raises(RuntimeError, match="not both") as error:
        configured_url()
    assert "user:secret" not in str(error.value)


@pytest.mark.parametrize("url", [None, "sqlite://", "mysql://user:password@db/app"])
def test_production_requires_shared_postgres_before_creating_local_files(tmp_path, monkeypatch, url):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("APP_ENV", "production")
    with pytest.raises(RuntimeError, match="shared PostgreSQL"):
        make_database(url)
    assert not Path(".local").exists()


def test_explicit_in_memory_database_retains_shared_pool_and_foreign_keys():
    engine, _ = make_database("sqlite://")
    try:
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE sample(id INTEGER PRIMARY KEY)"))
            connection.execute(text("INSERT INTO sample VALUES (7)"))
        with engine.connect() as connection:
            assert connection.execute(text("SELECT id FROM sample")).scalar_one() == 7
            assert connection.execute(text("PRAGMA foreign_keys")).scalar_one() == 1
    finally:
        engine.dispose()


def test_prepare_local_preserves_existing_passwords_on_every_run(tmp_path):
    directory = tmp_path / "private"
    prepare(directory)
    before = {p.name: p.read_bytes() for p in directory.iterdir()}
    prepare(directory)
    assert {p.name: p.read_bytes() for p in directory.iterdir()} == before
    assert set(before) == {"db-password", "admin-password"}
    assert before["db-password"] != before["admin-password"]
    assert all(len(value.strip()) >= 48 for value in before.values())
    assert os.stat(directory).st_mode & 0o077 == 0


def test_prepare_local_does_not_replace_an_invalid_existing_secret(tmp_path):
    directory = tmp_path / "private"
    directory.mkdir()
    (directory / "db-password").write_text("")
    with pytest.raises(RuntimeError, match="not a nonempty file"):
        prepare(directory)
    assert (directory / "db-password").read_bytes() == b""
