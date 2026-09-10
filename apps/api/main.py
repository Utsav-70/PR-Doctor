"""FastAPI application.

Deliberately thin. The API accepts webhooks and hands off; it never calls the LLM and
never does slow work. Its p99 is a GitHub-visible number — GitHub times webhook
deliveries out at 10 seconds and disables endpoints that fail persistently.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import redis.asyncio as aioredis
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import text

from apps.api.routes import webhooks
from db.session import get_engine, get_sessionmaker
from settings import get_settings

logger = logging.getLogger(__name__)

CHECK_TIMEOUT_SECONDS = 1.0


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    app.state.redis = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
    # Touch the sessionmaker so a bad DATABASE_URL fails at boot, not on first request.
    get_sessionmaker()
    logger.info("prguard api starting", extra={"env": settings.PRGUARD_ENV})
    try:
        yield
    finally:
        await app.state.redis.aclose()
        await get_engine().dispose()
        logger.info("prguard api stopped")


app = FastAPI(title="PRGuard", version="0.1.0", lifespan=lifespan)
app.include_router(webhooks.router)


async def _check_database() -> bool:
    try:
        async with asyncio.timeout(CHECK_TIMEOUT_SECONDS):
            async with get_sessionmaker()() as session:
                await session.execute(text("SELECT 1"))
        return True
    except Exception:
        logger.warning("database health check failed", exc_info=True)
        return False


async def _check_redis() -> bool:
    try:
        async with asyncio.timeout(CHECK_TIMEOUT_SECONDS):
            return bool(await app.state.redis.ping())
    except Exception:
        logger.warning("redis health check failed", exc_info=True)
        return False


@app.get("/health/live")
async def live() -> dict[str, str]:
    """Liveness: the process is up. No dependency checks.

    Separate from readiness on purpose — a process that is alive but cannot reach
    Postgres should stop receiving traffic without being restarted.
    """
    return {"status": "ok"}


@app.get("/health")
async def health() -> JSONResponse:
    """Readiness: the process can serve requests.

    Returns 503 when a dependency is down, so a load balancer stops sending traffic
    to an instance that would only fail.
    """
    database_ok, redis_ok = await asyncio.gather(_check_database(), _check_redis())
    checks = {
        "database": "ok" if database_ok else "error",
        "redis": "ok" if redis_ok else "error",
    }
    healthy = database_ok and redis_ok
    body: dict[str, Any] = {
        "status": "ok" if healthy else "degraded",
        "version": app.version,
        "checks": checks,
    }
    return JSONResponse(body, status_code=200 if healthy else 503)
