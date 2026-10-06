"""OpenAI 旧版 Completions（/v1/completions）适配。

仅作为客户端格式使用：请求被转换为规范格式，响应再还原为 text_completion。
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from .utils import (BaseStreamParser, BaseStreamRenderer, make_chunk, new_id,
                    normalize_usage, sse, text_of)

NAME = "openai_completions"
LABEL = "OpenAI Completions (legacy)"
ENDPOINT = "completions"


def _prompt_to_text(prompt: Any) -> str:
    if prompt is None:
        return ""
    if isinstance(prompt, str):
        return prompt
    if isinstance(prompt, list):
        if all(isinstance(p, str) for p in prompt):
            return "\n".join(prompt)
        parts = []
        for tok in prompt:
            if isinstance(tok, list) and all(isinstance(t, int) for t in tok):
                parts.append("".join(chr(t) for t in tok))
            else:
                parts.append(str(tok))
        return "".join(parts)
    return str(prompt)


def request_to_canonical(body: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    if body.get("model"):
        out["model"] = body["model"]
    messages: List[Dict[str, Any]] = []
    if body.get("messages"):
        messages = list(body["messages"])
    else:
        messages = [{"role": "user", "content": _prompt_to_text(body.get("prompt"))}]
    out["messages"] = messages
    for key in ("temperature", "top_p", "max_tokens", "stream", "user", "n", "seed"):
        if body.get(key) is not None:
            out[key] = body[key]
    if body.get("stop"):
        out["stop"] = body["stop"]
    if body.get("logprobs") is not None:
        out["logprobs"] = body["logprobs"]
    return out


def canonical_to_request(body: Dict[str, Any]) -> Dict[str, Any]:
    return dict(body)


def response_to_canonical(body: Dict[str, Any]) -> Dict[str, Any]:
    return body


def canonical_to_response(body: Dict[str, Any],
                          ctx: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    choice = (body.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    text = text_of(message.get("content"))
    for tc in message.get("tool_calls") or []:
        fn = tc.get("function") or {}
        text += f"\n<tool_call>{fn.get('name')}({fn.get('arguments')})</tool_call>"
    return {
        "id": body.get("id") or new_id("cmpl"),
        "object": "text_completion",
        "created": int(body.get("created") or time.time()),
        "model": body.get("model"),
        "choices": [{
            "text": text,
            "index": 0,
            "logprobs": None,
            "finish_reason": choice.get("finish_reason") or "stop",
        }],
        "usage": normalize_usage(body.get("usage")),
    }


class StreamParser(BaseStreamParser):
    """旧版格式不会作为上游，占位实现。"""

    def feed(self, data: Dict[str, Any]) -> List[Dict[str, Any]]:
        return [data] if isinstance(data, dict) else []


class StreamRenderer(BaseStreamRenderer):
    def __init__(self, meta: Optional[Dict[str, Any]] = None) -> None:
        meta = meta or {}
        self.cid = meta.get("id") or new_id("cmpl")
        self.model = meta.get("model")
        self.created = int(meta.get("created") or time.time())
        self.finished = False
        self.usage: Optional[Dict[str, Any]] = None

    def push(self, chunk: Dict[str, Any]) -> List[str]:
        out: List[str] = []
        if chunk.get("usage"):
            self.usage = chunk["usage"]
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            text = delta.get("content") or ""
            if choice.get("finish_reason"):
                self.finished = True
            if not text and not choice.get("finish_reason"):
                continue
            payload = {
                "id": self.cid,
                "object": "text_completion",
                "created": self.created,
                "model": self.model,
                "choices": [{
                    "text": text,
                    "index": 0,
                    "logprobs": None,
                    "finish_reason": choice.get("finish_reason"),
                }],
            }
            out.append(sse(payload))
        return out

    def close(self) -> List[str]:
        out: List[str] = []
        if not self.finished:
            out.append(sse({
                "id": self.cid, "object": "text_completion", "created": self.created,
                "model": self.model,
                "choices": [{"text": "", "index": 0, "logprobs": None, "finish_reason": "stop"}],
            }))
        out.append("data: [DONE]\n\n")
        return out


def error_response(message: str, err_type: str = "api_error") -> Dict[str, Any]:
    return {"error": {"message": message, "type": err_type, "param": None, "code": None}}


def stream_error_frames(message: str, err_type: str = "api_error") -> List[str]:
    return [sse(error_response(message, err_type)), "data: [DONE]\n\n"]
