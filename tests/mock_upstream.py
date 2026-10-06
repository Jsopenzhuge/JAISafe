"""模拟上游服务，用于端到端验证格式转换。

启动: python tests/mock_upstream.py  (默认 127.0.0.1:18080)
"""
from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI(title="mock-upstream")

LAST: Dict[str, Any] = {}
TEXT_PARTS = ["你好", "，这是一段", "来自上游的测试文本。"]
REASONING_TEXT = "让我先梳理一下思路。"
FULL_TEXT = "".join(TEXT_PARTS)


def _sse(payload: Any, event: str = "") -> str:
    body = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    prefix = f"event: {event}\n" if event else ""
    return f"{prefix}data: {body}\n\n"


def _record(path: str, request: Request, body: Dict[str, Any]) -> None:
    LAST[path] = {
        "headers": {k.lower(): v for k, v in request.headers.items()},
        "body": body,
        "at": time.time(),
    }


def _tool_of(body: Dict[str, Any]) -> str:
    tools = body.get("tools") or []
    if not tools:
        return ""
    first = tools[0]
    if isinstance(first, dict):
        return first.get("name") or (first.get("function") or {}).get("name") or "echo"
    return "echo"


def _wants_tool(body: Dict[str, Any]) -> bool:
    tc = body.get("tool_choice")
    if tc in ("required", "any"):
        return True
    return bool(body.get("tools")) and tc not in ("none", None)


def _wants_reasoning(body: Dict[str, Any], kind: str) -> bool:
    if kind == "openai":
        return bool(body.get("reasoning_effort"))
    if kind == "anthropic":
        thinking = body.get("thinking") or {}
        return isinstance(thinking, dict) and thinking.get("type") == "enabled"
    reasoning = body.get("reasoning") or {}
    return isinstance(reasoning, dict) and bool(
        reasoning.get("effort") or reasoning.get("summary"))


def _echo_text(body: Dict[str, Any]) -> str:
    """把请求里的全部文本原样拼回来，用于验证「上游看到什么 / 客户端拿回什么」。"""
    parts: List[str] = []
    system = body.get("system")
    if isinstance(system, str):
        parts.append(system)
    elif isinstance(system, list):
        parts.extend(b.get("text", "") for b in system if isinstance(b, dict))
    if isinstance(body.get("instructions"), str):
        parts.append(body["instructions"])
    raw_input = body.get("input")
    if isinstance(raw_input, str):
        parts.append(raw_input)
    elif isinstance(raw_input, list):
        for item in raw_input:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                content = item.get("content")
                if isinstance(content, str):
                    parts.append(content)
                elif isinstance(content, list):
                    parts.extend(p.get("text", "") for p in content
                                 if isinstance(p, dict) and p.get("text"))
    prompt = body.get("prompt")
    if isinstance(prompt, str):
        parts.append(prompt)
    for msg in body.get("messages") or []:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                if isinstance(block.get("text"), str):
                    parts.append(block["text"])
                elif isinstance(block.get("content"), str):
                    parts.append(block["content"])
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") if isinstance(tc, dict) else None
            if isinstance(fn, dict) and isinstance(fn.get("arguments"), str):
                parts.append(fn["arguments"])
    return "\n".join(p for p in parts if p)


def _chunk_text(text: str, size: int) -> List[str]:
    if size <= 0:
        return [text]
    return [text[i:i + size] for i in range(0, len(text), size)] or [""]


#: 输入文本以该前缀开头时，mock 会原样回显它（`ECHO:<分片大小>:<内容>`）。
#: 用「输入文本」而不是自定义字段做触发：网关做格式转换时只会重建已知字段，
#: 自定义字段在非直通路径下会被丢弃，而用户文本在任何路径下都保留。
ECHO_PREFIX = "ECHO:"


def _probe(body: Dict[str, Any]) -> Any:
    text = _echo_text(body)
    if not text.startswith(ECHO_PREFIX):
        return None
    rest = text[len(ECHO_PREFIX):]
    size, sep, payload = rest.partition(":")
    if not sep:
        return (rest, 8)
    try:
        return (payload, int(size))
    except ValueError:
        return (rest, 8)


