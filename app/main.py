"""FastAPI 应用入口。"""
from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from typing import Any, Dict

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import __version__, db
from . import admin as admin_api
from . import relay
from .formats.utils import error_payload

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")


async def _log_janitor() -> None:
    while True:
        try:
            days = int(db.get_setting("log_retention_days", "7") or 0)
            if days > 0:
                db.purge_logs(days)
        except Exception:
            pass
        await asyncio.sleep(3600)


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init_db()
    task = asyncio.create_task(_log_janitor())
    try:
        yield
    finally:
        task.cancel()


app = FastAPI(title="JAISafe LLM Gateway", version=__version__, docs_url=None,
              redoc_url=None, openapi_url=None, lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
)

app.include_router(admin_api.router)


@app.exception_handler(Exception)
async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
    fmt = "openai"
    path = request.url.path
    if path.startswith("/v1/messages"):
        fmt = "anthropic"
    elif path.startswith("/v1/responses"):
        fmt = "openai_responses"
    if path.startswith("/admin/"):
        return JSONResponse(status_code=500, content={"detail": f"服务端异常: {exc}"})
    return JSONResponse(status_code=500, content=error_payload(fmt, f"服务端异常: {exc}"))


# --------------------------------------------------------------------------- #
# 中转端点
# --------------------------------------------------------------------------- #
@app.post("/v1/chat/completions")
async def chat_completions(request: Request) -> Response:
    return await relay.relay(request, "openai")


@app.post("/v1/completions")
async def completions(request: Request) -> Response:
    return await relay.relay(request, "openai_completions")


@app.post("/v1/messages")
async def messages(request: Request) -> Response:
    return await relay.relay(request, "anthropic")


@app.post("/v1/responses")
async def responses(request: Request) -> Response:
    return await relay.relay(request, "openai_responses")


@app.post("/v1/embeddings")
async def embeddings(request: Request) -> Response:
    return await relay.relay_passthrough(request, "embeddings")


@app.post("/v1/messages/count_tokens")
async def count_tokens(request: Request) -> Response:
    try:
        body = await request.body()
        payload = json.loads(body) if body else {}
    except Exception:
        return JSONResponse(status_code=400,
                            content=error_payload("anthropic", "请求体不是合法 JSON",
                                                  "invalid_request_error"))
    return relay.count_tokens(request, payload if isinstance(payload, dict) else {})


@app.get("/v1/models")
async def models(request: Request) -> Response:
    ok, _, err = relay.check_auth(request)
    if not ok:
        return JSONResponse(status_code=401,
                            content=error_payload("openai", err, "authentication_error"))
    return relay.list_models(request)


@app.get("/v1/models/{model_id}")
async def model_detail(model_id: str, request: Request) -> Response:
    return JSONResponse(content={
        "id": model_id, "object": "model", "created": 0, "owned_by": "jaisafe-gateway",
    })


# --------------------------------------------------------------------------- #
# 健康检查 & WebUI
# --------------------------------------------------------------------------- #
@app.get("/health")
@app.get("/v1/health")
async def health() -> Response:
    return relay.health()


@app.get("/")
async def index() -> Response:
    index_path = os.path.join(STATIC_DIR, "index.html")
    if not os.path.exists(index_path):
        return JSONResponse(status_code=500, content={"detail": "WebUI 静态文件缺失"})
    return FileResponse(index_path, media_type="text/html")


@app.get("/favicon.ico")
async def favicon() -> Response:
    return Response(status_code=204)


if os.path.isdir(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
