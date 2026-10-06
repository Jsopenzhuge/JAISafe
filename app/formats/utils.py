"""格式转换公共工具。"""
from __future__ import annotations

import json
import time
import uuid
from typing import Any, Dict, List, Optional

FINISH_STOP = "stop"
FINISH_LENGTH = "length"
FINISH_TOOL = "tool_calls"
FINISH_FILTER = "content_filter"


def new_id(prefix: str = "chatcmpl") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:24]}"


def jd(obj: Any) -> str:
    try:
        return json.dumps(obj, ensure_ascii=False)
    except Exception:
        return json.dumps(str(obj), ensure_ascii=False)


def safe_json(text: Any, default: Any = None) -> Any:
    if isinstance(text, (dict, list)):
        return text
    if text is None or text == "":
        return default
    try:
        return json.loads(text)
    except Exception:
        return default


def text_of(content: Any) -> str:
    """把 OpenAI 风格的 content（字符串或 parts 数组）拍平成文本。"""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for p in content:
            if isinstance(p, str):
                parts.append(p)
            elif isinstance(p, dict):
                if p.get("type") in ("text", "input_text", "output_text") or "text" in p:
                    parts.append(p.get("text") or "")
        return "".join(parts)
    return str(content)


def normalize_usage(usage: Optional[Dict[str, Any]]) -> Dict[str, int]:
    usage = usage or {}
    p = usage.get("prompt_tokens", usage.get("input_tokens", 0)) or 0
    c = usage.get("completion_tokens", usage.get("output_tokens", 0)) or 0
    t = usage.get("total_tokens") or (p + c)
    return {"prompt_tokens": int(p), "completion_tokens": int(c), "total_tokens": int(t)}


def make_chunk(cid: Optional[str], created: Optional[int], model: Optional[str],
               delta: Dict[str, Any], finish_reason: Optional[str] = None,
               usage: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    chunk: Dict[str, Any] = {
        "id": cid or new_id(),
        "object": "chat.completion.chunk",
        "created": created or int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "delta": delta,
            "finish_reason": finish_reason,
            "logprobs": None,
        }],
    }
    if usage is not None:
        chunk["usage"] = usage
    return chunk


def sse(payload: Any, event: Optional[str] = None) -> str:
    body = payload if isinstance(payload, str) else jd(payload)
    if event:
        return f"event: {event}\ndata: {body}\n\n"
    return f"data: {body}\n\n"


def assemble_chunks(chunks: List[Dict[str, Any]], model: Optional[str] = None,
                    cid: Optional[str] = None, created: Optional[int] = None) -> Dict[str, Any]:
    """把 OpenAI 流式分片拼装成一次完整的 chat.completion，用于日志与统计。"""
    content: List[str] = []
    reasoning: List[str] = []
    tools: Dict[int, Dict[str, Any]] = {}
    finish: Optional[str] = None
    usage: Optional[Dict[str, Any]] = None
    for ch in chunks:
        if isinstance(ch.get("usage"), dict) and ch["usage"]:
            usage = ch["usage"]
        for choice in ch.get("choices") or []:
            delta = choice.get("delta") or {}
            if delta.get("content"):
                content.append(delta["content"])
            if delta.get("reasoning_content"):
                reasoning.append(delta["reasoning_content"])
            for tc in delta.get("tool_calls") or []:
                idx = tc.get("index", 0)
                slot = tools.setdefault(idx, {
                    "id": None, "type": "function",
                    "function": {"name": "", "arguments": ""},
                })
                if tc.get("id"):
                    slot["id"] = tc["id"]
                fn = tc.get("function") or {}
                if fn.get("name"):
                    slot["function"]["name"] += fn["name"]
                if fn.get("arguments"):
                    slot["function"]["arguments"] += fn["arguments"]
            if choice.get("finish_reason"):
                finish = choice["finish_reason"]
    message: Dict[str, Any] = {"role": "assistant", "content": "".join(content) or None}
    if reasoning:
        message["reasoning_content"] = "".join(reasoning)
    if tools:
        out_tools = []
        for k in sorted(tools):
            slot = tools[k]
            if not slot.get("id"):
                slot["id"] = new_id("call")
            out_tools.append(slot)
        message["tool_calls"] = out_tools
    return {
        "id": cid or new_id(),
        "object": "chat.completion",
        "created": created or int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": message,
            "finish_reason": finish or FINISH_STOP,
            "logprobs": None,
        }],
        "usage": normalize_usage(usage),
    }


class StreamError(Exception):
    """上游流式返回的错误事件。"""


class BaseStreamParser:
    """把上游 SSE 数据帧解析为「规范分片」（OpenAI chat chunk）。"""

    def feed(self, data: Dict[str, Any]) -> List[Dict[str, Any]]:
        raise NotImplementedError

    def finalize(self) -> List[Dict[str, Any]]:
        return []