def _response_text(body: Dict[str, Any]) -> str:
    probe = _probe(body)
    return probe[0] if probe else FULL_TEXT


def _response_parts(body: Dict[str, Any]) -> List[str]:
    probe = _probe(body)
    if probe:
        return _chunk_text(probe[0], probe[1])
    return TEXT_PARTS


# --------------------------------------------------------------------------- #
# OpenAI Chat Completions
# --------------------------------------------------------------------------- #
@app.post("/v1/chat/completions")
async def chat(request: Request) -> Any:
    body = await request.json()
    _record("/v1/chat/completions", request, body)
    model = body.get("model") or "mock-model"
    stream = bool(body.get("stream"))
    tool = _tool_of(body) if _wants_tool(body) else ""
    reasoning = _wants_reasoning(body, "openai")
    text = _response_text(body)
    usage = {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}

    if not stream:
        if tool:
            message: Dict[str, Any] = {
                "role": "assistant", "content": None,
                "tool_calls": [{
                    "id": "call_mock_1", "type": "function",
                    "function": {"name": tool,
                                 "arguments": json.dumps({"city": "北京"}, ensure_ascii=False)},
                }],
            }
            finish = "tool_calls"
        else:
            message = {"role": "assistant", "content": text}
            finish = "stop"
        if reasoning:
            message["reasoning_content"] = REASONING_TEXT
        return JSONResponse({
            "id": "chatcmpl-mock", "object": "chat.completion", "created": 1700000000,
            "model": model,
            "choices": [{"index": 0, "message": message, "finish_reason": finish,
                         "logprobs": None}],
            "usage": usage,
        })

    def gen():
        def chunk(delta, finish=None, with_usage=False):
            payload = {
                "id": "chatcmpl-mock", "object": "chat.completion.chunk",
                "created": 1700000000, "model": model,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish,
                             "logprobs": None}],
            }
            if with_usage:
                payload["usage"] = usage
            return _sse(payload)

        yield chunk({"role": "assistant", "content": ""})
        if reasoning:
            for piece in ["让我先", "梳理一下思路。"]:
                yield chunk({"reasoning_content": piece})
        if tool:
            yield chunk({"tool_calls": [{"index": 0, "id": "call_mock_1", "type": "function",
                                         "function": {"name": tool, "arguments": ""}}]})
            yield chunk({"tool_calls": [{"index": 0, "function": {
                "arguments": json.dumps({"city": "北京"}, ensure_ascii=False)}}]})
            yield chunk({}, finish="tool_calls")
        else:
            for part in _response_parts(body):
                yield chunk({"content": part})
            yield chunk({}, finish="stop")
        if (body.get("stream_options") or {}).get("include_usage"):
            yield _sse({
                "id": "chatcmpl-mock", "object": "chat.completion.chunk",
                "created": 1700000000, "model": model, "choices": [], "usage": usage,
            })
        yield "data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


