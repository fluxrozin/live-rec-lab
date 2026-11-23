"""FastAPI アプリケーション。"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from live_rec_lab.services.jobs import Coordinator
from live_rec_lab.web.api import router as api_router
from live_rec_lab.web.pages import router as page_router
from live_rec_lab.web.security import LocalSecurityMiddleware

STATIC_DIR = Path(__file__).resolve().parent / "web" / "static"


def create_app(coordinator: Coordinator | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.coordinator.startup()
        task = None
        if os.environ.get("LIVE_REC_LAB_DISABLE_SCHEDULER") != "1":
            import asyncio

            task = asyncio.create_task(app.state.coordinator.loop())
        try:
            yield
        finally:
            app.state.coordinator.stop.set()
            if task is not None:
                task.cancel()
            await app.state.coordinator.shutdown()

    app = FastAPI(title="LiveRecorder", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.coordinator = coordinator or Coordinator()
    app.add_middleware(LocalSecurityMiddleware)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.include_router(page_router)
    app.include_router(api_router)
    return app
