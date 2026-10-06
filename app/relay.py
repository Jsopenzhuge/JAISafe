"""中转核心：鉴权、渠道路由、格式转换、流式转发、日志采集。"""
from __future__ import annotations

import copy
import json
import time
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from . import db
from .formats import FORMATS, UPSTREAM_FORMATS, get_format
from .formats.utils import (StreamError, assemble_chunks, error_payload, jd,
                            normalize_usage, reasoning_context, safe_json,
                            status_to_err_type)
from .masking import (MODE_DRY, MODE_ENFORCE, MODE_OFF, CanonicalChunkUnmasker,
                      MaskEngine, effective_mode, global_mode, load_policy, mask_body,
                      resolve_session_id, summarize_hits, unmask_body)
from .masking.config import store_enabled
from .masking.slots import get_store


class UpstreamError(Exception):
    def __init__(self, status: int, body: Any, url: str, message: str = "") -> None:
        super().__init__(message or f"上游返回 {status}")
        self.status = status
        self.body = body
        self.url = url
        self.message = message


class UpstreamTransportError(Exception):
    def __init__(self, message: str, url: str) -> None:
        super().__init__(message)
        self.message = message
        self.url = url


SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


# --------------------------------------------------------------------------- #
# 鉴权
# --------------------------------------------------------------------------- #
def extract_relay_key(request: Request) -> str:
    auth = request.headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    for header in ("x-api-key", "api-key", "x-goog-api-key"):
        val = request.headers.get(header)
        if val:
            return val.strip()
    return ""


def check_auth(request: Request) -> Tuple[bool, str, str]:
    """返回 (是否通过, 密钥名称, 错误信息)。"""
    key = extract_relay_key(request)
    row = db.get_key_by_value(key) if key else None
    required = db.get_setting("require_api_key", "0") == "1" or db.count_enabled_keys() > 0
    if not required:
        return True, (row["name"] if row else "未启用鉴权"), ""
    if not key:
        return False, "", "缺少 API Key（请在请求头携带 Authorization: Bearer <key> 或 x-api-key）"
    if not row:
        return False, "", "API Key 无效"
    if not row["enabled"]:
        return False, "", "API Key 已被禁用"
    return True, row["name"], ""


# --------------------------------------------------------------------------- #
# 渠道路由
# --------------------------------------------------------------------------- #
def channel_supports(channel: Dict[str, Any], model: str) -> bool:
    if not model:
        return True
    if model in channel.get("model_list") or []:
        return True
    mm = channel.get("model_map") or {}
    if model in mm or "*" in mm:
        return True
    for m in channel.get("model_list") or []:
        if m.endswith("*") and model.startswith(m[:-1]):
            return True
    return False


def map_model(channel: Dict[str, Any], model: str) -> str:
    mm = channel.get("model_map") or {}
    if model in mm:
        return mm[model]
    if "*" in mm:
        return mm["*"]
    return model


def pick_channels(model: str) -> List[Dict[str, Any]]:
    channels = db.list_channels(enabled_only=True)
    if not channels:
        return []

    def score(c: Dict[str, Any]) -> int:
        if channel_supports(c, model):
            return 0
        if not c.get("model_list") and not c.get("model_map"):
            return 1
        return 2

    channels.sort(key=lambda c: (score(c), -int(c.get("priority") or 0), int(c["id"])))
    primary = [c for c in channels if score(c) <= 1]
    return primary or channels


def build_url(base_url: str, endpoint: str) -> str:
    base = (base_url or "").rstrip("/")
    if not base:
        return ""
    low = base.lower()
    if low.endswith("/" + endpoint.lower()):
        return base
    if not any(low.endswith(s) for s in ("/v1", "/v2", "/v3", "/v4", "/v1beta")):
        base = base + "/v1"
    return f"{base}/{endpoint}"


