"""OpenAI Responses API 格式 <-> 内部规范（OpenAI Chat Completions）。"""
from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional

from .utils import (BaseStreamParser, BaseStreamRenderer, jd, make_chunk, new_id,
                    normalize_effort, normalize_usage, safe_json, sse, StreamError, text_of)

NAME = "openai_responses"
LABEL = "OpenAI Responses API"
ENDPOINT = "responses"

TOOL_CHOICE_MAP = {"auto": "auto", "none": "none", "required": "required"}


# --------------------------------------------------------------------------- #
# 请求：Responses -> 规范
# --------------------------------------------------------------------------- #
def request_to_canonical(body: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    if body.get("model"):
        out["model"] = body["model"]
    messages: List[Dict[str, Any]] = []
    instructions = body.get("instructions")
    if instructions:
        messages.append({"role": "system", "content": text_of(instructions)})

    raw_input = body.get("input")
    if isinstance(raw_input, str):
        messages.append({"role": "user", "content": raw_input})
    elif isinstance(raw_input, list):
        for item in raw_input:
            if isinstance(item, str):
                messages.append({"role": "user", "content": item})
                continue
            if not isinstance(item, dict):
                continue
            itype = item.get("type")
            if itype in (None, "message"):
                role = item.get("role") or "user"
                if role == "developer":
                    role = "system"
                content = item.get("content")
                if isinstance(content, str):
                    messages.append({"role": role, "content": content})
                    continue
                parts: List[Dict[str, Any]] = []
                for p in content or []:
                    if not isinstance(p, dict):
                        continue
                    ptype = p.get("type")
                    if ptype in ("input_text", "output_text", "text"):
                        parts.append({"type": "text", "text": p.get("text") or ""})
                    elif ptype in ("input_image", "image_url"):
                        url = p.get("image_url") or p.get("url") or ""
                        if isinstance(url, dict):
                            url = url.get("url") or ""
                        parts.append({"type": "image_url", "image_url": {"url": url}})
                    elif ptype == "refusal":
                        parts.append({"type": "text", "text": p.get("refusal") or ""})
                if all(p["type"] == "text" for p in parts):
                    messages.append({"role": role, "content": "".join(p["text"] for p in parts)})
                else:
                    messages.append({"role": role, "content": parts})
            elif itype == "function_call":
                messages.append({
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": item.get("call_id") or item.get("id") or new_id("call"),
                        "type": "function",
                        "function": {
                            "name": item.get("name") or "",
                            "arguments": item.get("arguments") or "{}",
                        },
                    }],
                })
            elif itype == "function_call_output":
                output = item.get("output")
                if not isinstance(output, str):
                    output = jd(output)
                messages.append({
                    "role": "tool",
                    "tool_call_id": item.get("call_id") or "",
                    "content": output,
                })
            elif itype == "reasoning":
                continue
    out["messages"] = messages

    if body.get("max_output_tokens"):
        out["max_tokens"] = body["max_output_tokens"]
    for key in ("temperature", "top_p", "stream", "user", "metadata"):
        if body.get(key) is not None:
            out[key] = body[key]
    if body.get("parallel_tool_calls") is not None:
        out["parallel_tool_calls"] = body["parallel_tool_calls"]

    # Responses 的思考等级：reasoning = {"effort": ..., "summary": ...}
    reasoning = body.get("reasoning")
    if isinstance(reasoning, dict):
        effort = normalize_effort(reasoning.get("effort"))
        if effort:
            out["reasoning_effort"] = effort

    tools = []
    for t in body.get("tools") or []:
        if not isinstance(t, dict):
            continue
        if t.get("type") == "function" and t.get("name"):
            tools.append({
                "type": "function",
                "function": {
                    "name": t.get("name") or "",
                    "description": t.get("description") or "",
                    "parameters": t.get("parameters") or {"type": "object", "properties": {}},
                },
            })
    if tools:
        out["tools"] = tools
        tc = body.get("tool_choice")
        if isinstance(tc, str):
            out["tool_choice"] = tc
        elif isinstance(tc, dict) and tc.get("type") == "function":
            out["tool_choice"] = {"type": "function", "function": {"name": tc.get("name")}}

    text_cfg = body.get("text") or {}
    fmt = (text_cfg.get("format") or {}) if isinstance(text_cfg, dict) else {}
    if isinstance(fmt, dict):
        if fmt.get("type") == "json_object":
            out["response_format"] = {"type": "json_object"}
        elif fmt.get("type") == "json_schema":
            out["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": fmt.get("name") or "response",
                    "schema": fmt.get("schema") or {},
                    "strict": fmt.get("strict"),
                },
            }
    return out


