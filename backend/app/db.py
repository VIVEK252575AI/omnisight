"""PostgreSQL session lifecycle for durable domain records."""

from __future__ import annotations

from collections.abc import Generator
from functools import lru_cache

from fastapi import Depends
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings, get_settings
from app.errors import DomainError, ErrorCode


def database_url(settings: Settings) -> str:
    """Resolve a configured URL without logging or exposing it."""
    if settings.database_url is None or not settings.database_url.get_secret_value().strip():
        raise DomainError(
            ErrorCode.GITHUB_PUBLISH_UNAVAILABLE,
            "Durable GitHub publishing is not configured",
            status_code=503,
        )
    value = settings.database_url.get_secret_value().strip()
    if value.startswith("postgresql://"):
        value = "postgresql+psycopg://" + value.removeprefix("postgresql://")
    if not value.startswith("postgresql+psycopg://"):
        raise DomainError(
            ErrorCode.GITHUB_PUBLISH_UNAVAILABLE,
            "The durable publishing database must use PostgreSQL",
            status_code=503,
        )
    return value


def build_engine(settings: Settings) -> Engine:
    url = database_url(settings)
    return create_engine(url, pool_pre_ping=True)


@lru_cache(maxsize=4)
def _engine_for_url(url: str) -> Engine:
    return create_engine(url, pool_pre_ping=True)


def get_session(
    settings: Settings = Depends(get_settings),
) -> Generator[Session, None, None]:
    engine = _engine_for_url(database_url(settings))
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        yield session


