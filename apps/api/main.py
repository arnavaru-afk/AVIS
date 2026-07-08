"""FastAPI application factory for AVIS reporting APIs."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from apps.api.config import Settings, get_settings
from apps.api.middleware import AuthMiddleware, ComplianceMiddleware, RequestLoggingMiddleware
from apps.api.middleware.compliance import DEFAULT_SOURCE_POLICIES
from apps.api.routers import instruments, market, pipeline, quality, valuations


def create_app(*, settings: Settings | None = None) -> FastAPI:
    resolved_settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        logging.basicConfig(level=getattr(logging, resolved_settings.log_level, logging.INFO))
        engine_kwargs: dict[str, object] = {"future": True}
        if not resolved_settings.async_database_url.startswith("sqlite+"):
            engine_kwargs.update({"pool_size": 2, "max_overflow": 8})
        engine = create_async_engine(resolved_settings.async_database_url, **engine_kwargs)
        session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
        app.state.settings = resolved_settings
        app.state.engine = engine
        app.state.session_factory = session_factory
        app.state.source_policies = DEFAULT_SOURCE_POLICIES
        yield
        await engine.dispose()

    app = FastAPI(title="AVIS API", version="1.0", lifespan=lifespan)
    app.add_middleware(RequestLoggingMiddleware)
    app.add_middleware(ComplianceMiddleware)
    app.add_middleware(AuthMiddleware, settings=resolved_settings)

    api_router = APIRouter(prefix="/api/v1")
    api_router.include_router(instruments.router)
    api_router.include_router(valuations.router)
    api_router.include_router(market.router)
    api_router.include_router(quality.router)
    api_router.include_router(pipeline.router)
    app.include_router(api_router)
    return app


app = create_app()
