"""Database plumbing: engine, sessions and portable column types.

SQLite is supported for local use and tests; PostgreSQL is the production target. Column
types are chosen so the schema is identical on both.
"""

from __future__ import annotations

import json
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator

from sqlalchemy import JSON, Numeric, String, create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.types import TypeDecorator

from .config import get_settings


class Base(DeclarativeBase):
    pass


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:20]}"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def to_jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [to_jsonable(v) for v in value]
    if hasattr(value, "value") and hasattr(value, "name") and value.__class__.__module__ != "builtins":
        return value.value  # Enum
    return value


def json_dumps(value: Any, **kwargs: Any) -> str:
    return json.dumps(to_jsonable(value), **kwargs)


class Money(TypeDecorator):
    """Exact decimal. NUMERIC on PostgreSQL; text on SQLite (which has no exact decimal type)."""

    impl = String(64)
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            return dialect.type_descriptor(Numeric(28, 10))
        return dialect.type_descriptor(String(64))

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        value = Decimal(str(value)) if not isinstance(value, Decimal) else value
        return value if dialect.name == "postgresql" else format(value, "f")

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        value = Decimal(str(value))
        if dialect.name == "postgresql":
            # NUMERIC(28,10) pads to ten places; trim without switching to exponent notation.
            value = value.quantize(Decimal(1)) if value == value.to_integral_value() else value.normalize()
        return value


class JSONDoc(TypeDecorator):
    """JSON column that accepts Decimals and dates."""

    impl = JSON
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return to_jsonable(value) if value is not None else None


_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def configure(database_url: str | None = None) -> Engine:
    """(Re)initialise the engine. Called once at start-up, or by tests with a temp database."""
    global _engine, _session_factory
    settings = get_settings()
    url = database_url or settings.database_url
    if url.startswith("sqlite") and settings.is_production and not settings.allow_sqlite_in_production:
        raise RuntimeError("SQLite is not permitted in production; set SECURIFI_DATABASE_URL to PostgreSQL")
    connect_args: dict[str, Any] = {}
    if url.startswith("sqlite"):
        connect_args["check_same_thread"] = False
        path = url.split("///", 1)[-1]
        if path and path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    _engine = create_engine(url, echo=settings.sql_echo, connect_args=connect_args, pool_pre_ping=True,
                            json_serializer=json_dumps)
    if url.startswith("sqlite"):
        @event.listens_for(_engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _):  # pragma: no cover - trivial
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA journal_mode=WAL")
            cur.close()
    _session_factory = sessionmaker(bind=_engine, expire_on_commit=False)
    return _engine


def get_engine() -> Engine:
    if _engine is None:
        configure()
    assert _engine is not None
    return _engine


def create_schema() -> None:
    from . import models  # noqa: F401  (register tables)

    Base.metadata.create_all(get_engine())


@contextmanager
def session_scope() -> Iterator[Session]:
    if _session_factory is None:
        configure()
    assert _session_factory is not None
    session = _session_factory()
    try:
        yield session
        session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()


def new_session() -> Session:
    if _session_factory is None:
        configure()
    assert _session_factory is not None
    return _session_factory()
