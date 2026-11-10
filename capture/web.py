from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .auth import LOGIN_HEADER, is_allowed
from .capture import capture, share_to_text
from .config import Settings, load_settings
from .store import TYPE_HINTS, Store

HERE = Path(__file__).parent
TEMPLATES = Jinja2Templates(directory=str(HERE / "templates"))
EMPTY_MESSAGE = "Nothing to save — paste a link or some text."


def _item_line(item: dict) -> str:
    return item["title"] or item["url"] or (item["share_text"] or "")[:80]


def create_app(settings: Settings) -> FastAPI:
    # Opened eagerly (not in lifespan startup) so it also works when a TestClient
    # is used without a `with` block; closed on shutdown.
    store = Store(settings.db_path)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        try:
            yield
        finally:
            store.close()

    app = FastAPI(lifespan=lifespan)
    app.state.settings = settings
    app.state.store = store
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

    @app.middleware("http")
    async def check_login(request: Request, call_next):
        if request.url.path != "/healthz" and not settings.dev_no_auth:
            if not is_allowed(request.headers.get(LOGIN_HEADER), settings):
                return PlainTextResponse("Not allowed", status_code=403)
        return await call_next(request)

    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    @app.get("/manifest.webmanifest")
    async def manifest():
        return FileResponse(HERE / "static" / "manifest.webmanifest",
                            media_type="application/manifest+json")

    @app.get("/sw.js")
    async def service_worker():
        return FileResponse(HERE / "static" / "sw.js", media_type="text/javascript",
                            headers={"Cache-Control": "no-cache"})

    @app.get("/")
    async def home(request: Request, saved: str | None = None, error: str | None = None):
        items = [{**i, "line": _item_line(i)} for i in store.list_items(100)]
        return TEMPLATES.TemplateResponse(request, "home.html", {
            "items": items,
            "saved": saved == "1",
            "error": EMPTY_MESSAGE if error == "empty" else None,
            "type_hints": TYPE_HINTS,
        })

    @app.get("/saved")
    async def saved_page(request: Request):
        return TEMPLATES.TemplateResponse(request, "saved.html")

    @app.get("/api/items")
    async def api_items():
        return {"items": store.list_items(100)}

    @app.post("/api/capture")
    async def api_capture(request: Request):
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError("bad body")
            for key in ("text", "type_hint", "client_id"):
                if body.get(key) is not None and not isinstance(body[key], str):
                    raise ValueError(f"{key} must be a string")
            result = capture(store, body.get("text") or "",
                             now=datetime.now(timezone.utc),
                             type_hint=body.get("type_hint") or None,
                             client_id=body.get("client_id") or None)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=422)
        return JSONResponse({"item_id": result.item_id, "duplicate": result.duplicate},
                            status_code=200 if result.duplicate else 201)

    @app.post("/share")
    async def share(title: str = Form(""), text: str = Form(""), url: str = Form(""),
                    type_hint: str = Form(""), client_id: str = Form(""),
                    return_to: str = Form("")):
        combined = share_to_text(title, text, url)
        try:
            capture(store, combined, now=datetime.now(timezone.utc),
                    type_hint=type_hint if type_hint in TYPE_HINTS else None,
                    client_id=client_id or None)
        except ValueError:
            return RedirectResponse("/?error=empty", status_code=303)
        target = "/?saved=1" if return_to == "home" else "/saved"
        return RedirectResponse(target, status_code=303)

    return app


def make_app() -> FastAPI:
    return create_app(load_settings())
