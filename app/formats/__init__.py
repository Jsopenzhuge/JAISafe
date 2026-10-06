"""格式注册表。

对外暴露统一的格式接口，内部规范格式为 OpenAI Chat Completions。

每个格式模块提供：
    NAME / LABEL / ENDPOINT
    request_to_canonical(body)  客户端格式 -> 规范
    canonical_to_request(body)  规范 -> 上游格式
    response_to_canonical(body) 上游格式 -> 规范
    canonical_to_response(body) 规范 -> 客户端格式
    StreamParser / StreamRenderer
"""
from __future__ import annotations

from typing import Any, Dict, List

from . import anthropic, legacy_completions, openai_chat, openai_responses
from .utils import StreamError, error_payload, status_to_err_type

FORMATS: Dict[str, Any] = {
    openai_chat.NAME: openai_chat,
    anthropic.NAME: anthropic,
    openai_responses.NAME: openai_responses,
    legacy_completions.NAME: legacy_completions,
}

# 可作为上游的格式（渠道类型）
UPSTREAM_FORMATS = ("openai", "anthropic", "openai_responses")

# 渠道 type -> 格式
CHANNEL_TYPES = [
    {"value": "openai", "label": "OpenAI 兼容 (Chat Completions)"},
    {"value": "anthropic", "label": "Anthropic Messages"},
    {"value": "openai_responses", "label": "OpenAI Responses API"},
]

# 请求路径 -> 客户端格式
PATH_FORMATS = {
    "/v1/chat/completions": "openai",
    "/v1/completions": "openai",
    "/v1/messages": "anthropic",
    "/v1/responses": "openai_responses",
}


def get_format(name: str) -> Any:
    mod = FORMATS.get(name)
    if mod is None:
        raise KeyError(f"未知格式: {name}")
    return mod


def error_body(fmt: str, message: str, err_type: str = "api_error") -> Dict[str, Any]:
    return error_payload(fmt, message, err_type)


__all__ = [
    "FORMATS", "CHANNEL_TYPES", "PATH_FORMATS", "UPSTREAM_FORMATS", "get_format",
    "error_body", "StreamError", "status_to_err_type",
]