def build_upstream_headers(channel: Dict[str, Any], stream: bool) -> Dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "Accept": "text/event-stream" if stream else "application/json",
        "User-Agent": "JAISafe-LLM-Gateway/1.0",
    }
    key = channel.get("api_key") or ""
    if channel.get("type") == "anthropic":
        if key:
            headers["x-api-key"] = key
        headers["anthropic-version"] = "2023-06-01"
    elif key:
        headers["Authorization"] = f"Bearer {key}"
    for k, v in (channel.get("extra_headers") or {}).items():
        headers[str(k)] = str(v)
    return headers


# --------------------------------------------------------------------------- #
# 日志辅助
# --------------------------------------------------------------------------- #
def _clip(value: Any, limit: int) -> Any:
    if value is None:
        return None
    if not isinstance(value, str):
        value = jd(value)
    if limit > 0 and len(value) > limit:
        return value[:limit] + f"\n...[已截断，共 {len(value)} 字符]"
    return value


def _mask_headers(headers: Any) -> Dict[str, str]:
    out: Dict[str, str] = {}
    try:
        items = headers.items()
    except Exception:
        return out
    for k, v in items:
        if k.lower() in ("authorization", "x-api-key", "api-key", "cookie", "proxy-authorization"):
            v = "***"
        out[k] = v
    return out


def _log_limits() -> Tuple[int, bool, bool]:
    limit = int(db.get_setting("max_body_log", "262144") or 262144)
    log_req = db.get_setting("log_request_body", "1") == "1"
    log_resp = db.get_setting("log_response_body", "1") == "1"
    return limit, log_req, log_resp


def write_log(entry: Dict[str, Any]) -> None:
    try:
        db.insert_log(entry)
    except Exception:  # 日志失败不能影响主流程
        pass


def base_log(request: Request, client_format: str) -> Dict[str, Any]:
    return {
        "created_at": db.now_str(),
        "created_ts": time.time(),
        "method": request.method,
        "path": request.url.path,
        "client_format": client_format,
        "stream": 0,
        "status": 0,
        "duration_ms": 0,
        "retries": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "client_ip": request.client.host if request.client else "",
        "request_headers": jd(_mask_headers(request.headers)),
    }


def finalize_log(entry: Dict[str, Any], started: float, status: int,
                 error: str = "") -> None:
    entry["duration_ms"] = int((time.time() - started) * 1000)
    entry["status"] = status
    if error:
        entry["error"] = error
    write_log(entry)


