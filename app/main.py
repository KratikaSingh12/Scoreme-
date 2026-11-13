"""FastAPI application factory.

Run with:
    uvicorn app.main:create_app --factory --reload
"""

import logging
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api.routes import router
from app.container import Container
from app.core.errors import PlatformError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Pick up anything left stuck by a crash or outage before we stopped.
    recovered = app.state.container.service.recover_stuck()
    if recovered:
        log.info("recovered %d stuck request(s) on startup: %s", len(recovered), recovered)
    yield


def create_app(container: Optional[Container] = None) -> FastAPI:
    app = FastAPI(
        lifespan=lifespan,
        title="Resilient Decision Platform",
        version="1.0.0",
        description="Configurable workflow decision engine with audit trail, retries and idempotency.",
    )
    app.state.container = container or Container.build()
    app.include_router(router)

    if app.state.container.settings.admin_api_key is None:
        log.warning(
            "DECISION_ADMIN_API_KEY is not set: admin endpoints (reload, review, recover) are OPEN. "
            "Fine for local development, set the key anywhere else."
        )

    @app.exception_handler(PlatformError)
    def handle_platform_error(_: Request, exc: PlatformError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": exc.code, "message": exc.message, "details": exc.details},
        )

    return app
