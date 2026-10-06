"""端到端冒烟测试：覆盖 4 种客户端格式 × 3 种上游格式 × 流式/非流式。

运行: python tests/smoke_test.py
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

DATA_DIR = os.path.join(ROOT, "data_test")
if os.path.isdir(DATA_DIR):
    shutil.rmtree(DATA_DIR, ignore_errors=True)
os.environ["JAI_DATA_DIR"] = DATA_DIR
os.environ.pop("OPENAI_BASE_URL", None)
os.environ.pop("OPENAI_API_KEY", None)

import httpx  # noqa: E402
import uvicorn  # noqa: E402

from tests.mock_upstream import app as mock_app  # noqa: E402
from app import db  # noqa: E402
from app.formats import anthropic, openai_responses  # noqa: E402
from app.main import app as gw_app  # noqa: E402

UPSTREAM = "http://127.0.0.1:18080"
DEAD = "http://127.0.0.1:18099"
EXPECT_TEXT = "你好，这是一段来自上游的测试文本。"

PASSED = 0
FAILED: List[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASSED
    if cond:
        PASSED += 1
    else:
        FAILED.append(f"{name} {detail}")
        print(f"  [FAIL] {name} {detail}")


def parse_sse(text: str) -> List[Tuple[str, Any]]:
    events: List[Tuple[str, Any]] = []
    event = ""
    data_lines: List[str] = []
    for line in text.split("\n"):
        if line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
        elif line.strip() == "":
            if data_lines:
                raw = "\n".join(data_lines)
                try:
                    payload = json.loads(raw)
                except Exception:
                    payload = raw
                events.append((event, payload))
            event = ""
            data_lines = []
    return events


def stream_text(events: List[Tuple[str, Any]], client_format: str) -> str:
    parts: List[str] = []
    for _ev, payload in events:
        if not isinstance(payload, dict):
            continue
        if client_format == "openai":
            for choice in payload.get("choices") or []:
                parts.append((choice.get("delta") or {}).get("content") or "")
        elif client_format == "openai_completions":
            for choice in payload.get("choices") or []:
                parts.append(choice.get("text") or "")
        elif client_format == "anthropic":
            if payload.get("type") == "content_block_delta":
                delta = payload.get("delta") or {}
                if delta.get("type") == "text_delta":
                    parts.append(delta.get("text") or "")
        elif client_format == "openai_responses":
            if payload.get("type") == "response.output_text.delta":
                parts.append(payload.get("delta") or "")
    return "".join(parts)


def stream_tool_names(events: List[Tuple[str, Any]], client_format: str) -> List[str]:
    names: List[str] = []
    for _ev, payload in events:
        if not isinstance(payload, dict):
            continue
        if client_format in ("openai", "openai_completions"):
            for choice in payload.get("choices") or []:
                for tc in (choice.get("delta") or {}).get("tool_calls") or []:
                    n = (tc.get("function") or {}).get("name")
                    if n:
                        names.append(n)
        elif client_format == "anthropic":
            if payload.get("type") == "content_block_start":
                block = payload.get("content_block") or {}
                if block.get("type") == "tool_use":
                    names.append(block.get("name") or "")
        elif client_format == "openai_responses":
            if payload.get("type") == "response.output_item.added":
                item = payload.get("item") or {}
                if item.get("type") == "function_call":
                    names.append(item.get("name") or "")
    return names


# --------------------------------------------------------------------------- #
def start_mock() -> uvicorn.Server:
    config = uvicorn.Config(mock_app, host="127.0.0.1", port=18080, log_level="error")
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        try:
            httpx.get(f"{UPSTREAM}/v1/models", timeout=1.0)
            return server
        except Exception:
            time.sleep(0.1)
    raise RuntimeError("mock upstream 启动失败")


def build_request(client_format: str, model: str, stream: bool,
                  with_tools: bool = False) -> Dict[str, Any]:
    if client_format == "openai":
        body: Dict[str, Any] = {
            "model": model, "stream": stream,
            "messages": [{"role": "user", "content": "你好"}],
        }
    elif client_format == "anthropic":
        body = {
            "model": model, "stream": stream, "max_tokens": 128,
            "system": "你是助手",
            "messages": [{"role": "user", "content": "你好"}],
        }
    elif client_format == "openai_responses":
        body = {"model": model, "stream": stream, "input": "你好",
                "instructions": "你是助手"}
    else:  # legacy completions
        body = {"model": model, "stream": stream, "prompt": "你好"}
    if with_tools:
        if client_format == "anthropic":
            body["tools"] = [{"name": "get_weather", "description": "查询天气",
                              "input_schema": {"type": "object",
                                               "properties": {"city": {"type": "string"}},
                                               "required": ["city"]}}]
            body["tool_choice"] = {"type": "any"}
        elif client_format == "openai_responses":
            body["tools"] = [{"type": "function", "name": "get_weather",
                              "description": "查询天气",
                              "parameters": {"type": "object",
                                             "properties": {"city": {"type": "string"}}}}]
            body["tool_choice"] = "required"
        else:
            body["tools"] = [{"type": "function", "function": {
                "name": "get_weather", "description": "查询天气",
                "parameters": {"type": "object", "properties": {"city": {"type": "string"}}}}}]
            body["tool_choice"] = "required"
    return body


def endpoint_of(client_format: str) -> str:
    return {
        "openai": "/v1/chat/completions",
        "anthropic": "/v1/messages",
        "openai_responses": "/v1/responses",
        "openai_completions": "/v1/completions",
    }[client_format]


def extract_text(data: Dict[str, Any], client_format: str) -> str:
    if client_format in ("openai",):
        return (data["choices"][0]["message"].get("content") or "")
    if client_format == "openai_completions":
        return data["choices"][0].get("text") or ""
    if client_format == "anthropic":
        return "".join(b.get("text") or "" for b in data.get("content") or []
                       if b.get("type") == "text")
    if client_format == "openai_responses":
        text = data.get("output_text")
        if text:
            return text
        out = []
        for item in data.get("output") or []:
            for c in item.get("content") or []:
                if c.get("type") == "output_text":
                    out.append(c.get("text") or "")
        return "".join(out)
    return ""


def extract_tools(data: Dict[str, Any], client_format: str) -> List[str]:
    names: List[str] = []
    if client_format in ("openai",):
        for tc in data["choices"][0]["message"].get("tool_calls") or []:
            names.append((tc.get("function") or {}).get("name") or "")
    elif client_format == "anthropic":
        for b in data.get("content") or []:
            if b.get("type") == "tool_use":
                names.append(b.get("name") or "")
    elif client_format == "openai_responses":
        for item in data.get("output") or []:
            if item.get("type") == "function_call":
                names.append(item.get("name") or "")
    return names


async def run_matrix(client: httpx.AsyncClient) -> None:
    for upstream in ("openai", "anthropic", "openai_responses"):
        model = f"m-{upstream}"
        for client_format in ("openai", "anthropic", "openai_responses", "openai_completions"):
            for stream in (False, True):
                tag = f"{client_format}->{upstream} stream={stream}"
                body = build_request(client_format, model, stream)
                url = endpoint_of(client_format)
                if stream:
                    async with client.stream("POST", url, json=body) as resp:
                        raw = ""
                        async for chunk in resp.aiter_text():
                            raw += chunk
                        status = resp.status_code
                        ctype = resp.headers.get("content-type", "")
                    check(f"{tag} status", status == 200, f"got {status}: {raw[:200]}")
                    check(f"{tag} content-type", "text/event-stream" in ctype, ctype)
                    events = parse_sse(raw)
                    text = stream_text(events, client_format)
                    check(f"{tag} text", text == EXPECT_TEXT, repr(text))
                    if client_format in ("openai", "openai_completions"):
                        check(f"{tag} done-marker", "data: [DONE]" in raw)
                    elif client_format == "anthropic":
                        check(f"{tag} message_stop", any(e == "message_stop" for e, _ in events),
                              str([e for e, _ in events][-4:]))
                    else:
                        check(f"{tag} response.completed",
                              any(e == "response.completed" for e, _ in events))
                else:
                    resp = await client.post(url, json=body)
                    check(f"{tag} status", resp.status_code == 200, resp.text[:300])
                    data = resp.json()
                    text = extract_text(data, client_format)
                    check(f"{tag} text", text == EXPECT_TEXT, repr(text))
                    if client_format == "anthropic":
                        check(f"{tag} type", data.get("type") == "message", str(data.get("type")))
                        check(f"{tag} stop_reason", data.get("stop_reason") == "end_turn",
                              str(data.get("stop_reason")))
                    if client_format == "openai_responses":
                        check(f"{tag} object", data.get("object") == "response")
                        check(f"{tag} status", data.get("status") == "completed")
                    if client_format == "openai_completions":
                        check(f"{tag} object", data.get("object") == "text_completion")
                    check(f"{tag} usage", (data.get("usage") or {}).get("total_tokens", 0) > 0
                          or (data.get("usage") or {}).get("input_tokens", 0) > 0,
                          str(data.get("usage")))


async def run_tool_matrix(client: httpx.AsyncClient) -> None:
    for upstream in ("openai", "anthropic", "openai_responses"):
        model = f"m-{upstream}"
        for client_format in ("openai", "anthropic", "openai_responses"):
            for stream in (False, True):
                tag = f"tool {client_format}->{upstream} stream={stream}"
                body = build_request(client_format, model, stream, with_tools=True)
                url = endpoint_of(client_format)
                if stream:
                    async with client.stream("POST", url, json=body) as resp:
                        raw = ""
                        async for chunk in resp.aiter_text():
                            raw += chunk
                    events = parse_sse(raw)
                    names = stream_tool_names(events, client_format)
                else:
                    resp = await client.post(url, json=body)
                    check(f"{tag} status", resp.status_code == 200, resp.text[:300])
                    if resp.status_code != 200:
                        continue
                    names = extract_tools(resp.json(), client_format)
                check(f"{tag} tool name", "get_weather" in names, str(names))


async def run_upstream_shape(client: httpx.AsyncClient, mock: httpx.AsyncClient) -> None:
    """验证网关向上游发送的请求体确实是目标格式。"""
    await mock.delete("/_last")
    await client.post("/v1/messages", json=build_request("anthropic", "m-openai", False))
    last = (await mock.get("/_last")).json()
    body = last.get("/v1/chat/completions", {}).get("body", {})
    check("anthropic->openai upstream messages", isinstance(body.get("messages"), list),
          json.dumps(body)[:200])
    check("anthropic->openai system hoisted",
          (body.get("messages") or [{}])[0].get("role") == "system",
          json.dumps(body.get("messages"))[:200])
    check("anthropic->openai model mapped", body.get("model") == "openai-up-model",
          str(body.get("model")))

    await mock.delete("/_last")
    await client.post("/v1/chat/completions", json=build_request("openai", "m-anthropic", False))
    last = (await mock.get("/_last")).json()
    entry = last.get("/v1/messages", {})
    body = entry.get("body", {})
    check("openai->anthropic max_tokens", body.get("max_tokens") == 4096,
          str(body.get("max_tokens")))
    check("openai->anthropic auth header",
          entry.get("headers", {}).get("x-api-key") == "sk-anthropic-test",
          str(entry.get("headers", {}).get("x-api-key")))
    check("openai->anthropic version header",
          entry.get("headers", {}).get("anthropic-version") == "2023-06-01")
    check("openai->anthropic model mapped", body.get("model") == "anthropic-up-model",
          str(body.get("model")))

    await mock.delete("/_last")
    await client.post("/v1/chat/completions", json=build_request("openai", "m-responses", False))
    last = (await mock.get("/_last")).json()
    body = last.get("/v1/responses", {}).get("body", {})
    check("openai->responses input items", isinstance(body.get("input"), list),
          json.dumps(body)[:200])
    check("openai->responses instructions", body.get("instructions") == "你是助手"
          or body.get("instructions") is None, str(body.get("instructions")))

    # 工具往返：anthropic 客户端的 tool_result 应转换成 role=tool
    await mock.delete("/_last")
    conv: Dict[str, Any] = {
        "model": "m-openai", "max_tokens": 64,
        "messages": [
            {"role": "user", "content": "北京天气"},
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "toolu_1", "name": "get_weather",
                 "input": {"city": "北京"}}]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_1", "content": "晴，25 度"}]},
        ],
    }
    resp = await client.post("/v1/messages", json=conv)
    check("tool_result roundtrip status", resp.status_code == 200, resp.text[:200])
    last = (await mock.get("/_last")).json()
    msgs = last.get("/v1/chat/completions", {}).get("body", {}).get("messages", [])
    roles = [m.get("role") for m in msgs]
    check("tool_result -> role=tool", "tool" in roles, str(roles))
    check("tool_use -> assistant tool_calls",
          any(m.get("tool_calls") for m in msgs), json.dumps(msgs)[:300])


async def run_auth(client: httpx.AsyncClient) -> None:
    db.create_key({"name": "测试密钥", "key": "sk-jai-test-key"})
    resp = await client.post("/v1/chat/completions", json=build_request("openai", "m-openai", False))
    check("auth 缺失密钥 -> 401", resp.status_code == 401, str(resp.status_code))
    resp = await client.post("/v1/chat/completions", json=build_request("openai", "m-openai", False),
                             headers={"Authorization": "Bearer wrong"})
    check("auth 错误密钥 -> 401", resp.status_code == 401, str(resp.status_code))
    resp = await client.post("/v1/chat/completions", json=build_request("openai", "m-openai", False),
                             headers={"Authorization": "Bearer sk-jai-test-key"})
    check("auth 正确密钥 -> 200", resp.status_code == 200, resp.text[:200])
    resp = await client.post("/v1/messages", json=build_request("anthropic", "m-anthropic", False),
                             headers={"x-api-key": "sk-jai-test-key"})
    check("auth x-api-key -> 200", resp.status_code == 200, resp.text[:200])
    resp = await client.get("/v1/models")
    check("auth /v1/models -> 401", resp.status_code == 401, str(resp.status_code))
    resp = await client.get("/v1/models", headers={"Authorization": "Bearer sk-jai-test-key"})
    check("auth /v1/models -> 200", resp.status_code == 200, resp.text[:120])
    check("models 列表包含渠道模型",
          any(m["id"] == "m-openai" for m in resp.json().get("data", [])))
    db.execute("DELETE FROM api_keys")


async def run_failover(client: httpx.AsyncClient) -> None:
    db.clear_logs()
    resp = await client.post("/v1/chat/completions",
                             json=build_request("openai", "m-fallback", False))
    check("failover 成功", resp.status_code == 200, resp.text[:200])
    row = db.query_one("SELECT * FROM logs ORDER BY id DESC LIMIT 1")
    check("failover 记录重试次数", row and row["retries"] >= 1, str(row and row["retries"]))
    check("failover 落到正确渠道", row and row["channel_name"] == "openai-mock",
          str(row and row["channel_name"]))


async def run_logs(client: httpx.AsyncClient) -> None:
    db.clear_logs()
    resp = await client.post("/v1/chat/completions",
                             json=build_request("openai", "m-openai", False))
    await asyncio.sleep(0.05)
    row = db.query_one("SELECT * FROM logs ORDER BY id DESC LIMIT 1")
    check("日志已写入", row is not None)
    if not row:
        return
    check("日志记录请求体", "你好" in (row["request_body"] or ""), str(row["request_body"])[:120])
    check("日志记录响应体", EXPECT_TEXT in (row["response_body"] or ""),
          str(row["response_body"])[:120])
    check("日志记录上游请求", "messages" in (row["upstream_request"] or ""))
    check("日志 token 统计", row["total_tokens"] == 18, str(row["total_tokens"]))
    check("日志隐藏密钥", "sk-openai-test" not in (row["request_headers"] or "")
          and "sk-openai-test" not in (row["upstream_request"] or ""))

    # 流式日志
    db.clear_logs()
    async with client.stream("POST", "/v1/chat/completions",
                             json=build_request("openai", "m-openai", True)) as r:
        async for _ in r.aiter_text():
            pass
    await asyncio.sleep(0.1)
    row = db.query_one("SELECT * FROM logs ORDER BY id DESC LIMIT 1")
    check("流式日志已写入", row is not None)
    check("流式日志含拼装内容", row and EXPECT_TEXT in (row["response_body"] or ""),
          str(row and row["response_body"])[:200])
    check("流式日志含原始 SSE", row and "[DONE]" in (row["stream_raw"] or ""))


async def run_embeddings(client: httpx.AsyncClient) -> None:
    resp = await client.post("/v1/embeddings", json={"model": "m-openai", "input": "hello"})
    check("embeddings status", resp.status_code == 200, resp.text[:200])
    data = resp.json()
    check("embeddings 结构", data.get("object") == "list" and len(data.get("data") or []) == 1,
          json.dumps(data)[:200])


# --------------------------------------------------------------------------- #
# 推理等级（thinking / reasoning_effort / reasoning.effort）
# --------------------------------------------------------------------------- #
REASONING_TEXT = "让我先梳理一下思路。"


def build_reasoning_request(fmt: str, model: str, stream: bool) -> Dict[str, Any]:
    if fmt == "openai":
        return {"model": model, "stream": stream, "reasoning_effort": "high",
                "max_tokens": 40000, "messages": [{"role": "user", "content": "你好"}]}
    if fmt == "anthropic":
        return {"model": model, "stream": stream, "max_tokens": 40000,
                "thinking": {"type": "enabled", "budget_tokens": 16384},
                "messages": [{"role": "user", "content": "你好"}]}
    return {"model": model, "stream": stream, "input": "你好",
            "reasoning": {"effort": "high", "summary": "auto"}}


def extract_reasoning(data: Dict[str, Any], fmt: str) -> str:
    if fmt == "openai":
        return (data["choices"][0]["message"].get("reasoning_content") or "")
    if fmt == "anthropic":
        return "".join(b.get("thinking") or "" for b in data.get("content") or []
                       if b.get("type") == "thinking")
    if fmt == "openai_responses":
        parts: List[str] = []
        for item in data.get("output") or []:
            if item.get("type") == "reasoning":
                for s in item.get("summary") or []:
                    parts.append(s.get("text") or "")
        return "".join(parts)
    return ""


def stream_reasoning(events: List[Tuple[str, Any]], fmt: str) -> str:
    parts: List[str] = []
    for _ev, payload in events:
        if not isinstance(payload, dict):
            continue
        if fmt == "openai":
            for choice in payload.get("choices") or []:
                parts.append((choice.get("delta") or {}).get("reasoning_content") or "")
        elif fmt == "anthropic":
            if payload.get("type") == "content_block_delta":
                delta = payload.get("delta") or {}
                if delta.get("type") == "thinking_delta":
                    parts.append(delta.get("thinking") or "")
        elif fmt == "openai_responses":
            if payload.get("type") == "response.reasoning_summary_text.delta":
                parts.append(payload.get("delta") or "")
    return "".join(parts)


async def run_reasoning_matrix(client: httpx.AsyncClient) -> None:
    """推理内容必须按客户端协议的结构回吐（thinking 块 / reasoning 输出项）。"""
    for upstream in ("openai", "anthropic", "openai_responses"):
        model = f"m-{upstream}"
        for fmt in ("openai", "anthropic", "openai_responses"):
            for stream in (False, True):
                tag = f"reasoning {fmt}->{upstream} stream={stream}"
                body = build_reasoning_request(fmt, model, stream)
                url = endpoint_of(fmt)
                if stream:
                    async with client.stream("POST", url, json=body) as resp:
                        raw = ""
                        async for chunk in resp.aiter_text():
                            raw += chunk
                    check(f"{tag} status", resp.status_code == 200, raw[:200])
                    got = stream_reasoning(parse_sse(raw), fmt)
                else:
                    resp = await client.post(url, json=body)
                    check(f"{tag} status", resp.status_code == 200, resp.text[:200])
                    if resp.status_code != 200:
                        continue
                    got = extract_reasoning(resp.json(), fmt)
                check(f"{tag} 思考内容", got == REASONING_TEXT, repr(got))


async def run_reasoning_gating() -> None:
    """客户端没申请思考时，不应把上游的 reasoning 塞进结构化字段。"""
    canonical = {
        "id": "x", "object": "chat.completion", "created": 1, "model": "m",
        "choices": [{"index": 0, "finish_reason": "stop", "message": {
            "role": "assistant", "content": "hi", "reasoning_content": "think"}}],
        "usage": {},
    }
    anth = anthropic.canonical_to_response(canonical)
    check("anthropic 未申请 -> 无 thinking 块",
          all(b.get("type") != "thinking" for b in anth["content"]), json.dumps(anth["content"]))
    anth_ctx = anthropic.canonical_to_response(canonical, {"reasoning": True})
    check("anthropic 已申请 -> 有 thinking 块",
          anth_ctx["content"][0].get("type") == "thinking", json.dumps(anth_ctx["content"]))
    check("anthropic thinking 在 text 之前",
          [b["type"] for b in anth_ctx["content"]] == ["thinking", "text"])

    resp = openai_responses.canonical_to_response(canonical)
    check("responses 未申请 -> 无 reasoning 项",
          all(i.get("type") != "reasoning" for i in resp["output"]), json.dumps(resp["output"]))
    resp_ctx = openai_responses.canonical_to_response(canonical, {"reasoning": True})
    check("responses 已申请 -> 有 reasoning 项",
          resp_ctx["output"][0].get("type") == "reasoning", json.dumps(resp_ctx["output"]))
    check("responses reasoning 在 message 之前",
          [i["type"] for i in resp_ctx["output"]] == ["reasoning", "message"])


def run_reasoning_mapping() -> None:
    from app.formats.utils import (budget_to_effort, effort_to_budget, normalize_effort,
                                   reasoning_context)
    check("budget 1024 -> minimal", budget_to_effort(1024) == "minimal")
    check("budget 2048 -> low", budget_to_effort(2048) == "low")
    check("budget 8192 -> medium", budget_to_effort(8192) == "medium")
    check("budget 32768 -> high", budget_to_effort(32768) == "high")
    check("effort high -> 32768", effort_to_budget("high") == 32768)
    check("effort minimal -> 1024", effort_to_budget("minimal") == 1024)
    check("非法 effort 归一化为 None", normalize_effort("ludicrous") is None)
    check("effort 大小写不敏感", normalize_effort("HIGH") == "high")
    check("ctx: anthropic thinking 开启",
          reasoning_context("anthropic", {"thinking": {"type": "enabled",
                                                       "budget_tokens": 8192}})["effort"] == "medium")
    check("ctx: anthropic thinking 关闭",
          reasoning_context("anthropic", {"thinking": {"type": "disabled"}})["reasoning"] is False)
    check("ctx: responses reasoning",
          reasoning_context("openai_responses",
                            {"reasoning": {"effort": "low"}})["reasoning"] is True)
    check("ctx: openai 无 reasoning_effort",
          reasoning_context("openai", {"messages": []})["reasoning"] is False)


async def run_reasoning_params(client: httpx.AsyncClient, mock: httpx.AsyncClient) -> None:
    """校验网关转发给上游的「思考等级」参数形态。"""
    # 1) Anthropic thinking(budget) -> OpenAI reasoning_effort
    await mock.delete("/_last")
    await client.post("/v1/messages", json={
        "model": "m-openai", "max_tokens": 40000,
        "thinking": {"type": "enabled", "budget_tokens": 16384},
        "messages": [{"role": "user", "content": "hi"}]})
    body = (await mock.get("/_last")).json()["/v1/chat/completions"]["body"]
    check("thinking(16384) -> reasoning_effort=high", body.get("reasoning_effort") == "high",
          str(body.get("reasoning_effort")))

    await mock.delete("/_last")
    await client.post("/v1/messages", json={
        "model": "m-openai", "max_tokens": 4096,
        "thinking": {"type": "enabled", "budget_tokens": 1500},
        "messages": [{"role": "user", "content": "hi"}]})
    body = (await mock.get("/_last")).json()["/v1/chat/completions"]["body"]
    check("thinking(1500) -> reasoning_effort=low", body.get("reasoning_effort") == "low",
          str(body.get("reasoning_effort")))

    # 2) 未开启 thinking 时不应凭空产生 reasoning_effort
    await mock.delete("/_last")
    await client.post("/v1/messages", json={
        "model": "m-openai", "max_tokens": 4096,
        "messages": [{"role": "user", "content": "hi"}]})
    body = (await mock.get("/_last")).json()["/v1/chat/completions"]["body"]
    check("未开 thinking -> 无 reasoning_effort", "reasoning_effort" not in body,
          json.dumps(body)[:200])

    # 3) OpenAI reasoning_effort -> Anthropic thinking（含 max_tokens 腾挪与 temperature 剔除）
    await mock.delete("/_last")
    await client.post("/v1/chat/completions", json={
        "model": "m-anthropic", "reasoning_effort": "high", "temperature": 0.7,
        "messages": [{"role": "user", "content": "hi"}]})
    body = (await mock.get("/_last")).json()["/v1/messages"]["body"]
    check("effort high -> thinking budget 32768",
          (body.get("thinking") or {}).get("budget_tokens") == 32768, json.dumps(body)[:200])
    check("effort high -> thinking enabled",
          (body.get("thinking") or {}).get("type") == "enabled")
    check("effort high -> max_tokens 自动腾出空间", body.get("max_tokens") == 33792,
          str(body.get("max_tokens")))
    check("effort high -> 剔除 temperature", "temperature" not in body, json.dumps(body)[:200])

    await mock.delete("/_last")
    await client.post("/v1/chat/completions", json={
        "model": "m-anthropic", "reasoning_effort": "low", "top_p": 0.9,
        "messages": [{"role": "user", "content": "hi"}]})
    body = (await mock.get("/_last")).json()["/v1/messages"]["body"]
    check("effort low -> budget 2048",
          (body.get("thinking") or {}).get("budget_tokens") == 2048, json.dumps(body)[:200])
    check("effort low -> 剔除 top_p", "top_p" not in body, json.dumps(body)[:200])

    # 4) max_tokens 显式很小时按 Anthropic 约束收敛 budget
    await mock.delete("/_last")
    await client.post("/v1/chat/completions", json={
        "model": "m-anthropic", "reasoning_effort": "high", "max_tokens": 5000,
        "messages": [{"role": "user", "content": "hi"}]})
    body = (await mock.get("/_last")).json()["/v1/messages"]["body"]
    check("max_tokens 5000 -> budget 收敛到 4744",
          (body.get("thinking") or {}).get("budget_tokens") == 4744, json.dumps(body)[:200])
    check("max_tokens 保持显式值", body.get("max_tokens") == 5000, str(body.get("max_tokens")))

    # 5) OpenAI reasoning_effort -> Responses reasoning
    await mock.delete("/_last")
    await client.post("/v1/chat/completions", json={
        "model": "m-responses", "reasoning_effort": "low",
        "messages": [{"role": "user", "content": "hi"}]})
    body = (await mock.get("/_last")).json()["/v1/responses"]["body"]
    check("effort low -> responses.reasoning",
          body.get("reasoning") == {"effort": "low", "summary": "auto"},
          json.dumps(body.get("reasoning")))

    # 6) Responses reasoning.effort -> OpenAI / Anthropic
    await mock.delete("/_last")
    await client.post("/v1/responses", json={
        "model": "m-openai", "input": "hi", "reasoning": {"effort": "high"}})
    body = (await mock.get("/_last")).json()["/v1/chat/completions"]["body"]
    check("responses effort high -> openai reasoning_effort",
          body.get("reasoning_effort") == "high", str(body.get("reasoning_effort")))

    await mock.delete("/_last")
    await client.post("/v1/responses", json={
        "model": "m-anthropic", "input": "hi", "reasoning": {"effort": "high"}})
    body = (await mock.get("/_last")).json()["/v1/messages"]["body"]
    check("responses effort high -> anthropic thinking 32768",
          (body.get("thinking") or {}).get("budget_tokens") == 32768, json.dumps(body)[:200])


MASK_PATH = r"C:\ws\proj\src\main.py"
MASK_SECRET = "ghp_" + "Z" * 36


def mask_probe_body(fmt: str, model: str, stream: bool, text: str) -> Dict[str, Any]:
    """构造请求；文本以 `ECHO:<分片>:<内容>` 触发 mock 原样回显。"""
    probe = f"ECHO:{3 if stream else 0}:{text}"
    body: Dict[str, Any] = {"model": model, "stream": stream}
    if fmt == "openai":
        body["messages"] = [{"role": "user", "content": probe}]
    elif fmt == "anthropic":
        body["max_tokens"] = 256
        body["messages"] = [{"role": "user", "content": probe}]
    elif fmt == "openai_responses":
        body["input"] = probe
    else:
        body["prompt"] = probe
    return body


def tree_contains(node: Any, needle: str) -> bool:
    """在解析后的 JSON 树里搜字符串，避免被 JSON 转义（`\\\\`）骗到。"""
    if isinstance(node, str):
        return needle in node
    if isinstance(node, dict):
        return any(tree_contains(v, needle) for v in node.values())
    if isinstance(node, list):
        return any(tree_contains(v, needle) for v in node)
    return False


def jd_short(obj: Any) -> str:
    try:
        return json.dumps(obj, ensure_ascii=False)[:240]
    except Exception:
        return str(obj)[:240]


async def run_masking(client: httpx.AsyncClient, mock: httpx.AsyncClient) -> None:
    """端到端：上游只看到 handle，客户端拿回真值。"""
    db.set_setting("mask_mode", "enforce")
    db.set_setting("mask_workspace_roots", r"C:\ws")
    db.set_setting("mask_include_home", "0")
    db.set_setting("mask_paths", "1")
    db.set_setting("mask_credentials", "1")
    db.set_setting("mask_store_map", "1")
    db.set_setting("mask_session_source", "global")
    db.clear_logs()

    probe = f"读取 {MASK_PATH} 并用 token {MASK_SECRET} 调用"

    for upstream in ("openai", "anthropic", "openai_responses"):
        model = f"m-{upstream}"
        for fmt in ("openai", "anthropic", "openai_responses", "openai_completions"):
            for stream in (False, True):
                tag = f"mask {fmt}->{upstream} stream={stream}"
                body = mask_probe_body(fmt, model, stream, probe)
                url = endpoint_of(fmt)
                await mock.delete("/_last")
                if stream:
                    async with client.stream("POST", url, json=body) as resp:
                        raw = ""
                        async for chunk in resp.aiter_text():
                            raw += chunk
                    check(f"{tag} status", resp.status_code == 200, raw[:200])
                    got = stream_text(parse_sse(raw), fmt)
                else:
                    resp = await client.post(url, json=body)
                    check(f"{tag} status", resp.status_code == 200, resp.text[:200])
                    if resp.status_code != 200:
                        continue
                    got = extract_text(resp.json(), fmt)
                check(f"{tag} 客户端拿回真值", got == probe, repr(got[:160]))

                last = (await mock.get("/_last")).json()
                sent = next(iter(last.values()), {}).get("body", {})
                check(f"{tag} 上游未收到原始路径", not tree_contains(sent, MASK_PATH),
                      jd_short(sent))
                check(f"{tag} 上游未收到原始凭证", not tree_contains(sent, MASK_SECRET),
                      jd_short(sent))
                check(f"{tag} 上游未收到根目录", not tree_contains(sent, r"C:\ws"),
                      jd_short(sent))
                check(f"{tag} 上游收到路径 handle",
                      tree_contains(sent, "[WORKSPACE_ROOT_1]"), jd_short(sent))

    # 日志可回溯
    logs = db.list_logs(limit=5)["items"]
    row = db.get_log(logs[0]["id"]) if logs else None
    check("日志记录了脱敏模式", row and row["mask_mode"] == "enforce",
          str(row and row["mask_mode"]))
    check("日志记录了脱敏会话", row and row["mask_session"] == "global",
          str(row and row["mask_session"]))
    summary = json.loads((row or {}).get("mask_summary") or "{}")
    check("日志脱敏统计含路径与凭证",
          summary.get("paths", 0) >= 1 and summary.get("credentials", 0) >= 1,
          str(summary))
    mapping = json.loads((row or {}).get("mask_map") or "[]")
    raw_values = {m["raw"] for m in mapping}
    check("日志映射包含路径真值", MASK_PATH in raw_values, str(raw_values)[:200])
    check("日志映射包含凭证真值", MASK_SECRET in raw_values, str(raw_values)[:200])
    resp_logged: Any = {}
    try:
        resp_logged = json.loads((row or {}).get("response_body") or "{}")
    except Exception:
        resp_logged = {}
    check("日志响应体是还原后的内容", tree_contains(resp_logged, probe),
          str((row or {}).get("response_body"))[:200])
    check("日志响应体不含 handle",
          not tree_contains(resp_logged, "[WORKSPACE_ROOT_1]"),
          str((row or {}).get("response_body"))[:200])
    check("日志上游请求体是脱敏后的内容",
          MASK_SECRET not in ((row or {}).get("upstream_request") or "")
          and "[WORKSPACE_ROOT_1]" in ((row or {}).get("upstream_request") or ""),
          str((row or {}).get("upstream_request"))[:200])

    # 干跑模式：不改写上游请求，但记录预览
    db.set_setting("mask_mode", "dry_run")
    db.clear_logs()
    await mock.delete("/_last")
    resp = await client.post("/v1/chat/completions",
                             json=mask_probe_body("openai", "m-openai", False, probe))
    check("dry_run status", resp.status_code == 200, resp.text[:200])
    sent = next(iter((await mock.get("/_last")).json().values()), {}).get("body", {})
    check("dry_run 不改变上游请求（仍是真值）", tree_contains(sent, MASK_SECRET),
          jd_short(sent))
    row = db.get_log(db.list_logs(limit=1)["items"][0]["id"])
    check("dry_run 记录模式", row and row["mask_mode"] == "dry_run",
          str(row and row["mask_mode"]))
    preview = (row or {}).get("mask_preview") or ""
    check("dry_run 记录预览（脱敏后）",
          MASK_SECRET not in preview and "[WORKSPACE_ROOT_1]" in preview, preview[:220])

    db.set_setting("mask_mode", "off")


async def run_errors(client: httpx.AsyncClient) -> None:
    resp = await client.post("/v1/chat/completions", content=b"{not json")
    check("非法 JSON -> 400", resp.status_code == 400, str(resp.status_code))
    resp = await client.post("/v1/messages", content=b"{not json")
    check("非法 JSON(anthropic) -> 400", resp.status_code == 400, str(resp.status_code))
    check("anthropic 错误结构", resp.json().get("type") == "error", resp.text[:200])
    resp = await client.post("/v1/responses", content=b"{not json")
    check("非法 JSON(responses) -> 400", resp.status_code == 400, str(resp.status_code))
    resp = await client.post("/v1/chat/completions", json={"model": "no-such-model-xyz",
                                                           "messages": []})
    check("未知模型仍可兜底尝试", resp.status_code == 200, str(resp.status_code))
    resp = await client.post("/v1/messages/count_tokens",
                             json={"model": "m-anthropic",
                                   "messages": [{"role": "user", "content": "hi"}]})
    check("count_tokens", resp.status_code == 200 and "input_tokens" in resp.json(),
          resp.text[:120])


async def main() -> int:
    server = start_mock()
    db.init_db()
    db.execute("DELETE FROM channels")
    db.execute("DELETE FROM api_keys")
    base = {"model_map": {}, "extra_headers": {}, "timeout": 60, "stream_usage": 1,
            "enabled": 1, "priority": 0, "remark": ""}
    db.create_channel({**base, "name": "openai-mock", "type": "openai", "base_url": UPSTREAM,
                       "api_key": "sk-openai-test", "models": "m-openai, m-fallback",
                       "priority": 10, "model_map": {"m-openai": "openai-up-model",
                                                     "m-fallback": "openai-up-model"}})
    db.create_channel({**base, "name": "anthropic-mock", "type": "anthropic",
                       "base_url": UPSTREAM, "api_key": "sk-anthropic-test",
                       "models": "m-anthropic", "priority": 10,
                       "model_map": {"m-anthropic": "anthropic-up-model"}})
    db.create_channel({**base, "name": "responses-mock", "type": "openai_responses",
                       "base_url": UPSTREAM, "api_key": "sk-responses-test",
                       "models": "m-responses", "priority": 10,
                       "model_map": {"m-responses": "responses-up-model"}})
    db.create_channel({**base, "name": "dead-channel", "type": "openai", "base_url": DEAD,
                       "api_key": "sk-dead", "models": "m-fallback", "priority": 100})

    transport = httpx.ASGITransport(app=gw_app)
    mock = httpx.AsyncClient(base_url=UPSTREAM, timeout=30.0)
    async with httpx.AsyncClient(transport=transport, base_url="http://gateway",
                                 timeout=60.0) as client:
        print("\n== 1. 格式转换矩阵 (4 客户端 × 3 上游 × 流式/非流式) ==")
        await run_matrix(client)
        print("\n== 2. 工具调用矩阵 ==")
        await run_tool_matrix(client)
        print("\n== 3. 上游请求体形状 ==")
        await run_upstream_shape(client, mock)
        print("\n== 4. 鉴权 ==")
        await run_auth(client)
        print("\n== 5. 故障转移 ==")
        await run_failover(client)
        print("\n== 6. 日志 ==")
        await run_logs(client)
        print("\n== 7. Embeddings 透传 ==")
        await run_embeddings(client)
        print("\n== 8. 推理等级映射与响应门控 ==")
        run_reasoning_mapping()
        await run_reasoning_gating()
        print("\n== 9. 推理等级参数转发 ==")
        await run_reasoning_params(client, mock)
        print("\n== 10. 推理内容跨格式回吐 ==")
        await run_reasoning_matrix(client)
        print("\n== 11. 脱敏与还原（端到端） ==")
        await run_masking(client, mock)
        print("\n== 12. 错误处理 ==")
        await run_errors(client)
    await mock.aclose()

    server.should_exit = True
    time.sleep(0.3)

    print(f"\n通过: {PASSED}  失败: {len(FAILED)}")
    for f in FAILED:
        print("  - " + f)
    return 0 if not FAILED else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
