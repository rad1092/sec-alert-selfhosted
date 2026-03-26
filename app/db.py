from __future__ import annotations

import threading
from collections.abc import Generator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.models import Base

_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None
_lock = threading.Lock()


def _configure_sqlite_pragmas(engine: Engine) -> None:
    @event.listens_for(engine, "connect")
    def on_connect(dbapi_connection, _connection_record) -> None:  # type: ignore[no-untyped-def]
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL;")
        cursor.execute("PRAGMA busy_timeout=5000;")
        cursor.execute("PRAGMA foreign_keys=ON;")
        cursor.close()


def configure_database(database_url: str) -> None:
    global _engine, _session_factory

    with _lock:
        if _engine is not None:
            _engine.dispose()

        connect_args: dict[str, object] = {}
        if database_url.startswith("sqlite:///"):
            connect_args["check_same_thread"] = False

        engine = create_engine(
            database_url,
            future=True,
            pool_pre_ping=True,
            connect_args=connect_args,
        )
        if database_url.startswith("sqlite:///"):
            _configure_sqlite_pragmas(engine)

        _engine = engine
        _session_factory = sessionmaker(
            bind=_engine,
            autoflush=False,
            autocommit=False,
            expire_on_commit=False,
        )


def get_engine() -> Engine:
    if _engine is None:
        raise RuntimeError("Database is not configured.")
    return _engine


def init_database() -> None:
    Base.metadata.create_all(bind=get_engine())


def dispose_database() -> None:
    global _engine, _session_factory
    with _lock:
        if _engine is not None:
            _engine.dispose()
        _engine = None
        _session_factory = None


def get_session() -> Generator[Session, None, None]:
    if _session_factory is None:
        raise RuntimeError("Database session factory is not configured.")
    session = _session_factory()
    try:
        yield session
    finally:
        session.close()


def open_session() -> Session:
    if _session_factory is None:
        raise RuntimeError("Database session factory is not configured.")
    return _session_factory()
