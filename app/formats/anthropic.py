"""Anthropic Messages API 格式 <-> 内部规范（OpenAI Chat Completions）。"""
from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional

from .utils import (ANTHROPIC_MIN_BUDGET, BaseStreamParser, BaseStreamRenderer,
                    budget_to_effort, effort_to_budget, jd, make_chunk, new_id,
                    normalize_effort, normalize_usage, safe_json, sse, StreamError, text_of)

NAME = "anthropic"
LABEL = "Anthropic Messages"
ENDPOINT = "messages"
DEFAULT_MAX_TOKENS = 4096

STOP_REASON_TO_FINISH = {
    "end_turn": "stop",
    "max_tokens": "length",
    "stop_sequence": "stop",
    "tool_use": "tool_calls",
    "pause_turn": "stop",
    "refusal": "content_filter",
}
FINISH_TO_STOP_REASON = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "content_filter": "refusal",
    "function_call": "tool_use",
}


# --------------------------------------------------------------------------- #
# 请求：Anthropic -> 规范
# --------------------------------------------------------------------------- #
def _anthropic_content_to_openai(role: str, content: Any,
                                 out: List[Dict[str, Any]]) -> None:
    if isinstance(content, str):
        out.append({"role": role, "content": content})
        return
    if not isinstance(content, list):
        out.append({"role": role, "content": "" if content is None else str(content)})
        return

    if role == "assistant":
        texts: List[str] = []
        tool_calls: List[Dict[str, Any]] = []
        reasoning: List[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                texts.append(block.get("text") or "")
            elif btype == "thinking":
                reasoning.append(block.get("thinking") or "")
            elif btype == "tool_use":
                tool_calls.append({
                    "id": block.get("id") or new_id("call"),
                    "type": "function",
                    "function": {
                        "name": block.get("name") or "",
                        "arguments": jd(block.get("input") if block.get("input") is not None else {}),
                    },
                })
        msg: Dict[str, Any] = {"role": "assistant", "content": "".join(texts) or None}
        if reasoning:
            msg["reasoning_content"] = "".join(reasoning)
        if tool_calls:
            msg["tool_calls"] = tool_calls
        out.append(msg)
        return

    # user（或 system）消息，可能包含 text / image / tool_result
    parts: List[Dict[str, Any]] = []
    tool_messages: List[Dict[str, Any]] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            parts.append({"type": "text", "text": block.get("text") or ""})
        elif btype == "image":
            src = block.get("source") or {}
            if src.get("type") == "base64":
                url = f"data:{src.get('media_type', 'image/png')};base64,{src.get('data', '')}"
            else:
                url = src.get("url") or ""
            parts.append({"type": "image_url", "image_url": {"url": url}})
        elif btype == "tool_result":
            tool_messages.append({
                "role": "tool",
                "tool_call_id": block.get("tool_use_id") or "",
                "content": _tool_result_text(block.get("content")),
            })
    if parts:
        if all(p["type"] == "text" for p in parts):
            out.append({"role": role, "content": "".join(p["text"] for p in parts)})
        else:
            out.append({"role": role, "content": parts})
    out.extend(tool_messages)


def _tool_result_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks = []
        for b in content:
            if isinstance(b, str):
                chunks.append(b)
            elif isinstance(b, dict):
                if b.get("type") == "text":
                    chunks.append(b.get("text") or "")
                elif b.get("type") == "image":
                    chunks.append("[image]")
                else:
                    chunks.append(jd(b))
        return "".join(chunks)
    return jd(content)


def request_to_canonical(body: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    if body.get("model"):
        out["model"] = body["model"]

    messages: List[Dict[str, Any]] = []
    system = body.get("system")
    if system:
        messages.append({"role": "system", "content": text_of(system)})
    for m in body.get("messages") or []:
        if not isinstance(m, dict):
            continue
        role = m.get("role") or "user"
        content = m.get("content")
        if isinstance(content, str) or content is None:
            messages.append({"role": role, "content": content or ""})
        else:
            _anthropic_content_to_openai(role, content, messages)
    out["messages"] = messages

    if body.get("max_tokens") is not None:
        out["max_tokens"] = body["max_tokens"]
    for key in ("temperature", "top_p", "stream"):
        if body.get(key) is not None:
            out[key] = body[key]
    if body.get("stop_sequences"):
        out["stop"] = body["stop_sequences"]
    if body.get("metadata", {}).get("user_id"):
        out["user"] = body["metadata"]["user_id"]

    tools = []
    for t in body.get("tools") or []:
        if not isinstance(t, dict):
            continue
        if not t.get("input_schema"):
            continue  # 跳过服务端内置工具（web_search 等）
        tools.append({
            "type": "function",
            "function": {
                "name": t.get("name") or "",
                "description": t.get("description") or "",
                "parameters": t["input_schema"],
            },
        })
    if tools:
        out["tools"] = tools
        tc = body.get("tool_choice") or {}
        if isinstance(tc, dict):
            ttype = tc.get("type")
            if ttype == "auto":
                out["tool_choice"] = "auto"
            elif ttype == "any":
                out["tool_choice"] = "required"
            elif ttype == "none":
                out["tool_choice"] = "none"
            elif ttype == "tool":
                out["tool_choice"] = {"type": "function", "function": {"name": tc.get("name")}}
    if body.get("thinking", {}).get("type") == "enabled":
        # Anthropic 的思考等级 -> OpenAI 的 reasoning_effort
        out["reasoning_effort"] = budget_to_effort(
            (body.get("thinking") or {}).get("budget_tokens")) or "medium"
    return out


# --------------------------------------------------------------------------- #
# 请求：规范 -> Anthropic
# --------------------------------------------------------------------------- #
def canonical_to_request(body: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    if body.get("model"):
        out["model"] = body["model"]
    out["max_tokens"] = int(body.get("max_tokens") or body.get("max_completion_tokens")
                            or DEFAULT_MAX_TOKENS)
    for key in ("temperature", "top_p", "stream"):
        if body.get(key) is not None:
            out[key] = body[key]
    stop = body.get("stop")
    if stop:
        out["stop_sequences"] = stop if isinstance(stop, list) else [stop]

    system_parts: List[str] = []
    messages: List[Dict[str, Any]] = []
    pending_tool_results: List[Dict[str, Any]] = []

    def flush_tool_results(merge_into: Optional[Dict[str, Any]] = None) -> None:
        if not pending_tool_results:
            return
        if merge_into is not None and merge_into.get("role") == "user":
            blocks = pending_tool_results + list(merge_into.get("content") or [])
            merge_into["content"] = blocks
        else:
            messages.append({"role": "user", "content": list(pending_tool_results)})
        pending_tool_results.clear()

    for m in body.get("messages") or []:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        if role == "system":
            system_parts.append(text_of(m.get("content")))
            continue
        if role == "tool":
            pending_tool_results.append({
                "type": "tool_result",
                "tool_use_id": m.get("tool_call_id") or "",
                "content": m.get("content") if isinstance(m.get("content"), str)
                else jd(m.get("content")),
            })
            continue

        if role == "assistant":
            flush_tool_results()
            blocks: List[Dict[str, Any]] = []
            text = text_of(m.get("content"))
            if text:
                blocks.append({"type": "text", "text": text})
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function") or {}
                raw = fn.get("arguments")
                if isinstance(raw, str):
                    parsed = safe_json(raw, None)
                    if parsed is None:
                        parsed = {"_raw": raw}
                else:
                    parsed = raw if raw is not None else {}
                blocks.append({
                    "type": "tool_use",
                    "id": tc.get("id") or new_id("toolu"),
                    "name": fn.get("name") or "",
                    "input": parsed,
                })
            if not blocks:
                blocks.append({"type": "text", "text": ""})
            messages.append({"role": "assistant", "content": blocks})
            continue

        # user
        content = m.get("content")
        blocks = []
        if isinstance(content, str):
            if content:
                blocks.append({"type": "text", "text": content})
        elif isinstance(content, list):
            for p in content:
                if not isinstance(p, dict):
                    continue
                ptype = p.get("type")
                if ptype == "text":
                    blocks.append({"type": "text", "text": p.get("text") or ""})
                elif ptype == "image_url":
                    url = p.get("image_url")
                    if isinstance(url, dict):
                        url = url.get("url") or ""
                    if isinstance(url, str) and url.startswith("data:"):
                        header, _, b64 = url.partition(",")
                        media = header[5:].split(";")[0] or "image/png"
                        blocks.append({"type": "image", "source": {
                            "type": "base64", "media_type": media, "data": b64}})
                    elif url:
                        blocks.append({"type": "image", "source": {"type": "url", "url": url}})
        msg = {"role": "user", "content": blocks}
        if pending_tool_results:
            flush_tool_results(merge_into=msg)
        messages.append(msg)

    flush_tool_results()

    if system_parts:
        out["system"] = "\n\n".join(s for s in system_parts if s)
    if not messages:
        messages = [{"role": "user", "content": [{"type": "text", "text": ""}]}]
    # Anthropic 要求首条消息必须是 user
    if messages[0]["role"] != "user":
        messages.insert(0, {"role": "user", "content": [{"type": "text", "text": ""}]})
    out["messages"] = messages

    tools = []
    for t in body.get("tools") or []:
        if not isinstance(t, dict):
            continue
        if t.get("type") == "function" and isinstance(t.get("function"), dict):
            fn = t["function"]
        elif t.get("name"):
            fn = t
        else:
            continue
        tools.append({
            "name": fn.get("name") or "",
            "description": fn.get("description") or "",
            "input_schema": fn.get("parameters") or {"type": "object", "properties": {}},
        })
    if tools:
        out["tools"] = tools
        tc = body.get("tool_choice")
        if tc == "auto":
            out["tool_choice"] = {"type": "auto"}
        elif tc == "required":
            out["tool_choice"] = {"type": "any"}
        elif tc == "none":
            out["tool_choice"] = {"type": "none"}
        elif isinstance(tc, dict):
            if tc.get("type") == "function":
                name = (tc.get("function") or {}).get("name")
                out["tool_choice"] = {"type": "tool", "name": name}

    rf = body.get("response_format")
    if isinstance(rf, dict) and rf.get("type") == "json_object":
        # Anthropic 无原生 json 模式，用提示词兜底
        out.setdefault("system", "")
        out["system"] = (out.get("system") + "\n\n请只输出合法 JSON，不要包含额外说明。").strip()

    # reasoning_effort -> Anthropic thinking
    effort = normalize_effort(body.get("reasoning_effort"))
    if effort:
        budget = effort_to_budget(effort)
        explicit_max = body.get("max_tokens") or body.get("max_completion_tokens")
        max_tokens = int(out.get("max_tokens") or DEFAULT_MAX_TOKENS)
        if not explicit_max:
            # 客户端没有指定输出上限时，为思考腾出空间，避免高档位被削平
            max_tokens = max(max_tokens, budget + 1024)
            out["max_tokens"] = max_tokens
        # Anthropic 约束：1024 <= budget_tokens < max_tokens
        if max_tokens > ANTHROPIC_MIN_BUDGET + 256:
            budget = max(ANTHROPIC_MIN_BUDGET, min(budget, max_tokens - 256))
            out["thinking"] = {"type": "enabled", "budget_tokens": budget}
            # 开启 thinking 时 Anthropic 不允许自定义 temperature / top_p
            out.pop("temperature", None)
            out.pop("top_p", None)
    return out


# --------------------------------------------------------------------------- #
# 响应：Anthropic -> 规范
# --------------------------------------------------------------------------- #
def response_to_canonical(body: Dict[str, Any]) -> Dict[str, Any]:
    content = body.get("content") or []
    texts: List[str] = []
    reasoning: List[str] = []
    tool_calls: List[Dict[str, Any]] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            texts.append(block.get("text") or "")
        elif btype == "thinking":
            reasoning.append(block.get("thinking") or "")
        elif btype == "tool_use":
            tool_calls.append({
                "id": block.get("id") or new_id("call"),
                "type": "function",
                "function": {
                    "name": block.get("name") or "",
                    "arguments": jd(block.get("input") if block.get("input") is not None else {}),
                },
            })
    message: Dict[str, Any] = {"role": "assistant", "content": "".join(texts) or None}
    if reasoning:
        message["reasoning_content"] = "".join(reasoning)
    if tool_calls:
        message["tool_calls"] = tool_calls
    usage = normalize_usage(body.get("usage"))
    finish = STOP_REASON_TO_FINISH.get(body.get("stop_reason") or "end_turn", "stop")
    return {
        "id": body.get("id") or new_id(),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": body.get("model"),
        "choices": [{"index": 0, "message": message, "finish_reason": finish, "logprobs": None}],
        "usage": usage,
    }


# --------------------------------------------------------------------------- #
# 响应：规范 -> Anthropic
# --------------------------------------------------------------------------- #
def canonical_to_response(body: Dict[str, Any], ctx: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    choice = (body.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    blocks: List[Dict[str, Any]] = []
    # 仅当客户端本次请求开启了 thinking 才回吐思考块，
    # 否则会改变 content 结构、让未申请思考的客户端拿到意料之外的内容块。
    reasoning = message.get("reasoning_content")
    if reasoning and (ctx or {}).get("reasoning"):
        blocks.append({"type": "thinking", "thinking": reasoning, "signature": ""})
    text = text_of(message.get("content"))
    if text:
        blocks.append({"type": "text", "text": text})
    for tc in message.get("tool_calls") or []:
        fn = tc.get("function") or {}
        parsed = safe_json(fn.get("arguments"), None)
        if parsed is None:
            parsed = {"_raw": fn.get("arguments")} if fn.get("arguments") else {}
        blocks.append({
            "type": "tool_use",
            "id": tc.get("id") or new_id("toolu"),
            "name": fn.get("name") or "",
            "input": parsed,
        })
    if not blocks:
        blocks.append({"type": "text", "text": ""})
    usage = normalize_usage(body.get("usage"))
    return {
        "id": body.get("id") or new_id("msg"),
        "type": "message",
        "role": "assistant",
        "model": body.get("model"),
        "content": blocks,
        "stop_reason": FINISH_TO_STOP_REASON.get(choice.get("finish_reason") or "stop", "end_turn"),
        "stop_sequence": None,
        "usage": {"input_tokens": usage["prompt_tokens"], "output_tokens": usage["completion_tokens"]},
    }


# --------------------------------------------------------------------------- #
# 流式：解析
# --------------------------------------------------------------------------- #
class StreamParser(BaseStreamParser):
    def __init__(self) -> None:
        self.cid: Optional[str] = None
        self.model: Optional[str] = None
        self.created = int(time.time())
        self.input_tokens = 0
        self.output_tokens = 0
        self.finish_reason: Optional[str] = None
        self._tool_index: Dict[int, int] = {}
        self._tool_counter = 0
        self._stopped = False

    def feed(self, data: Dict[str, Any]) -> List[Dict[str, Any]]:
        if not isinstance(data, dict):
            return []
        etype = data.get("type")
        out: List[Dict[str, Any]] = []

        if etype == "message_start":
            msg = data.get("message") or {}
            self.cid = msg.get("id") or self.cid
            self.model = msg.get("model") or self.model
            usage = msg.get("usage") or {}
            self.input_tokens = usage.get("input_tokens", 0) or 0
            self.output_tokens = usage.get("output_tokens", 0) or 0
            out.append(make_chunk(self.cid, self.created, self.model,
                                  {"role": "assistant", "content": ""}))
        elif etype == "content_block_start":
            block = data.get("content_block") or {}
            if block.get("type") == "tool_use":
                idx = self._tool_counter
                self._tool_counter += 1
                self._tool_index[data.get("index", 0)] = idx
                out.append(make_chunk(self.cid, self.created, self.model, {
                    "tool_calls": [{
                        "index": idx,
                        "id": block.get("id") or new_id("call"),
                        "type": "function",
                        "function": {"name": block.get("name") or "", "arguments": ""},
                    }]
                }))
        elif etype == "content_block_delta":
            delta = data.get("delta") or {}
            dtype = delta.get("type")
            if dtype == "text_delta":
                out.append(make_chunk(self.cid, self.created, self.model,
                                      {"content": delta.get("text") or ""}))
            elif dtype == "thinking_delta":
                out.append(make_chunk(self.cid, self.created, self.model,
                                      {"reasoning_content": delta.get("thinking") or ""}))
            elif dtype == "input_json_delta":
                oai_idx = self._tool_index.get(data.get("index", 0))
                if oai_idx is not None:
                    out.append(make_chunk(self.cid, self.created, self.model, {
                        "tool_calls": [{
                            "index": oai_idx,
                            "function": {"arguments": delta.get("partial_json") or ""},
                        }]
                    }))
        elif etype == "message_delta":
            delta = data.get("delta") or {}
            usage = data.get("usage") or {}
            if usage.get("output_tokens"):
                self.output_tokens = usage["output_tokens"]
            if delta.get("stop_reason"):
                self.finish_reason = STOP_REASON_TO_FINISH.get(delta["stop_reason"], "stop")
        elif etype == "message_stop":
            self._stopped = True
            out.append(make_chunk(
                self.cid, self.created, self.model, {}, finish_reason=self.finish_reason or "stop",
                usage={"prompt_tokens": self.input_tokens, "completion_tokens": self.output_tokens,
                       "total_tokens": self.input_tokens + self.output_tokens}))
        elif etype == "error":
            err = data.get("error") or {}
            raise StreamError(err.get("message") or "上游返回错误")
        return out

    def finalize(self) -> List[Dict[str, Any]]:
        if self._stopped:
            return []
        self._stopped = True
        return [make_chunk(
            self.cid, self.created, self.model, {},
            finish_reason=self.finish_reason or "stop",
            usage={"prompt_tokens": self.input_tokens, "completion_tokens": self.output_tokens,
                   "total_tokens": self.input_tokens + self.output_tokens})]


# --------------------------------------------------------------------------- #
# 流式：渲染
# --------------------------------------------------------------------------- #
class StreamRenderer(BaseStreamRenderer):
    def __init__(self, meta: Optional[Dict[str, Any]] = None) -> None:
        meta = meta or {}
        self.meta = meta
        self.cid = meta.get("id") or new_id("msg")
        self.model = meta.get("model")
        self.started = False
        self.closed = False
        self.next_index = 0
        self.open_block: Optional[Dict[str, Any]] = None
        self.tool_blocks: Dict[int, int] = {}
        self.finish_reason: Optional[str] = None
        self.input_tokens = 0
        self.output_tokens = 0

    # -- 内部 -------------------------------------------------------------- #
    def _emit_start(self) -> List[str]:
        if self.started:
            return []
        self.started = True
        msg = {
            "id": self.cid, "type": "message", "role": "assistant", "model": self.model,
            "content": [], "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": self.input_tokens, "output_tokens": 0},
        }
        return [sse({"type": "message_start", "message": msg}, "message_start")]

    def _close_block(self) -> List[str]:
        if not self.open_block:
            return []
        idx = self.open_block["index"]
        self.open_block = None
        return [sse({"type": "content_block_stop", "index": idx}, "content_block_stop")]

    def _open_block(self, block_type: str, extra: Optional[Dict[str, Any]] = None) -> List[str]:
        out = self._close_block()
        idx = self.next_index
        self.next_index += 1
        block: Dict[str, Any] = {"type": block_type}
        if extra:
            block.update(extra)
        self.open_block = {"index": idx, "type": block_type}
        out.append(sse({"type": "content_block_start", "index": idx, "content_block": block},
                       "content_block_start"))
        return out

    @staticmethod
    def _delta(index: int, delta: Dict[str, Any]) -> str:
        return sse({"type": "content_block_delta", "index": index, "delta": delta},
                   "content_block_delta")

    # -- 接口 -------------------------------------------------------------- #
    def push(self, chunk: Dict[str, Any]) -> List[str]:
        out: List[str] = []
        if not self.started:
            usage = chunk.get("usage") or {}
            if usage:
                self.input_tokens = usage.get("prompt_tokens", 0) or 0
                self.output_tokens = usage.get("completion_tokens", 0) or 0
            out += self._emit_start()
        if chunk.get("id"):
            self.cid = chunk["id"]
        if chunk.get("model"):
            self.model = chunk["model"]
        usage = chunk.get("usage") or {}
        if usage:
            self.input_tokens = usage.get("prompt_tokens", self.input_tokens) or self.input_tokens
            self.output_tokens = (usage.get("completion_tokens") or self.output_tokens)

        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            if choice.get("finish_reason"):
                self.finish_reason = choice["finish_reason"]

            reasoning = delta.get("reasoning_content")
            if reasoning and self.meta.get("reasoning"):
                if not self.open_block or self.open_block["type"] != "thinking":
                    out += self._open_block("thinking", {"thinking": ""})
                out.append(self._delta(self.open_block["index"],
                                       {"type": "thinking_delta", "thinking": reasoning}))

            text = delta.get("content")
            if text:
                if not self.open_block or self.open_block["type"] != "text":
                    out += self._open_block("text", {"text": ""})
                out.append(self._delta(self.open_block["index"],
                                       {"type": "text_delta", "text": text}))

            for tc in delta.get("tool_calls") or []:
                oai_idx = tc.get("index", 0)
                if oai_idx not in self.tool_blocks:
                    out += self._open_block("tool_use", {
                        "id": tc.get("id") or new_id("toolu"),
                        "name": (tc.get("function") or {}).get("name") or "",
                        "input": {},
                    })
                    self.tool_blocks[oai_idx] = self.open_block["index"]
                args = (tc.get("function") or {}).get("arguments")
                if args:
                    out.append(self._delta(self.tool_blocks[oai_idx],
                                           {"type": "input_json_delta", "partial_json": args}))
        return out

    def close(self) -> List[str]:
        if self.closed:
            return []
        self.closed = True
        out: List[str] = []
        if not self.started:
            out += self._emit_start()
        out += self._close_block()
        stop_reason = FINISH_TO_STOP_REASON.get(self.finish_reason or "stop", "end_turn")
        out.append(sse({
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason, "stop_sequence": None},
            "usage": {"output_tokens": self.output_tokens},
        }, "message_delta"))
        out.append(sse({"type": "message_stop"}, "message_stop"))
        return out


def error_response(message: str, err_type: str = "api_error") -> Dict[str, Any]:
    return {"type": "error", "error": {"type": err_type, "message": message}}


def stream_error_frames(message: str, err_type: str = "api_error") -> List[str]:
    return [sse({"type": "error", "error": {"type": err_type, "message": message}}, "error")]
