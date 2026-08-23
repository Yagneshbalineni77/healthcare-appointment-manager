"""Database engine, session factory and dialect-aware helpers.

The application is written against SQLAlchemy 2.0 and runs on **SQLite** (zero
config, used for local dev/tests) and **PostgreSQL** (production). The few
places where the two dialects differ meaningfully — row-level locking and
`ON CONFLICT` semantics — are isolated behind helpers in this module so the
business code stays dialect-neutral.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings


class Base(DeclarativeBase):
    """Declarative base for every ORM model."""


def _build_engine() -> Engine:
    url = settings.database_url

    if url.startswith("sqlite"):
        connect_args = {"check_same_thread": False, "timeout": 30}
        kwargs: dict = {"connect_args": connect_args}
        if ":memory:" in url:
            # Tests share one in-memory database across sessions/threads.
            kwargs["poolclass"] = StaticPool
        return create_engine(url, future=True, **kwargs)

    # Postgres (Render/Railway hand out `postgres://`, SQLAlchemy wants `postgresql+psycopg://`)
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+psycopg://", 1)
    elif url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)

    return create_engine(
        url,
        future=True,
        pool_pre_ping=True,
        pool_size=10,
        max_overflow=20,
        pool_recycle=1800,
    )


engine = _build_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


@event.listens_for(Engine, "connect")
def _sqlite_pragmas(dbapi_connection, connection_record) -> None:
    """Make SQLite behave under concurrency.

    * WAL lets readers run while a writer holds the write lock.
    * `busy_timeout` makes competing writers wait instead of instantly raising
      "database is locked", which is what we want for simultaneous bookings.
    * Foreign keys are OFF by default in SQLite — turn them on.
    """
    if engine.dialect.name != "sqlite":
        return
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA synchronous=NORMAL")
    finally:
        cursor.close()


def get_db() -> Iterator[Session]:
    """FastAPI dependency: one session per request, always closed."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope for background workers and scripts."""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def supports_row_locking() -> bool:
    """`SELECT ... FOR UPDATE` exists on Postgres, not on SQLite."""
    return engine.dialect.name != "sqlite"


def init_db() -> None:
    from app import models  # noqa: F401  (import registers the mappers)

    Base.metadata.create_all(bind=engine)
