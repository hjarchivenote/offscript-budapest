import os
from pathlib import Path

from sqlalchemy import URL, create_engine, event, make_url
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from sqlalchemy.pool import StaticPool


class Base(DeclarativeBase):
    pass


def configured_url():
    direct = os.environ.get("DATABASE_URL")
    secret_file = os.environ.get("DATABASE_PASSWORD_FILE")
    if direct and secret_file:
        raise RuntimeError("Configure DATABASE_URL or DATABASE_PASSWORD_FILE, not both.")
    if direct:
        return direct
    if secret_file:
        try:
            password = Path(secret_file).read_text(encoding="utf-8").rstrip("\r\n")
        except OSError:
            raise RuntimeError("The database password file could not be read.") from None
        if not password.strip():
            raise RuntimeError("The database password file is empty.")
        return URL.create(
            "postgresql+psycopg",
            username=os.environ.get("DATABASE_USER", "offscript"),
            password=password,
            host=os.environ.get("DATABASE_HOST", "db"),
            port=int(os.environ.get("DATABASE_PORT", "5432")),
            database=os.environ.get("DATABASE_NAME", "offscript"),
        )
    return None


def make_database(url: str | URL | None = None):
    url = url or configured_url()
    if os.environ.get("APP_ENV") == "production" and (
        url is None or make_url(url).get_backend_name() != "postgresql"
    ):
        raise RuntimeError("Production requires a shared PostgreSQL DATABASE_URL or password file.")
    if not url:
        Path(".local").mkdir(exist_ok=True)
        url = "sqlite:///.local/offscript.db"
    url = make_url(url)
    sqlite = url.get_backend_name() == "sqlite"
    options = {"pool_pre_ping": True}
    if not sqlite:
        options.update(connect_args={"connect_timeout": 5}, pool_timeout=5)
    if sqlite:
        options["connect_args"] = {"check_same_thread": False, "timeout": 15}
        if url.database in (None, "", ":memory:"):
            options["poolclass"] = StaticPool
    engine = create_engine(url, **options)
    if sqlite:

        @event.listens_for(engine, "connect")
        def configure_sqlite(connection, _):
            connection.execute("PRAGMA foreign_keys=ON")

    return engine, sessionmaker(bind=engine, expire_on_commit=False)
