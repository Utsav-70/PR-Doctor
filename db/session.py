"""Async engine and session factory.

One engine per process, created lazily. FastAPI gets sessions via dependency
injection; the Celery worker opens one per task inside its own event loop.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import lru_cache

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from settings import get_settings


@lru_cache(maxsize=1)
def get_engine() -> AsyncEngine:
    settings = get_settings()
    return create_async_engine(
        settings.DATABASE_URL,
        pool_pre_ping=True,  # a recycled connection killed by the DB fails on checkout,
        pool_size=10,  # not mid-transaction
        max_overflow=5,
        echo=False,
    )


@lru_cache(maxsize=1)
def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(
        bind=get_engine(),
        expire_on_commit=False,  # attributes stay readable after commit
        autoflush=False,
    )


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Transactional scope. Commits on success, rolls back on any exception.

    Used by the worker, where a task owns its transaction boundary.
    """
    async with get_sessionmaker()() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency.

    Does not commit — request handlers commit explicitly, so a handler that only
    reads never opens a write transaction.
    """
    async with get_sessionmaker()() as session:
        yield session