class BaseStreamRenderer:
    """把规范分片渲染成目标格式的 SSE 文本。"""

    #: 渲染上下文（model/id/include_usage/reasoning…），子类在 __init__ 中覆盖
    meta: Dict[str, Any] = {}

    def push(self, chunk: Dict[str, Any]) -> List[str]:
        raise NotImplementedError

    def close(self) -> List[str]:
        return []


def error_payload(fmt: str, message: str, err_type: str = "api_error") -> Dict[str, Any]:
    if fmt == "anthropic":
        return {"type": "error", "error": {"type": err_type, "message": message}}
    if fmt == "openai_responses":
        return {"error": {"message": message, "type": err_type, "param": None, "code": None}}
    return {"error": {"message": message, "type": err_type, "param": None, "code": None}}


def status_to_err_type(status: int) -> str:
    if status == 401:
        return "authentication_error"
    if status == 403:
        return "permission_error"
    if status == 404:
        return "not_found_error"
    if status == 429:
        return "rate_limit_error"
    if status >= 500:
        return "api_error"
    return "invalid_request_error"


# --------------------------------------------------------------------------- #
# 推理强度（thinking / reasoning_effort）映射
#
# 三种协议对「思考等级」的表达方式不同，内部统一用 OpenAI 的 reasoning_effort：
#   OpenAI Chat  : reasoning_effort = "minimal" | "low" | "medium" | "high"
#   Responses    : reasoning = {"effort": "low"|"medium"|"high", "summary": "auto"}
#   Anthropic    : thinking  = {"type": "enabled", "budget_tokens": N}
#
# Anthropic 的 budget_tokens 是连续量，与离散的 effort 之间是启发式映射：
#   minimal <= 1024 | low: 1025-2048 | medium: 2049-8192 | high: > 8192
# 反向下发时取该档位的代表值，因此跨格式往返可能出现档位内的数值漂移。
# --------------------------------------------------------------------------- #
EFFORT_LEVELS = ("minimal", "low", "medium", "high")

EFFORT_TO_BUDGET = {"minimal": 1024, "low": 2048, "medium": 8192, "high": 32768}

ANTHROPIC_MIN_BUDGET = 1024


def normalize_effort(value: Any) -> Optional[str]:
    """把任意来源的 effort 归一化为受支持的档位，非法值返回 None。"""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return budget_to_effort(int(value))
    text = str(value).strip().lower()
    if text in EFFORT_LEVELS:
        return text
    # 兼容少数厂商的写法
    alias = {"low_effort": "low", "mid": "medium", "default": "medium", "none": None,
             "off": None, "disabled": None, "high_effort": "high", "max": "high"}
    return alias.get(text, None)


def budget_to_effort(budget: Any) -> Optional[str]:
    """Anthropic budget_tokens -> reasoning_effort。"""
    try:
        value = int(budget)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    if value <= 1024:
        return "minimal"
    if value <= 2048:
        return "low"
    if value <= 8192:
        return "medium"
    return "high"


def effort_to_budget(effort: Any, default: int = EFFORT_TO_BUDGET["medium"]) -> int:
    """reasoning_effort -> Anthropic budget_tokens 代表值。"""
    level = normalize_effort(effort)
    if level is None:
        return default
    return EFFORT_TO_BUDGET.get(level, default)


def reasoning_context(fmt: str, body: Dict[str, Any]) -> Dict[str, Any]:
    """从**客户端原始请求**中提取「客户端是否要思考内容」，用于响应侧渲染。

    跨格式转换时上游一定会返回推理内容，但客户端没要就不该塞给它
    （Anthropic 的 thinking 是内容块，会改变 content 结构）。
    """
    ctx = {"reasoning": False, "summary": False, "effort": None}
    if not isinstance(body, dict):
        return ctx
    if fmt == "anthropic":
        thinking = body.get("thinking") or {}
        if isinstance(thinking, dict) and thinking.get("type") == "enabled":
            ctx["reasoning"] = True
            ctx["effort"] = budget_to_effort(thinking.get("budget_tokens"))
    elif fmt == "openai_responses":
        reasoning = body.get("reasoning") or {}
        if isinstance(reasoning, dict):
            effort = normalize_effort(reasoning.get("effort"))
            wants_summary = bool(reasoning.get("summary")) and reasoning.get("summary") != "none"
            if effort or wants_summary:
                ctx["reasoning"] = True
                ctx["summary"] = wants_summary
                ctx["effort"] = effort
    else:  # openai / openai_completions
        effort = normalize_effort(body.get("reasoning_effort"))
        if effort:
            ctx["reasoning"] = True
            ctx["effort"] = effort
    return ctx