# --------------------------------------------------------------------------- #
# Anthropic Messages
# --------------------------------------------------------------------------- #
@app.post("/v1/messages")
async def anthropic_messages(request: Request) -> Any:
    body = await request.json()
    _record("/v1/messages", request, body)
    model = body.get("model") or "mock-claude"
    stream = bool(body.get("stream"))
    tool = _tool_of(body) if _wants_tool(body) else ""
    thinking = _wants_reasoning(body, "anthropic")
    text = _response_text(body)

    if not stream:
        content: List[Dict[str, Any]] = []
        if thinking:
            content.append({"type": "thinking", "thinking": REASONING_TEXT,
                            "signature": "sig-mock"})
        if tool:
            content.append({"type": "tool_use", "id": "toolu_mock_1", "name": tool,
                            "input": {"city": "北京"}})
            stop = "tool_use"
        else:
            content.append({"type": "text", "text": text})
            stop = "end_turn"
        usage = {"input_tokens": 11, "output_tokens": 7}
        if thinking:
            usage["output_tokens"] = 27
        return JSONResponse({
            "id": "msg_mock", "type": "message", "role": "assistant", "model": model,
            "content": content, "stop_reason": stop, "stop_sequence": None,
            "usage": usage,
        })

    def gen():
        index = 0
        yield _sse({"type": "message_start", "message": {
            "id": "msg_mock", "type": "message", "role": "assistant", "model": model,
            "content": [], "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": 11, "output_tokens": 1}}}, "message_start")
        if thinking:
            yield _sse({"type": "content_block_start", "index": index,
                        "content_block": {"type": "thinking", "thinking": ""}},
                       "content_block_start")
            for piece in ["让我先", "梳理一下思路。"]:
                yield _sse({"type": "content_block_delta", "index": index,
                            "delta": {"type": "thinking_delta", "thinking": piece}},
                           "content_block_delta")
            yield _sse({"type": "content_block_stop", "index": index}, "content_block_stop")
            index += 1
        if tool:
            yield _sse({"type": "content_block_start", "index": index, "content_block": {
                "type": "tool_use", "id": "toolu_mock_1", "name": tool, "input": {}}},
                "content_block_start")
            yield _sse({"type": "content_block_delta", "index": index, "delta": {
                "type": "input_json_delta", "partial_json": '{"city": '}}, "content_block_delta")
            yield _sse({"type": "content_block_delta", "index": index, "delta": {
                "type": "input_json_delta", "partial_json": '"北京"}'}}, "content_block_delta")
            yield _sse({"type": "content_block_stop", "index": index}, "content_block_stop")
            stop_reason = "tool_use"
        else:
            yield _sse({"type": "content_block_start", "index": index,
                        "content_block": {"type": "text", "text": ""}}, "content_block_start")
            for part in _response_parts(body):
                yield _sse({"type": "content_block_delta", "index": index,
                            "delta": {"type": "text_delta", "text": part}},
                           "content_block_delta")
            yield _sse({"type": "content_block_stop", "index": index}, "content_block_stop")
            stop_reason = "end_turn"
        yield _sse({"type": "message_delta",
                    "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                    "usage": {"output_tokens": 27 if thinking else 7}}, "message_delta")
        yield _sse({"type": "message_stop"}, "message_stop")

    return StreamingResponse(gen(), media_type="text/event-stream")


# --------------------------------------------------------------------------- #
# OpenAI Responses
# --------------------------------------------------------------------------- #
@app.post("/v1/responses")
async def openai_responses(request: Request) -> Any:
    body = await request.json()
    _record("/v1/responses", request, body)
    model = body.get("model") or "mock-model"
    stream = bool(body.get("stream"))
    tool = _tool_of(body) if _wants_tool(body) else ""
    reasoning = _wants_reasoning(body, "responses")
    text = _response_text(body)
    parts = _response_parts(body)
    usage = {"input_tokens": 11, "output_tokens": 7, "total_tokens": 18}

    reasoning_item = {
        "type": "reasoning", "id": "rs_mock",
        "summary": [{"type": "summary_text", "text": REASONING_TEXT}],
    }
    if tool:
        out_item: Dict[str, Any] = {
            "type": "function_call", "id": "fc_mock", "call_id": "call_mock_1",
            "name": tool, "arguments": json.dumps({"city": "北京"}, ensure_ascii=False),
            "status": "completed",
        }
    else:
        out_item = {"type": "message", "id": "msg_mock", "status": "completed",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": text,
                                 "annotations": []}]}
    final_output = ([reasoning_item] if reasoning else []) + [out_item]

    def response_obj(status: str, output: List[Dict[str, Any]]) -> Dict[str, Any]:
        return {
            "id": "resp_mock", "object": "response", "created_at": 1700000000,
            "status": status, "error": None, "incomplete_details": None,
            "instructions": body.get("instructions"), "max_output_tokens": None,
            "model": model, "output": output, "parallel_tool_calls": True,
            "previous_response_id": None, "reasoning": {"effort": None, "summary": None},
            "store": False, "temperature": 1.0, "text": {"format": {"type": "text"}},
            "tool_choice": "auto", "tools": [], "top_p": 1.0, "truncation": "disabled",
            "usage": usage, "user": None, "metadata": {},
        }

    if not stream:
        return JSONResponse(response_obj("completed", final_output))

    def gen():
        seq = 0

        def ev(etype: str, payload: Dict[str, Any]) -> str:
            nonlocal seq
            seq += 1
            data = {"type": etype, "sequence_number": seq}
            data.update(payload)
            return _sse(data, etype)

        yield ev("response.created", {"response": response_obj("in_progress", [])})
        index = 0
        if reasoning:
            partial = {"type": "reasoning", "id": "rs_mock", "summary": []}
            yield ev("response.output_item.added", {"output_index": index, "item": partial})
            yield ev("response.reasoning_summary_part.added", {
                "item_id": "rs_mock", "output_index": index, "summary_index": 0,
                "part": {"type": "summary_text", "text": ""}})
            for piece in ["让我先", "梳理一下思路。"]:
                yield ev("response.reasoning_summary_text.delta", {
                    "item_id": "rs_mock", "output_index": index, "summary_index": 0,
                    "delta": piece})
            yield ev("response.reasoning_summary_text.done", {
                "item_id": "rs_mock", "output_index": index, "summary_index": 0,
                "text": REASONING_TEXT})
            yield ev("response.reasoning_summary_part.done", {
                "item_id": "rs_mock", "output_index": index, "summary_index": 0,
                "part": {"type": "summary_text", "text": REASONING_TEXT}})
            yield ev("response.output_item.done", {"output_index": index,
                                                   "item": reasoning_item})
            index += 1

        if tool:
            partial_call = {"type": "function_call", "id": "fc_mock",
                            "call_id": "call_mock_1", "name": tool, "arguments": "",
                            "status": "in_progress"}
            yield ev("response.output_item.added", {"output_index": index,
                                                    "item": partial_call})
            for piece in ['{"city": ', '"北京"}']:
                yield ev("response.function_call_arguments.delta",
                         {"item_id": "fc_mock", "output_index": index, "delta": piece})
            yield ev("response.function_call_arguments.done",
                     {"item_id": "fc_mock", "output_index": index,
                      "arguments": out_item["arguments"]})
            yield ev("response.output_item.done", {"output_index": index, "item": out_item})
        else:
            item = {"type": "message", "id": "msg_mock", "status": "in_progress",
                    "role": "assistant", "content": []}
            yield ev("response.output_item.added", {"output_index": index, "item": item})
            yield ev("response.content_part.added", {
                "item_id": "msg_mock", "output_index": index, "content_index": 0,
                "part": {"type": "output_text", "text": "", "annotations": []}})
            for part in parts:
                yield ev("response.output_text.delta", {
                    "item_id": "msg_mock", "output_index": index, "content_index": 0,
                    "delta": part})
            yield ev("response.output_text.done", {
                "item_id": "msg_mock", "output_index": index, "content_index": 0,
                "text": text})
            yield ev("response.content_part.done", {
                "item_id": "msg_mock", "output_index": index, "content_index": 0,
                "part": {"type": "output_text", "text": text, "annotations": []}})
            yield ev("response.output_item.done", {"output_index": index, "item": out_item})
        yield ev("response.completed", {"response": response_obj("completed", final_output)})

    return StreamingResponse(gen(), media_type="text/event-stream")


# --------------------------------------------------------------------------- #
@app.post("/v1/embeddings")
async def embeddings(request: Request) -> Any:
    body = await request.json()
    _record("/v1/embeddings", request, body)
    model = body.get("model") or "mock-embed"
    inputs = body.get("input")
    count = len(inputs) if isinstance(inputs, list) else 1
    return JSONResponse({
        "object": "list", "model": model,
        "data": [{"object": "embedding", "index": i, "embedding": [0.1, 0.2, 0.3]}
                 for i in range(count)],
        "usage": {"prompt_tokens": 3, "total_tokens": 3},
    })


@app.get("/_last")
async def last() -> Any:
    return LAST


@app.delete("/_last")
async def clear_last() -> Any:
    LAST.clear()
    return {"ok": True}


@app.get("/v1/models")
async def models() -> Any:
    return {"object": "list", "data": [{"id": "mock-model", "object": "model"}]}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=18080, log_level="warning")
