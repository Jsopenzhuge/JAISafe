"""OpenAI Chat Completions 格式。作为内部规范格式，转换基本为恒等映射。"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from .utils import (BaseStreamParser, BaseStreamRenderer, jd, make_chunk, new_id,
                    normalize_usage, sse)

NAME = "openai"
LABEL = "OpenAI Chat Completions"
ENDPOINT = "chat/completions"


# --------------------------------------------------------------------------- #
# 非流式
# --------------------------------------------------------------------------- #
def request_to_canonical(body: Dict[str, Any]) -> Dict[str, Any]:
    return dict(body)


def canonical_to_request(body: Dict[str, Any]) -> Dict[str, Any]:
    return dict(body)


def response_to_canonical(body: Dict[str, Any]) -> Dict[str, Any]:
    return body


def canonical_to_response(body: Dict[str, Any],
                          ctx: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    # 规范格式本身就是 OpenAI Chat，恒等返回；reasoning_content 随 message 一并透出
    return body


# --------------------------------------------------------------------------- #
# 流式
# --------------------------------------------------------------------------- #
class StreamParser(BaseStreamParser):
    """OpenAI 分片本身即规范分片。"""

    def __init__(self) -> None:
        self.chunks: List[Dict[str, Any]] = []

    def feed(self, data: Dict[str, Any]) -> List[Dict[str, Any]]:
        if not isinstance(data, dict):
            return []
        if data.get("error"):
            from .utils import StreamError
            err = data["error"]
            raise StreamError(err.get("message") if isinstance(err, dict) else str(err))
        self.chunks.append(data)
        return [data]


class StreamRenderer(BaseStreamRenderer):
    def __init__(self, meta: Optional[Dict[str, Any]] = None) -> None:
        self.meta = meta or {}
        self.cid = self.meta.get("id") or new_id()
        self.model = self.meta.get("model")
        self.created = self.meta.get("created") or int(time.time())
        self.sent_role = False
        self.finished = False
        self.done = False
        self._pending_usage: Optional[Dict[str, Any]] = None

    def _fix(self, chunk: Dict[str, Any]) -> Dict[str, Any]:
        chunk = dict(chunk)
        chunk.setdefault("id", self.cid)
        chunk.setdefault("object", "chat.completion.chunk")
        chunk.setdefault("created", self.created)
        if not chunk.get("model"):
            chunk["model"] = self.model
        return chunk

    def push(self, chunk: Dict[str, Any]) -> List[str]:
        out: List[str] = []
        chunk = self._fix(chunk)
        if not self.meta.get("include_usage"):
            if "usage" in chunk:
                chunk.pop("usage", None)
                if not chunk.get("choices"):
                    return []
        if not self.sent_role:
            for choice in chunk.get("choices") or []:
                delta = choice.setdefault("delta", {})
                if "role" not in delta and not delta.get("content") and not delta.get("tool_calls"):
                    delta["role"] = "assistant"
                elif "role" not in delta:
                    delta["role"] = "assistant"
            self.sent_role = True
        for choice in chunk.get("choices") or []:
            if choice.get("finish_reason"):
                self.finished = True
        out.append(sse(chunk))
        return out

    def close(self) -> List[str]:
        out: List[str] = []
        if not self.finished:
            out.append(sse(make_chunk(self.cid, self.created, self.model,
                                      {}, finish_reason="stop")))
        if not self.done:
            out.append("data: [DONE]\n\n")
            self.done = True
        return out


def error_response(message: str, err_type: str = "api_error") -> Dict[str, Any]:
    return {"error": {"message": message, "type": err_type, "param": None, "code": None}}


def stream_error_frames(message: str, err_type: str = "api_error") -> List[str]:
    return [sse(error_response(message, err_type)), "data: [DONE]\n\n"]