# --------------------------------------------------------------------------- #
# 请求：规范 -> Responses
# --------------------------------------------------------------------------- #
def canonical_to_request(body: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    if body.get("model"):
        out["model"] = body["model"]
    instructions: List[str] = []
    items: List[Dict[str, Any]] = []
    for m in body.get("messages") or []:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        if role == "system":
            instructions.append(text_of(m.get("content")))
            continue
        if role == "tool":
            items.append({
                "type": "function_call_output",
                "call_id": m.get("tool_call_id") or "",
                "output": m.get("content") if isinstance(m.get("content"), str)
                else jd(m.get("content")),
            })
            continue
        if role == "assistant" and m.get("tool_calls"):
            text = text_of(m.get("content"))
            if text:
                items.append({"type": "message", "role": "assistant",
                              "content": [{"type": "output_text", "text": text}]})
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function") or {}
                items.append({
                    "type": "function_call",
                    "call_id": tc.get("id") or new_id("call"),
                    "name": fn.get("name") or "",
                    "arguments": fn.get("arguments") or "{}",
                })
            continue

        content = m.get("content")
        text_type = "input_text" if role == "user" else "output_text"
        parts: List[Dict[str, Any]] = []
        if isinstance(content, str):
            if content:
                parts.append({"type": text_type, "text": content})
        elif isinstance(content, list):
            for p in content:
                if not isinstance(p, dict):
                    continue
                if p.get("type") == "text":
                    parts.append({"type": text_type, "text": p.get("text") or ""})
                elif p.get("type") == "image_url":
                    url = p.get("image_url")
                    if isinstance(url, dict):
                        url = url.get("url") or ""
                    parts.append({"type": "input_image", "image_url": url})
        if not parts:
            parts.append({"type": text_type, "text": ""})
        items.append({"type": "message", "role": role, "content": parts})

    if instructions:
        out["instructions"] = "\n\n".join(s for s in instructions if s)
    out["input"] = items
    if body.get("max_tokens") or body.get("max_completion_tokens"):
        out["max_output_tokens"] = int(body.get("max_tokens") or body.get("max_completion_tokens"))
    for key in ("temperature", "top_p", "stream", "user"):
        if body.get(key) is not None:
            out[key] = body[key]

    tools = []
    for t in body.get("tools") or []:
        if not isinstance(t, dict):
            continue
        fn = t.get("function") if isinstance(t.get("function"), dict) else t
        if not fn.get("name"):
            continue
        tools.append({
            "type": "function",
            "name": fn.get("name"),
            "description": fn.get("description") or "",
            "parameters": fn.get("parameters") or {"type": "object", "properties": {}},
        })
    if tools:
        out["tools"] = tools
        tc = body.get("tool_choice")
        if isinstance(tc, str):
            out["tool_choice"] = tc
        elif isinstance(tc, dict) and tc.get("type") == "function":
            out["tool_choice"] = {"type": "function", "name": (tc.get("function") or {}).get("name")}

    rf = body.get("response_format")
    if isinstance(rf, dict):
        if rf.get("type") == "json_object":
            out["text"] = {"format": {"type": "json_object"}}
        elif rf.get("type") == "json_schema":
            js = rf.get("json_schema") or {}
            out["text"] = {"format": {
                "type": "json_schema",
                "name": js.get("name") or "response",
                "schema": js.get("schema") or {},
            }}

    # reasoning_effort -> Responses reasoning
    effort = normalize_effort(body.get("reasoning_effort"))
    if effort:
        out["reasoning"] = {"effort": effort, "summary": "auto"}
    return out


# --------------------------------------------------------------------------- #
# 响应：Responses -> 规范
# --------------------------------------------------------------------------- #
def response_to_canonical(body: Dict[str, Any]) -> Dict[str, Any]:
    texts: List[str] = []
    reasoning: List[str] = []
    tool_calls: List[Dict[str, Any]] = []
    for item in body.get("output") or []:
        if not isinstance(item, dict):
            continue
        itype = item.get("type")
        if itype == "message":
            for c in item.get("content") or []:
                if not isinstance(c, dict):
                    continue
                if c.get("type") in ("output_text", "text"):
                    texts.append(c.get("text") or "")
                elif c.get("type") == "refusal":
                    texts.append(c.get("refusal") or "")
        elif itype == "function_call":
            tool_calls.append({
                "id": item.get("call_id") or item.get("id") or new_id("call"),
                "type": "function",
                "function": {
                    "name": item.get("name") or "",
                    "arguments": item.get("arguments") or "{}",
                },
            })
        elif itype == "reasoning":
            for s in item.get("summary") or []:
                if isinstance(s, dict) and s.get("text"):
                    reasoning.append(s["text"])
    message: Dict[str, Any] = {"role": "assistant", "content": "".join(texts) or None}
    if reasoning:
        message["reasoning_content"] = "".join(reasoning)
    if tool_calls:
        message["tool_calls"] = tool_calls

    status = body.get("status")
    finish = "stop"
    if tool_calls:
        finish = "tool_calls"
    elif status == "incomplete" and (body.get("incomplete_details") or {}).get("reason") == "max_output_tokens":
        finish = "length"
    return {
        "id": body.get("id") or new_id(),
        "object": "chat.completion",
        "created": int(body.get("created_at") or time.time()),
        "model": body.get("model"),
        "choices": [{"index": 0, "message": message, "finish_reason": finish, "logprobs": None}],
        "usage": normalize_usage(body.get("usage")),
    }


# --------------------------------------------------------------------------- #
# 响应：规范 -> Responses
# --------------------------------------------------------------------------- #
def canonical_to_response(body: Dict[str, Any], ctx: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    choice = (body.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    output: List[Dict[str, Any]] = []
    reasoning = message.get("reasoning_content")
    if reasoning and (ctx or {}).get("reasoning"):
        output.append({
            "type": "reasoning",
            "id": new_id("rs"),
            "summary": [{"type": "summary_text", "text": reasoning}],
        })
    text = text_of(message.get("content"))
    output.append({
        "type": "message",
        "id": new_id("msg"),
        "status": "completed",
        "role": "assistant",
        "content": [{"type": "output_text", "text": text, "annotations": []}],
    })
    for tc in message.get("tool_calls") or []:
        fn = tc.get("function") or {}
        output.append({
            "type": "function_call",
            "id": new_id("fc"),
            "call_id": tc.get("id") or new_id("call"),
            "name": fn.get("name") or "",
            "arguments": fn.get("arguments") or "{}",
            "status": "completed",
        })
    usage = normalize_usage(body.get("usage"))
    finish = choice.get("finish_reason")
    status = "incomplete" if finish == "length" else "completed"
    return {
        "id": body.get("id") if str(body.get("id") or "").startswith("resp_") else new_id("resp"),
        "object": "response",
        "created_at": int(body.get("created") or time.time()),
        "status": status,
        "error": None,
        "incomplete_details": {"reason": "max_output_tokens"} if status == "incomplete" else None,
        "instructions": None,
        "max_output_tokens": None,
        "model": body.get("model"),
        "output": output,
        "output_text": text,
        "parallel_tool_calls": True,
        "previous_response_id": None,
        "reasoning": {"effort": None, "summary": None},
        "store": False,
        "temperature": 1.0,
        "text": {"format": {"type": "text"}},
        "tool_choice": "auto",
        "tools": [],
        "top_p": 1.0,
        "truncation": "disabled",
        "usage": {
            "input_tokens": usage["prompt_tokens"],
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens": usage["completion_tokens"],
            "output_tokens_details": {"reasoning_tokens": 0},
            "total_tokens": usage["total_tokens"],
        },
        "user": None,
        "metadata": {},
    }


# --------------------------------------------------------------------------- #
# 流式：解析
# --------------------------------------------------------------------------- #
class StreamParser(BaseStreamParser):
    def __init__(self) -> None:
        self.cid: Optional[str] = None
        self.model: Optional[str] = None
        self.created = int(time.time())
        self.finish_reason: Optional[str] = None
        self.usage: Dict[str, int] = {}
        self._tool_index: Dict[int, int] = {}
        self._tool_counter = 0
        self._completed = False
        self._started = False

    def feed(self, data: Dict[str, Any]) -> List[Dict[str, Any]]:
        if not isinstance(data, dict):
            return []
        etype = data.get("type")
        out: List[Dict[str, Any]] = []

        if etype == "error":
            err = data.get("error") or {}
            raise StreamError(err.get("message") if isinstance(err, dict) else str(err))

        if etype == "response.created":
            resp = data.get("response") or {}
            self.cid = resp.get("id") or self.cid
            self.model = resp.get("model") or self.model
            self.created = int(resp.get("created_at") or self.created)
            self._started = True
            out.append(make_chunk(self.cid, self.created, self.model,
                                  {"role": "assistant", "content": ""}))
        elif etype == "response.output_item.added":
            item = data.get("item") or {}
            oidx = data.get("output_index", 0)
            if item.get("type") == "function_call":
                idx = self._tool_counter
                self._tool_counter += 1
                self._tool_index[oidx] = idx
                out.append(make_chunk(self.cid, self.created, self.model, {
                    "tool_calls": [{
                        "index": idx,
                        "id": item.get("call_id") or item.get("id") or new_id("call"),
                        "type": "function",
                        "function": {"name": item.get("name") or "", "arguments": ""},
                    }]
                }))
        elif etype == "response.output_text.delta":
            if not self._started:
                self._started = True
                out.append(make_chunk(self.cid, self.created, self.model,
                                      {"role": "assistant", "content": ""}))
            out.append(make_chunk(self.cid, self.created, self.model,
                                  {"content": data.get("delta") or ""}))
        elif etype in ("response.reasoning_summary_text.delta",
                       "response.reasoning_text.delta"):
            if not self._started:
                self._started = True
                out.append(make_chunk(self.cid, self.created, self.model,
                                      {"role": "assistant", "content": ""}))
            out.append(make_chunk(self.cid, self.created, self.model,
                                  {"reasoning_content": data.get("delta") or ""}))
        elif etype == "response.function_call_arguments.delta":
            idx = self._tool_index.get(data.get("output_index", 0))
            if idx is not None:
                out.append(make_chunk(self.cid, self.created, self.model, {
                    "tool_calls": [{"index": idx,
                                    "function": {"arguments": data.get("delta") or ""}}]
                }))
        elif etype == "response.completed":
            self._completed = True
            resp = data.get("response") or {}
            self.cid = resp.get("id") or self.cid
            self.model = resp.get("model") or self.model
            self.usage = normalize_usage(resp.get("usage"))
            if self.usage.get("completion_tokens") and not self.finish_reason:
                self.finish_reason = "stop"
            out.append(make_chunk(self.cid, self.created, self.model, {},
                                  finish_reason=self.finish_reason or "stop",
                                  usage=self.usage))
        elif etype == "response.incomplete":
            self._completed = True
            self.finish_reason = "length"
            out.append(make_chunk(self.cid, self.created, self.model, {},
                                  finish_reason="length", usage=self.usage))
        return out

    def finalize(self) -> List[Dict[str, Any]]:
        if self._completed:
            return []
        self._completed = True
        return [make_chunk(self.cid, self.created, self.model, {},
                           finish_reason=self.finish_reason or "stop", usage=self.usage)]


# --------------------------------------------------------------------------- #
# 流式：渲染
# --------------------------------------------------------------------------- #
class StreamRenderer(BaseStreamRenderer):
    def __init__(self, meta: Optional[Dict[str, Any]] = None) -> None:
        meta = meta or {}
        self.meta = meta
        self.cid = meta.get("id") if str(meta.get("id") or "").startswith("resp_") else new_id("resp")
        self.model = meta.get("model")
        self.created = int(meta.get("created") or time.time())
        self.seq = 0
        self.started = False
        self.closed = False
        self.output: List[Dict[str, Any]] = []
        self.next_output_index = 0
        self.current: Optional[Dict[str, Any]] = None  # {"item":..., "index":..., "kind":...}
        self.tool_items: Dict[int, Dict[str, Any]] = {}
        self.finish_reason: Optional[str] = None
        self.usage: Dict[str, int] = {}
        self.text_buf: List[str] = []
        self.reasoning_buf: List[str] = []

    # -- 内部 -------------------------------------------------------------- #
    def _ev(self, etype: str, payload: Dict[str, Any]) -> str:
        self.seq += 1
        body = {"type": etype, "sequence_number": self.seq}
        body.update(payload)
        return sse(body, etype)

    def _snapshot(self, status: str) -> Dict[str, Any]:
        return {
            "id": self.cid,
            "object": "response",
            "created_at": self.created,
            "status": status,
            "error": None,
            "incomplete_details": None,
            "instructions": None,
            "max_output_tokens": None,
            "model": self.model,
            "output": list(self.output),
            "parallel_tool_calls": True,
            "previous_response_id": None,
            "reasoning": {"effort": None, "summary": None},
            "store": False,
            "temperature": 1.0,
            "text": {"format": {"type": "text"}},
            "tool_choice": "auto",
            "tools": [],
            "top_p": 1.0,
            "truncation": "disabled",
            "usage": {
                "input_tokens": self.usage.get("prompt_tokens", 0),
                "input_tokens_details": {"cached_tokens": 0},
                "output_tokens": self.usage.get("completion_tokens", 0),
                "output_tokens_details": {"reasoning_tokens": 0},
                "total_tokens": self.usage.get("total_tokens", 0),
            },
            "user": None,
            "metadata": {},
        }

    def _ensure_started(self) -> List[str]:
        if self.started:
            return []
        self.started = True
        snap = self._snapshot("in_progress")
        return [
            self._ev("response.created", {"response": snap}),
            self._ev("response.in_progress", {"response": snap}),
        ]

    def _open_message(self) -> List[str]:
        if self.current and self.current["kind"] == "message":
            return []
        out = self._close_current()
        item = {
            "type": "message", "id": new_id("msg"), "status": "in_progress",
            "role": "assistant", "content": [],
        }
        idx = self.next_output_index
        self.next_output_index += 1
        self.current = {"item": item, "index": idx, "kind": "message"}
        part = {"type": "output_text", "text": "", "annotations": []}
        out.append(self._ev("response.output_item.added", {"output_index": idx, "item": item}))
        out.append(self._ev("response.content_part.added", {
            "item_id": item["id"], "output_index": idx, "content_index": 0, "part": part}))
        return out

    def _open_reasoning(self) -> List[str]:
        if self.current and self.current["kind"] == "reasoning":
            return []
        out = self._close_current()
        item = {"type": "reasoning", "id": new_id("rs"), "summary": []}
        idx = self.next_output_index
        self.next_output_index += 1
        self.current = {"item": item, "index": idx, "kind": "reasoning"}
        out.append(self._ev("response.output_item.added", {"output_index": idx, "item": item}))
        out.append(self._ev("response.reasoning_summary_part.added", {
            "item_id": item["id"], "output_index": idx, "summary_index": 0,
            "part": {"type": "summary_text", "text": ""}}))
        return out

    def _close_current(self) -> List[str]:
        if not self.current:
            return []
        out: List[str] = []
        cur = self.current
        idx = cur["index"]
        item = cur["item"]
        if cur["kind"] == "message":
            text = "".join(self.text_buf)
            item["status"] = "completed"
            item["content"] = [{"type": "output_text", "text": text, "annotations": []}]
            out.append(self._ev("response.output_text.done", {
                "item_id": item["id"], "output_index": idx, "content_index": 0, "text": text}))
            out.append(self._ev("response.content_part.done", {
                "item_id": item["id"], "output_index": idx, "content_index": 0,
                "part": {"type": "output_text", "text": text, "annotations": []}}))
            out.append(self._ev("response.output_item.done", {"output_index": idx, "item": item}))
            self.output.append(item)
        elif cur["kind"] == "reasoning":
            text = "".join(self.reasoning_buf)
            item["summary"] = [{"type": "summary_text", "text": text}]
            out.append(self._ev("response.reasoning_summary_text.done", {
                "item_id": item["id"], "output_index": idx, "summary_index": 0, "text": text}))
            out.append(self._ev("response.reasoning_summary_part.done", {
                "item_id": item["id"], "output_index": idx, "summary_index": 0,
                "part": {"type": "summary_text", "text": text}}))
            out.append(self._ev("response.output_item.done", {"output_index": idx, "item": item}))
            self.output.append(item)
        self.current = None
        return out

    def _open_tool(self, oai_index: int, call_id: str, name: str) -> List[str]:
        out = self._close_current()
        item = {
            "type": "function_call", "id": new_id("fc"), "call_id": call_id or new_id("call"),
            "name": name or "", "arguments": "", "status": "in_progress",
        }
        idx = self.next_output_index
        self.next_output_index += 1
        self.current = {"item": item, "index": idx, "kind": "tool"}
        self.tool_items[oai_index] = self.current
        out.append(self._ev("response.output_item.added", {"output_index": idx, "item": item}))
        return out

    # -- 接口 -------------------------------------------------------------- #
    def push(self, chunk: Dict[str, Any]) -> List[str]:
        out: List[str] = []
        if chunk.get("id"):
            self.cid = chunk["id"] if str(chunk["id"]).startswith("resp_") else self.cid
        if chunk.get("model"):
            self.model = chunk["model"]
        usage = chunk.get("usage") or {}
        if usage:
            self.usage = normalize_usage(usage)
        out += self._ensure_started()

        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            if choice.get("finish_reason"):
                self.finish_reason = choice["finish_reason"]

            reasoning = delta.get("reasoning_content")
            if reasoning and self.meta.get("reasoning"):
                out += self._open_reasoning()
                self.reasoning_buf.append(reasoning)
                item = self.current["item"]
                out.append(self._ev("response.reasoning_summary_text.delta", {
                    "item_id": item["id"], "output_index": self.current["index"],
                    "summary_index": 0, "delta": reasoning,
                }))

            text = delta.get("content")
            if text:
                out += self._open_message()
                self.text_buf.append(text)
                item = self.current["item"]
                out.append(self._ev("response.output_text.delta", {
                    "item_id": item["id"], "output_index": self.current["index"],
                    "content_index": 0, "delta": text,
                }))

            for tc in delta.get("tool_calls") or []:
                oai_idx = tc.get("index", 0)
                fn = tc.get("function") or {}
                if oai_idx not in self.tool_items:
                    out += self._open_tool(oai_idx, tc.get("id") or "", fn.get("name") or "")
                args = fn.get("arguments")
                if args:
                    cur = self.tool_items[oai_idx]
                    cur["item"]["arguments"] += args
                    out.append(self._ev("response.function_call_arguments.delta", {
                        "item_id": cur["item"]["id"], "output_index": cur["index"], "delta": args,
                    }))
        return out

    def close(self) -> List[str]:
        if self.closed:
            return []
        self.closed = True
        out = self._ensure_started()
        if self.current and self.current["kind"] == "tool":
            cur = self.current
            cur["item"]["status"] = "completed"
            out.append(self._ev("response.function_call_arguments.done", {
                "item_id": cur["item"]["id"], "output_index": cur["index"],
                "arguments": cur["item"]["arguments"],
            }))
            out.append(self._ev("response.output_item.done",
                                {"output_index": cur["index"], "item": cur["item"]}))
            self.output.append(cur["item"])
            self.current = None
        else:
            out += self._close_current()
        if not self.output:
            item = {
                "type": "message", "id": new_id("msg"), "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "", "annotations": []}],
            }
            idx = self.next_output_index
            self.next_output_index += 1
            out.append(self._ev("response.output_item.added", {"output_index": idx, "item": item}))
            out.append(self._ev("response.content_part.added", {
                "item_id": item["id"], "output_index": idx, "content_index": 0,
                "part": {"type": "output_text", "text": "", "annotations": []}}))
            out.append(self._ev("response.output_text.done", {
                "item_id": item["id"], "output_index": idx, "content_index": 0, "text": ""}))
            out.append(self._ev("response.content_part.done", {
                "item_id": item["id"], "output_index": idx, "content_index": 0,
                "part": {"type": "output_text", "text": "", "annotations": []}}))
            out.append(self._ev("response.output_item.done", {"output_index": idx, "item": item}))
            self.output.append(item)

        status = "incomplete" if self.finish_reason == "length" else "completed"
        snap = self._snapshot(status)
        if status == "incomplete":
            snap["incomplete_details"] = {"reason": "max_output_tokens"}
        snap["output_text"] = "".join(self.text_buf)
        out.append(self._ev("response.completed" if status == "completed" else "response.incomplete",
                            {"response": snap}))
        return out


def error_response(message: str, err_type: str = "api_error") -> Dict[str, Any]:
    return {"error": {"message": message, "type": err_type, "param": None, "code": None}}


def stream_error_frames(message: str, err_type: str = "api_error") -> List[str]:
    return [sse({"type": "error", "error": {"type": err_type, "message": message, "code": None}},
                "error")]
