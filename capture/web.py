from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
from fastapi.templating import Jinja2Templates

from .auth import LOGIN_HEADER, is_allowed
from .config import Settings, load_settings

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def create_app(settings: Settings) -> FastAPI:
    app = FastAPI()
    app.state.settings = settings

    @app.middleware("http")
    async def check_login(request: Request, call_next):
        if request.url.path != "/healthz" and not settings.dev_no_auth:
            if not is_allowed(request.headers.get(LOGIN_HEADER), settings):
                return PlainTextResponse("Not allowed", status_code=403)
        return await call_next(request)

    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    @app.get("/")
    async def home(request: Request):
        return TEMPLATES.TemplateResponse(request, "hello.html")

    return app


def make_app() -> FastAPI:
    return create_app(load_settings())