def json_error(fmt: str, status: int, message: str,
               err_type: Optional[str] = None) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content=error_payload(fmt, message, err_type or status_to_err_type(status)),
    )


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
async def relay(request: Request, client_format: str,
                body_override: Optional[Dict[str, Any]] = None) -> Response:
    started = time.time()
    client_mod = get_format(client_format)
    entry = base_log(request, client_format)

    raw = await request.body()
    try:
        body = body_override if body_override is not None else (json.loads(raw) if raw else {})
    except Exception:
        finalize_log(entry, started, 400, "请求体不是合法 JSON")
        return json_error(client_format, 400, "请求体不是合法 JSON")

    if not isinstance(body, dict):
        finalize_log(entry, started, 400, "请求体必须是 JSON 对象")
        return json_error(client_format, 400, "请求体必须是 JSON 对象")

    limit, log_req, log_resp = _log_limits()
    entry["request_body"] = _clip(body, limit) if log_req else None
    entry["request_model"] = body.get("model") or ""

    ok, key_name, auth_err = check_auth(request)
    entry["api_key_name"] = key_name
    if not ok:
        finalize_log(entry, started, 401, auth_err)
        return json_error(client_format, 401, auth_err, "authentication_error")

    model = str(body.get("model") or "")
    stream = bool(body.get("stream"))
    entry["stream"] = 1 if stream else 0
    # 客户端是否要「思考内容」，决定响应侧是否渲染 thinking / reasoning 结构
    ctx = reasoning_context(client_format, body)

    # 脱敏上下文：会话决定 handle 的作用域，engine 承载一次请求内的映射查找
    mask_session = ""
    engine: Optional[MaskEngine] = None
    mask_init_error = ""

    channels = pick_channels(model)
    if not channels:
        msg = "没有可用的渠道，请先在管理后台添加渠道"
        finalize_log(entry, started, 503, msg)
        return json_error(client_format, 503, msg)

    if any(effective_mode(c) != MODE_OFF for c in channels):
        mask_session = resolve_session_id(request, body, client_format,
                                          extract_relay_key(request))
        try:
            engine = MaskEngine(load_policy(), get_store(mask_session))
        except Exception as exc:  # 脱敏初始化失败不应阻断转发
            mask_init_error = f"脱敏初始化失败: {exc}"
            mask_session, engine = "", None

    last_status = 502
    last_message = "没有可用渠道"
    last_type = "api_error"

    for attempt, channel in enumerate(channels):
        upstream_format = channel.get("type") or "openai"
        if upstream_format not in UPSTREAM_FORMATS:
            continue
        try:
            upstream_mod = get_format(upstream_format)
        except KeyError:
            continue
        upstream_model = map_model(channel, model)
        passthrough = upstream_format == client_format
        entry.update({
            "upstream_format": upstream_format,
            "channel_id": channel["id"],
            "channel_name": channel["name"],
            "upstream_model": upstream_model,
            "retries": attempt,
        })

        # ---- 本地上下文脱敏 -------------------------------------------- #
        mask_mode = effective_mode(channel)
        work_body = body
        hits: List[Any] = []
        if mask_mode != MODE_OFF and engine is not None:
            masked_copy = copy.deepcopy(body)
            hits = mask_body(masked_copy, client_format, engine)
            entry["mask_mode"] = mask_mode
            entry["mask_session"] = mask_session
            entry["mask_summary"] = jd(summarize_hits(hits))
            if store_enabled():
                entry["mask_map"] = jd(_hits_to_map(hits))
            if mask_mode == MODE_ENFORCE:
                work_body = masked_copy
            else:
                # 干跑：不改写真正发出去的请求，只记录「本应改成什么」
                entry["mask_preview"] = _clip(masked_copy, limit) if log_req else None

        masking = mask_mode == MODE_ENFORCE
        # 流式还原无法在原始 SSE 字节上做，因此强制走「解析 -> 还原 -> 重渲染」
        stream_passthrough = passthrough and not masking

        if passthrough:
            upstream_body = dict(work_body)
            upstream_body["model"] = upstream_model
        else:
            canonical = client_mod.request_to_canonical(work_body)
            canonical["model"] = upstream_model
            upstream_body = upstream_mod.canonical_to_request(canonical)

        include_usage = bool((body.get("stream_options") or {}).get("include_usage"))
        if stream and upstream_format == "openai" and channel.get("stream_usage") and not passthrough:
            upstream_body.setdefault("stream_options", {"include_usage": True})
            include_usage = include_usage or True

        endpoint = upstream_mod.ENDPOINT
        url = build_url(channel.get("base_url") or "", endpoint)
        if not url:
            continue
        headers = build_upstream_headers(channel, stream)
        entry["upstream_url"] = url
        entry["upstream_request"] = _clip(upstream_body, limit) if log_req else None

        if stream:
            try:
                client, resp = await _open_stream(channel, url, headers, upstream_body)
            except (UpstreamError, UpstreamTransportError) as exc:
                last_status, last_message = _exc_info(exc)
                last_type = status_to_err_type(last_status)
                entry["error"] = last_message
                continue
            entry["response_headers"] = jd(_mask_headers(resp.headers))
            return _streaming_response(
                client, resp, request=request, entry=entry, started=started,
                client_format=client_format, upstream_format=upstream_format,
                passthrough=stream_passthrough, upstream_mod=upstream_mod, limit=limit,
                log_resp=log_resp, include_usage=include_usage, model=upstream_model,
                ctx=ctx, engine=engine if masking else None,
            )

        try:
            resp = await _post_json(channel, url, headers, upstream_body)
        except UpstreamTransportError as exc:
            last_status, last_message = 502, exc.message
            last_type = "api_error"
            entry["error"] = last_message
            continue

        entry["response_headers"] = jd(_mask_headers(resp.headers))
        if resp.status_code >= 400:
            text = resp.text
            last_status = resp.status_code
            last_type = status_to_err_type(resp.status_code)
            last_message = _extract_error_message(text) or f"上游返回 {resp.status_code}"
            entry["error"] = f"[{channel['name']}] {last_message}"
            entry["response_body"] = _clip(text, limit) if log_resp else None
            continue

        try:
            data = resp.json()
        except Exception:
            last_status, last_message = 502, "上游返回了非 JSON 内容"
            entry["response_body"] = _clip(resp.text, limit) if log_resp else None
            continue

        if passthrough:
            out_body = data
        else:
            out_body = client_mod.canonical_to_response(
                upstream_mod.response_to_canonical(data), ctx)

        # 把 handle 还原成真值后再返回给客户端
        if masking:
            try:
                out_body = unmask_body(out_body, client_format, engine)
            except Exception as exc:
                entry["error"] = f"还原失败: {exc}"

        usage = normalize_usage(
            (out_body.get("usage") if isinstance(out_body, dict) else None))
        entry.update({
            "prompt_tokens": usage["prompt_tokens"],
            "completion_tokens": usage["completion_tokens"],
            "total_tokens": usage["total_tokens"],
        })
        entry["response_body"] = _clip(out_body, limit) if log_resp else None
        entry["error"] = mask_init_error or None
        finalize_log(entry, started, resp.status_code)
        return JSONResponse(content=out_body, status_code=200)

    finalize_log(entry, started, last_status, mask_init_error or last_message)
    return json_error(client_format, last_status, last_message, last_type)


