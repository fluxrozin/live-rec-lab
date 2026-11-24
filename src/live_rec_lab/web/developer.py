"""Local OpenAPI viewers for the isolated developer workspace."""

from html import escape
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

DOC_PATHS = {"/docs", "/redoc"}
_ASSETS = "/_dev/docs-assets"


def mount_developer_docs(app: FastAPI) -> None:
    directory = Path(__file__).parent / "developer-assets"
    app.mount(_ASSETS, StaticFiles(directory=directory), name="developer_docs_assets")

    def page(title: str, viewer: str, scripts: str, stylesheet: str = "") -> HTMLResponse:
        return HTMLResponse(f"""<!doctype html>
<html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="csrf-token" content="{escape(app.state.csrf_token, quote=True)}">
<title>LiveRecorder {title}</title>
{stylesheet}</head>
<body><main id="{viewer}"></main>{scripts}</body></html>""")

    @app.get("/docs", include_in_schema=False)
    def swagger():
        return page(
            "Swagger UI",
            "swagger-ui",
            f'<script src="{_ASSETS}/swagger-ui-bundle.js"></script><script src="{_ASSETS}/viewers.js"></script>',
            f'<link rel="stylesheet" href="{_ASSETS}/swagger-ui.css">',
        )

    @app.get("/redoc", include_in_schema=False)
    def redoc():
        return page(
            "ReDoc",
            "redoc",
            f'<script src="{_ASSETS}/redoc.standalone.js"></script><script src="{_ASSETS}/viewers.js"></script>',
        )
