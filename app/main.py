"""Application assembly: middleware, exception handling, routing, lifecycle."""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.v1.routes import (
    auth,
    catalog,
    console,
    health,
    leads,
    me,
    media,
    orders,
    payments,
    vehicles,
)
from app.core.config import get_settings
from app.core.errors import AppError, ErrorCode, error_body
from app.core.logging import configure_logging, request_id_var, user_id_var
from app.infrastructure.database.client import connect, disconnect, get_db
from app.infrastructure.database.indexes import ensure_indexes

logger = logging.getLogger("voltaris.api")


def _init_error_tracking(settings: Any) -> None:
    """Wire Sentry when a DSN is configured.

    Without this an exception in production is visible only to whoever happens to
    read container logs. `send_default_pii=False` is the important argument: the
    default would attach request bodies and headers to every event, which for
    this app means shipping passwords and session cookies to a third party.
    """
    if not settings.sentry_dsn:
        return
    try:
        import sentry_sdk
        from sentry_sdk.integrations.fastapi import FastApiIntegration
    except ImportError:
        logger.warning("SENTRY_DSN is set but sentry-sdk is not installed")
        return

    sentry_sdk.init(
        dsn=settings.sentry_dsn,
        environment=settings.environment,
        send_default_pii=False,
        # 10% of transactions. Enough to see latency trends without paying to
        # trace every health check.
        traces_sample_rate=0.1,
        integrations=[FastApiIntegration()],
    )
    logger.info("error tracking enabled")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings.log_level)
    _init_error_tracking(settings)
    await connect()

    # A database that is not up yet must not kill the process.
    #
    # Crashing here means the container never serves /readyz, so the orchestrator has
    # nothing to read except a crash-loop, and during local development a reload while
    # Mongo is restarting takes the API down permanently. Instead: log it, keep
    # serving, report not-ready, and let /readyz converge the indexes once the
    # database appears. Traffic is still withheld, which is the actual goal.
    try:
        created = await ensure_indexes(get_db())
        app.state.indexes_ready = True
        logger.info(
            "startup complete",
            extra={"environment": settings.environment, "indexes": created},
        )
    except Exception:
        app.state.indexes_ready = False
        logger.exception(
            "startup completed without the database; serving as not-ready",
            extra={"environment": settings.environment},
        )
    yield
    await disconnect()
    logger.info("shutdown complete")


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="Voltaris Mobility API",
        version="1.0.0",
        lifespan=lifespan,
        # Interactive docs are useful in development and are attack surface in
        # production, where the spec is published to consumers out of band instead.
        docs_url=None if settings.is_production else "/docs",
        redoc_url=None if settings.is_production else "/redoc",
        openapi_url=None if settings.is_production else "/openapi.json",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "X-CSRF-Token"],
        max_age=600,
    )

    @app.middleware("http")
    async def observability(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        # Honour an upstream id so a trace survives across services, but generate one
        # if absent — no request is ever uncorrelated.
        incoming = request.headers.get("X-Request-ID")
        request_id = (
            incoming if incoming and len(incoming) <= 64 else f"req_{uuid.uuid4().hex[:12]}"
        )
        request_id_var.set(request_id)
        user_id_var.set(None)

        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            logger.exception(
                "unhandled exception",
                extra={
                    "endpoint": request.url.path,
                    "method": request.method,
                    "duration_ms": duration_ms,
                    "status": 500,
                },
            )
            return JSONResponse(
                status_code=500,
                content=error_body(ErrorCode.INTERNAL_ERROR, request_id),
                headers={"X-Request-ID": request_id},
            )

        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        response.headers["X-Request-ID"] = request_id
        logger.info(
            "request",
            extra={
                "endpoint": request.url.path,
                "method": request.method,
                "status": response.status_code,
                "duration_ms": duration_ms,
            },
        )
        return response

    @app.middleware("http")
    async def security_headers(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        # Reject oversized bodies before they are parsed. A 200 MB JSON document
        # should never reach the deserialiser.
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > settings.max_request_bytes:
            return JSONResponse(
                status_code=413,
                content=error_body(ErrorCode.INVALID_REQUEST, request_id_var.get()),
            )
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("Cache-Control", "no-store")
        if settings.is_production:
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=63072000; includeSubDomains; preload"
            )
        return response

    @app.exception_handler(AppError)
    async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        request_id = request_id_var.get()
        # `detail` is logged. Only the mapped public message is serialised.
        logger.warning(
            "business error",
            extra={
                "error_code": exc.code.value,
                "detail": exc.detail,
                "endpoint": request.url.path,
                "status": exc.status_code,
                **exc.context,
            },
        )
        return JSONResponse(
            status_code=exc.status_code,
            content=error_body(exc.code, request_id),
            headers={"X-Request-ID": request_id},
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        request_id = request_id_var.get()
        logger.info(
            "validation error",
            extra={"endpoint": request.url.path, "errors": str(exc.errors())[:2000]},
        )
        # Pydantic's error list can echo submitted values, which may include a
        # password. The client gets the code and nothing else.
        return JSONResponse(
            status_code=422,
            content=error_body(ErrorCode.INVALID_REQUEST, request_id),
            headers={"X-Request-ID": request_id},
        )

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        request_id = request_id_var.get()
        logger.exception("unhandled error", extra={"endpoint": request.url.path})
        if settings.sentry_dsn:
            try:
                import sentry_sdk

                # Tag with the request id so a Sentry event and a log line can be
                # matched without guesswork.
                sentry_sdk.set_tag("request_id", request_id)
                sentry_sdk.capture_exception(exc)
            except Exception:
                # Reporting must never mask the error it is reporting. Logged at
                # debug so a broken Sentry does not itself fill the log.
                logger.debug("could not report to sentry", exc_info=True)
        return JSONResponse(
            status_code=500,
            content=error_body(ErrorCode.INTERNAL_ERROR, request_id),
            headers={"X-Request-ID": request_id},
        )

    prefix = settings.api_v1_prefix
    app.include_router(health.router)
    app.include_router(auth.router, prefix=prefix)
    app.include_router(orders.router, prefix=prefix)
    app.include_router(payments.router, prefix=prefix)
    app.include_router(console.router, prefix=prefix)
    app.include_router(vehicles.router, prefix=prefix)
    app.include_router(catalog.router, prefix=prefix)
    app.include_router(leads.router, prefix=prefix)
    app.include_router(me.router, prefix=prefix)
    app.include_router(media.router, prefix=prefix)
    return app


app = create_app()


def openapi_snapshot() -> dict[str, Any]:
    return app.openapi()