def _mask_embedding_input(body: Dict[str, Any], engine: MaskEngine) -> List[Any]:
    """embeddings 的 input 是字符串或字符串数组。"""
    hits: List[Any] = []
    value = body.get("input")
    if isinstance(value, str):
        body["input"] = engine._mask_collect(value, hits)
    elif isinstance(value, list):
        body["input"] = [engine._mask_collect(v, hits) if isinstance(v, str)
                         else engine.mask_value(v, hits) for v in value]
    return hits


def _hits_to_map(hits: List[Any]) -> List[Dict[str, str]]:
    """命中列表去重成 handle -> raw 映射，供 WebUI 回溯。"""
    seen: Dict[str, Dict[str, str]] = {}
    for hit in hits:
        handle = getattr(hit, "handle", "")
        if not handle or handle in seen:
            continue
        seen[handle] = {
            "handle": handle,
            "raw": getattr(hit, "raw", ""),
            "kind": getattr(hit, "kind", ""),
            "slot_type": getattr(hit, "slot_type", ""),
        }
    return list(seen.values())


def _exc_info(exc: Exception) -> Tuple[int, str]:
    if isinstance(exc, UpstreamError):
        msg = _extract_error_message(exc.body) or f"上游返回 {exc.status}"
        return exc.status, msg
    return 502, str(exc)


def _extract_error_message(body: Any) -> str:
    data = body
    if isinstance(body, (str, bytes)):
        try:
            data = json.loads(body.decode("utf-8", "ignore") if isinstance(body, bytes) else body)
        except Exception:
            return (body.decode("utf-8", "ignore") if isinstance(body, bytes) else body)[:500]
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            return str(err.get("message") or err)
        if isinstance(err, str):
            return err
        for key in ("message", "detail", "msg", "error_msg"):
            if data.get(key):
                return str(data[key])[:500]
    return jd(data)[:500]


async def _post_json(channel: Dict[str, Any], url: str, headers: Dict[str, str],
                     body: Dict[str, Any]) -> httpx.Response:
    timeout = httpx.Timeout(float(channel.get("timeout") or 300), connect=20.0)
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            return await client.post(url, headers=headers, json=body)
    except httpx.HTTPError as exc:
        raise UpstreamTransportError(f"连接上游失败: {exc}", url) from exc


