"""FastAPI初期化・ルーター登録・lifespan。"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from live_rec_lab.context import AppContext
from live_rec_lab.web.security import SecurityMiddleware


def create_app(ctx: AppContext) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        from live_rec_lab.platforms.registry import build_adapters
        from live_rec_lab.supervisor import Supervisor

        adapters = ctx.services.get("adapters") or build_adapters(ctx)
        sup = Supervisor(ctx, adapters)
        ctx.services["supervisor"] = sup
        await sup.start()
        try:
            yield
        finally:
            await sup.stop()
            for a in adapters.values():
                close = getattr(a, "aclose", None)
                if close:
                    await close()

    app = FastAPI(title="LiveRecorder", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.ctx = ctx
    app.add_middleware(SecurityMiddleware, port=ctx.port, csrf_token=ctx.csrf_token)

    from pathlib import Path

    from fastapi.exceptions import RequestValidationError
    from fastapi.responses import JSONResponse
    from fastapi.staticfiles import StaticFiles

    from live_rec_lab.web import api, pages

    @app.exception_handler(api.ApiError)
    async def _api_error(_request, exc: api.ApiError):
        return api.error_response(exc)

    @app.exception_handler(RequestValidationError)
    async def _validation(_request, exc: RequestValidationError):
        return JSONResponse({"error": {"code": "VALIDATION_ERROR", "message": "入力が不正です"}}, status_code=422)

    @app.exception_handler(404)
    async def _not_found(request, exc):
        if request.url.path.startswith("/api/"):
            return JSONResponse({"error": {"code": "NOT_FOUND", "message": "見つかりません"}}, status_code=404)
        return JSONResponse({"error": {"code": "NOT_FOUND", "message": "ページが見つかりません"}}, status_code=404)

    app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "web" / "static")), name="static")
    app.include_router(api.router)
    app.include_router(pages.router)
    return app
