from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Request

from app.api.deps import DbDep
from app.core.config import get_settings
from app.infrastructure.database.indexes import ensure_indexes

logger = logging.getLogger("voltaris.ops")

router = APIRouter(tags=["ops"])


# HEAD as well as GET. Uptime monitors and Render's own probe use HEAD, and a
# GET-only route answers them with 405 — which reads as "the service is broken"
# on a dashboard that only shows the status code.
@router.api_route("/", methods=["GET", "HEAD"], include_in_schema=False)
async def root() -> dict[str, Any]:
    """A signpost, not a 404.

    Opening the API host in a browser is the first thing anyone does, and a bare
    404 reads as "it is broken" when the service is fine. This says what it is
    and where to go, and it is the cheapest possible way to answer "is the
    backend up?" without reading logs.
    """
    settings = get_settings()
    links: dict[str, str] = {"health": "/healthz", "readiness": "/readyz"}
    if not settings.is_production:
        # Docs are disabled in production, so do not advertise a dead link there.
        links["docs"] = "/docs"
        links["openapi"] = "/openapi.json"
    return {
        "service": settings.service_name,
        "status": "ok",
        "api": settings.api_v1_prefix,
        "environment": settings.environment,
        "links": links,
    }


@router.api_route("/healthz", methods=["GET", "HEAD"])
async def liveness() -> dict[str, str]:
    """Is the process up. Must not touch the database — a slow Mongo would otherwise
    cause the orchestrator to kill healthy pods."""
    return {"status": "ok", "service": get_settings().service_name}


@router.api_route("/readyz", methods=["GET", "HEAD"])
async def readiness(request: Request, db: DbDep) -> dict[str, Any]:
    """Can this instance serve traffic. Does touch the database, deliberately.

    Returns rather than raises, so a load balancer can drain the instance instead of
    watching it crash-loop. If startup could not reach the database, this is also
    where index creation is retried once it comes back.
    """
    try:
        await db.command("ping")
    except Exception:
        return {"status": "degraded", "database": "unreachable", "indexes": "unknown"}

    if not getattr(request.app.state, "indexes_ready", False):
        try:
            await ensure_indexes(db)
            request.app.state.indexes_ready = True
            logger.info("indexes converged after a delayed database connection")
        except Exception:
            logger.exception("database is reachable but index creation failed")
            return {"status": "degraded", "database": "ok", "indexes": "failed"}

    return {"status": "ready", "database": "ok", "indexes": "ok"}