async def _open_stream(channel: Dict[str, Any], url: str, headers: Dict[str, str],
                       body: Dict[str, Any]) -> Tuple[httpx.AsyncClient, httpx.Response]:
    timeout = httpx.Timeout(float(channel.get("timeout") or 300), connect=20.0)
    client = httpx.AsyncClient(timeout=timeout, follow_redirects=True)
    try:
        req = client.build_request("POST", url, headers=headers, json=body)
        resp = await client.send(req, stream=True)
    except httpx.HTTPError as exc:
        await client.aclose()
        raise UpstreamTransportError(f"连接上游失败: {exc}", url) from exc
    if resp.status_code >= 400:
        try:
            data = await resp.aread()
        finally:
            await resp.aclose()
            await client.aclose()
        raise UpstreamError(resp.status_code, data, url)
    return client, resp


# --------------------------------------------------------------------------- #
# 流式转发
# --------------------------------------------------------------------------- #
def _streaming_response(client: httpx.AsyncClient, resp: httpx.Response, *,
                        request: Request, entry: Dict[str, Any], started: float,
                        client_format: str, upstream_format: str, passthrough: bool,
                        upstream_mod: Any, limit: int, log_resp: bool,
                        include_usage: bool, model: str,
                        ctx: Optional[Dict[str, Any]] = None,
                        engine: Optional[MaskEngine] = None) -> StreamingResponse:
    client_mod = get_format(client_format)
    ctx = ctx or {}
    meta = {
        "id": None,
        "model": model,
        "created": int(time.time()),
        "include_usage": include_usage,
        "reasoning": bool(ctx.get("reasoning")),
    }
    # 流式还原：在 canonical 分片层做，一次覆盖三种上游协议
    unmasker = CanonicalChunkUnmasker(engine) if engine is not None else None
    renderer = client_mod.StreamRenderer(meta)
    parser = upstream_mod.StreamParser()
    state: Dict[str, Any] = {
        "chunks": [],
        "raw": [],
        "raw_len": 0,
        "error": "",
        "finished": False,
        "event_name": None,
        "data_lines": [],
    }

    def feed_event(event_name: Optional[str], data_text: str) -> List[str]:
        """处理一个完整的 SSE 事件，返回需要下发给客户端的文本。"""
        if data_text == "[DONE]":
            state["finished"] = True
            return [] if not passthrough else ["data: [DONE]\n\n"]
        payload = safe_json(data_text, None)
        if payload is None:
            return []
        try:
            chunks = parser.feed(payload)
        except StreamError as exc:
            state["error"] = str(exc)
            fn = getattr(client_mod, "stream_error_frames", None)
            return fn(str(exc), "api_error") if fn else []
        if passthrough:
            if state["error"]:
                return []
            state["chunks"].extend(chunks)
            return []
        out: List[str] = []
        for chunk in chunks:
            if unmasker is not None:
                chunk = unmasker.push(chunk)
            out.extend(renderer.push(chunk))
        state["chunks"].extend(chunks)
        return out

    async def event_generator() -> AsyncIterator[str]:
        try:
            async for line in resp.aiter_lines():
                if not passthrough:
                    if line.startswith("event:"):
                        state["event_name"] = line[6:].strip()
                        continue
                    if line.startswith("data:"):
                        state["data_lines"].append(line[5:].lstrip())
                        continue
                    if line.startswith(":"):
                        continue
                    if line.strip() == "":
                        if state["data_lines"]:
                            data_text = "\n".join(state["data_lines"])
                            state["data_lines"] = []
                            for frame in feed_event(state["event_name"], data_text):
                                yield frame
                            state["event_name"] = None
                        continue
                    continue

                # 透传模式：原样转发每一行
                raw = line + "\n"
                if state["raw_len"] < limit:
                    state["raw"].append(raw)
                    state["raw_len"] += len(raw)
                if line.startswith("event:"):
                    state["event_name"] = line[6:].strip()
                    yield raw
                    continue
                if line.startswith("data:"):
                    state["data_lines"].append(line[5:].lstrip())
                    yield raw
                    continue
                if line.startswith(":"):
                    yield raw
                    continue
                if line.strip() == "":
                    yield "\n"
                    if state["data_lines"]:
                        data_text = "\n".join(state["data_lines"])
                        state["data_lines"] = []
                        feed_event(state["event_name"], data_text)
                        state["event_name"] = None
                    continue
                yield raw

            # 收尾：把最后一个未闭合的事件处理掉
            if state["data_lines"]:
                data_text = "\n".join(state["data_lines"])
                state["data_lines"] = []
                for frame in feed_event(state["event_name"], data_text):
                    yield frame

            try:
                for chunk in parser.finalize():
                    if passthrough:
                        state["chunks"].append(chunk)
                    else:
                        if unmasker is not None:
                            chunk = unmasker.push(chunk)
                        for frame in renderer.push(chunk):
                            yield frame
                        state["chunks"].append(chunk)
            except StreamError as exc:
                state["error"] = str(exc)

            # 冲刷还原缓冲里的残留，避免末尾几个字符被吞掉
            if unmasker is not None and not state["error"]:
                try:
                    leftover = unmasker.flush()
                    if leftover is not None:
                        for frame in renderer.push(leftover):
                            yield frame
                        state["chunks"].append(leftover)
                except Exception:
                    pass

            if not state["error"]:
                for frame in renderer.close():
                    yield frame
        except Exception as exc:  # 网络中断等
            state["error"] = state["error"] or f"流式转发中断: {exc}"
            fn = getattr(client_mod, "stream_error_frames", None)
            if fn:
                for frame in fn(state["error"], "api_error"):
                    yield frame
        finally:
            try:
                await resp.aclose()
            except Exception:
                pass
            try:
                await client.aclose()
            except Exception:
                pass
            assembled = assemble_chunks(state["chunks"], model=model)
            usage = normalize_usage(assembled.get("usage"))
            entry["prompt_tokens"] = usage["prompt_tokens"]
            entry["completion_tokens"] = usage["completion_tokens"]
            entry["total_tokens"] = usage["total_tokens"]
            if log_resp:
                entry["response_body"] = _clip(assembled, limit)
                entry["stream_raw"] = _clip("".join(state["raw"]), max(limit // 2, 4096))
            if state["error"]:
                entry["error"] = state["error"]
            else:
                entry["error"] = None
            finalize_log(entry, started, 200 if not state["error"] else 502,
                         state["error"])

    headers = dict(SSE_HEADERS)
    upstream_ct = resp.headers.get("content-type")
    if upstream_ct:
        headers["X-Upstream-Content-Type"] = upstream_ct
    return StreamingResponse(event_generator(), media_type="text/event-stream",
                             headers=headers)


# --------------------------------------------------------------------------- #
# 其它端点
# --------------------------------------------------------------------------- #
def list_models(request: Request) -> Response:
    models: List[str] = []
    seen = set()

    def add(name: str) -> None:
        name = (name or "").strip()
        if name and name not in seen:
            seen.add(name)
            models.append(name)

    for channel in db.list_channels(enabled_only=True):
        for m in channel.get("model_list") or []:
            add(m.replace("*", "") or m)
        for k in (channel.get("model_map") or {}).keys():
            if k != "*":
                add(k)
    if not models:
        for m in ("gpt-4o", "gpt-4o-mini", "claude-3-5-sonnet-20241022", "deepseek-chat"):
            add(m)
    created = int(time.time())
    return JSONResponse(content={
        "object": "list",
        "data": [{"id": m, "object": "model", "created": created, "owned_by": "jaisafe-gateway"}
                 for m in models],
    })


def count_tokens(request: Request, body: Dict[str, Any]) -> Response:
    """粗略估算 token 数（Anthropic 客户端会调用）。"""
    text_parts: List[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, str):
            text_parts.append(node)
        elif isinstance(node, dict):
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(body.get("system"))
    walk(body.get("messages"))
    walk(body.get("tools"))
    total_chars = sum(len(p) for p in text_parts)
    return JSONResponse(content={"input_tokens": max(1, total_chars // 4)})


def health() -> Response:
    return JSONResponse(content={
        "status": "ok",
        "time": db.now_str(),
        "channels": len(db.list_channels(enabled_only=True)),
        "require_api_key": db.get_setting("require_api_key", "0") == "1"
        or db.count_enabled_keys() > 0,
    })


async def relay_passthrough(request: Request, endpoint: str) -> Response:
    """原样转发（不做格式转换）的端点，例如 /v1/embeddings。"""
    started = time.time()
    entry = base_log(request, "passthrough")
    entry["path"] = request.url.path
    limit, log_req, log_resp = _log_limits()

    raw = await request.body()
    try:
        body = json.loads(raw) if raw else {}
    except Exception:
        finalize_log(entry, started, 400, "请求体不是合法 JSON")
        return json_error("openai", 400, "请求体不是合法 JSON")
    if not isinstance(body, dict):
        body = {}

    entry["request_body"] = _clip(body, limit) if log_req else None
    entry["request_model"] = body.get("model") or ""
    ok, key_name, auth_err = check_auth(request)
    entry["api_key_name"] = key_name
    if not ok:
        finalize_log(entry, started, 401, auth_err)
        return json_error("openai", 401, auth_err, "authentication_error")

    model = str(body.get("model") or "")
    channels = pick_channels(model)
    if not channels:
        msg = "没有可用的渠道"
        finalize_log(entry, started, 503, msg)
        return json_error("openai", 503, msg)

    last_status, last_message = 502, "没有可用渠道"
    for attempt, channel in enumerate(channels):
        if channel.get("type") not in ("openai",):
            continue
        url = build_url(channel.get("base_url") or "", endpoint)
        if not url:
            continue
        upstream_body = copy.deepcopy(body)

        # embeddings 的输入同样可能夹带本机路径 / 凭证；这里只做脱敏，
        # 不做还原（向量无法还原，且客户端要的就是脱敏后文本的向量）。
        mask_mode = effective_mode(channel)
        if mask_mode == MODE_ENFORCE and endpoint == "embeddings":
            try:
                engine = MaskEngine(load_policy(), get_store(
                    resolve_session_id(request, body, "openai", extract_relay_key(request))))
                hits = _mask_embedding_input(upstream_body, engine)
                entry["mask_mode"] = mask_mode
                entry["mask_summary"] = jd(summarize_hits(hits))
                if store_enabled():
                    entry["mask_map"] = jd(_hits_to_map(hits))
            except Exception as exc:
                entry["error"] = f"脱敏失败: {exc}"
        upstream_body["model"] = map_model(channel, model)
        headers = build_upstream_headers(channel, False)
        entry.update({
            "upstream_format": channel.get("type"),
            "channel_id": channel["id"],
            "channel_name": channel["name"],
            "upstream_model": upstream_body["model"],
            "upstream_url": url,
            "upstream_request": _clip(upstream_body, limit) if log_req else None,
            "retries": attempt,
        })
        try:
            resp = await _post_json(channel, url, headers, upstream_body)
        except UpstreamTransportError as exc:
            last_status, last_message = 502, exc.message
            entry["error"] = last_message
            continue
        entry["response_headers"] = jd(_mask_headers(resp.headers))
        if resp.status_code >= 400:
            last_status = resp.status_code
            last_message = _extract_error_message(resp.text) or f"上游返回 {resp.status_code}"
            entry["error"] = f"[{channel['name']}] {last_message}"
            entry["response_body"] = _clip(resp.text, limit) if log_resp else None
            continue
        try:
            data = resp.json()
        except Exception:
            last_status, last_message = 502, "上游返回了非 JSON 内容"
            continue
        usage = normalize_usage(data.get("usage") if isinstance(data, dict) else None)
        entry.update({
            "prompt_tokens": usage["prompt_tokens"],
            "completion_tokens": usage["completion_tokens"],
            "total_tokens": usage["total_tokens"],
        })
        entry["response_body"] = _clip(data, limit) if log_resp else None
        finalize_log(entry, started, resp.status_code)
        return JSONResponse(content=data, status_code=200)

    finalize_log(entry, started, last_status, last_message)
    return json_error("openai", last_status, last_message)
