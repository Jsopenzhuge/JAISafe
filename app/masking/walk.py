"""按协议遍历请求/响应中的文本字段，做脱敏与还原。

请求侧在「客户端原始 body」上原地操作（保证同格式直通的字节保真度），
响应侧在「已完成格式转换、即将返回给客户端」的 body 上操作。
流式走 canonical 分片层，见 stream.py。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .engine import MaskEngine, mask_json_field, unmask_json_field
from .slots import MaskHit

_FORMATS = ("openai", "anthropic", "openai_responses", "openai_completions")


# --------------------------------------------------------------------------- #
# 内部工具
# --------------------------------------------------------------------------- #
def _mask_str(value: Any, engine: MaskEngine, hits: List[MaskHit]) -> Any:
    if isinstance(value, str):
        return engine._mask_collect(value, hits)
    return value


def _unmask_str(value: Any, engine: MaskEngine) -> Any:
    if isinstance(value, str):
        return engine.unmask_text(value)
    return value


def _mask_block_list(blocks: Any, engine: MaskEngine, hits: List[MaskHit],
                     text_keys: Dict[str, str]) -> Any:
    """处理 content 块数组。text_keys 形如 {"text": "text", "thinking": "thinking"}。"""
    if not isinstance(blocks, list):
        return blocks
    for block in blocks:
        if not isinstance(block, dict):
            continue
        for out_key in text_keys:
            if isinstance(block.get(out_key), str):
                block[out_key] = engine._mask_collect(block[out_key], hits)
    return blocks


def _unmask_block_list(blocks: Any, engine: MaskEngine, text_keys: List[str]) -> Any:
    if not isinstance(blocks, list):
        return blocks
    for block in blocks:
        if not isinstance(block, dict):
            continue
        for key in text_keys:
            if isinstance(block.get(key), str):
                block[key] = engine.unmask_text(block[key])
    return blocks


def _mask_content(content: Any, engine: MaskEngine, hits: List[MaskHit]) -> Any:
    """OpenAI 风格 content：字符串或 parts 数组。"""
    if isinstance(content, str):
        return engine._mask_collect(content, hits)
    if isinstance(content, list):
        return _mask_block_list(content, engine, hits, {"text": "text"})
    return content


def _unmask_content(content: Any, engine: MaskEngine) -> Any:
    if isinstance(content, str):
        return engine.unmask_text(content)
    if isinstance(content, list):
        return _unmask_block_list(content, engine, ["text"])
    return content


# --------------------------------------------------------------------------- #
# 请求侧
# --------------------------------------------------------------------------- #
def mask_body(body: Dict[str, Any], fmt: str, engine: MaskEngine) -> List[MaskHit]:
    """在 body 上原地脱敏，返回命中列表。body 必须是调用方独占的副本。"""
    hits: List[MaskHit] = []
    if not isinstance(body, dict):
        return hits
    if fmt == "anthropic":
        _mask_anthropic_request(body, engine, hits)
    elif fmt == "openai_responses":
        _mask_responses_request(body, engine, hits)
    elif fmt == "openai_completions":
        _mask_completions_request(body, engine, hits)
    else:
        _mask_openai_request(body, engine, hits)
    return hits


def _mask_tools(tools: Any, engine: MaskEngine, hits: List[MaskHit]) -> None:
    if not isinstance(tools, list):
        return
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        fn = tool.get("function") if isinstance(tool.get("function"), dict) else tool
        if isinstance(fn.get("description"), str):
            fn["description"] = engine._mask_collect(fn["description"], hits)
        if isinstance(fn.get("name"), str):
            # 工具名一般不含隐私；仅在确实命中占位符形态时才处理，避免误伤
            pass


def _unmask_tools(tools: Any, engine: MaskEngine) -> None:
    if not isinstance(tools, list):
        return
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        fn = tool.get("function") if isinstance(tool.get("function"), dict) else tool
        if isinstance(fn.get("description"), str):
            fn["description"] = engine.unmask_text(fn["description"])


def _mask_message_list(messages: Any, engine: MaskEngine, hits: List[MaskHit],
                       args_as_json: bool = True) -> None:
    if not isinstance(messages, list):
        return
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        if "content" in msg:
            msg["content"] = _mask_content(msg["content"], engine, hits)
        for tc in msg.get("tool_calls") or []:
            if not isinstance(tc, dict):
                continue
            fn = tc.get("function")
            if isinstance(fn, dict) and isinstance(fn.get("arguments"), str):
                fn["arguments"] = (mask_json_field(fn["arguments"], engine, hits)
                                   if args_as_json
                                   else engine._mask_collect(fn["arguments"], hits))
    return None


def _mask_openai_request(body: Dict[str, Any], engine: MaskEngine,
                         hits: List[MaskHit]) -> None:
    _mask_message_list(body.get("messages"), engine, hits)
    _mask_tools(body.get("tools"), engine, hits)
    if isinstance(body.get("system"), str):
        body["system"] = engine._mask_collect(body["system"], hits)


def _mask_completions_request(body: Dict[str, Any], engine: MaskEngine,
                              hits: List[MaskHit]) -> None:
    prompt = body.get("prompt")
    if isinstance(prompt, str):
        body["prompt"] = engine._mask_collect(prompt, hits)
    elif isinstance(prompt, list):
        body["prompt"] = [_mask_str(p, engine, hits) for p in prompt]
    _mask_message_list(body.get("messages"), engine, hits)


def _mask_anthropic_request(body: Dict[str, Any], engine: MaskEngine,
                            hits: List[MaskHit]) -> None:
    system = body.get("system")
    if isinstance(system, str):
        body["system"] = engine._mask_collect(system, hits)
    elif isinstance(system, list):
        _mask_block_list(system, engine, hits, {"text": "text"})

    for msg in body.get("messages") or []:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if isinstance(content, str):
            msg["content"] = engine._mask_collect(content, hits)
            continue
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text" and isinstance(block.get("text"), str):
                block["text"] = engine._mask_collect(block["text"], hits)
            elif btype == "thinking" and isinstance(block.get("thinking"), str):
                block["thinking"] = engine._mask_collect(block["thinking"], hits)
            elif btype == "tool_use" and isinstance(block.get("input"), (dict, list)):
                block["input"] = engine.mask_value(block["input"], hits)
            elif btype == "tool_result":
                inner = block.get("content")
                if isinstance(inner, str):
                    block["content"] = engine._mask_collect(inner, hits)
                elif isinstance(inner, list):
                    _mask_block_list(inner, engine, hits, {"text": "text"})
    _mask_tools(body.get("tools"), engine, hits)


def _mask_responses_request(body: Dict[str, Any], engine: MaskEngine,
                            hits: List[MaskHit]) -> None:
    if isinstance(body.get("instructions"), str):
        body["instructions"] = engine._mask_collect(body["instructions"], hits)
    raw_input = body.get("input")
    if isinstance(raw_input, str):
        body["input"] = engine._mask_collect(raw_input, hits)
    elif isinstance(raw_input, list):
        for item in raw_input:
            if isinstance(item, str):
                continue
            if not isinstance(item, dict):
                continue
            itype = item.get("type")
            if itype in (None, "message"):
                content = item.get("content")
                if isinstance(content, str):
                    item["content"] = engine._mask_collect(content, hits)
                elif isinstance(content, list):
                    _mask_block_list(content, engine, hits, {"text": "text"})
            elif itype == "function_call" and isinstance(item.get("arguments"), str):
                item["arguments"] = mask_json_field(item["arguments"], engine, hits)
            elif itype == "function_call_output":
                out = item.get("output")
                if isinstance(out, str):
                    item["output"] = engine._mask_collect(out, hits)
                else:
                    item["output"] = engine.mask_value(out, hits)
    _mask_tools(body.get("tools"), engine, hits)


# --------------------------------------------------------------------------- #
# 响应侧（还原）
# --------------------------------------------------------------------------- #
def unmask_body(body: Any, fmt: str, engine: MaskEngine) -> Any:
    """把即将返回给客户端的响应里的 handle 还原成真值（原地）。"""
    if not isinstance(body, dict):
        return body
    if fmt == "anthropic":
        _unmask_anthropic_response(body, engine)
    elif fmt == "openai_responses":
        _unmask_responses_response(body, engine)
    elif fmt == "openai_completions":
        _unmask_completions_response(body, engine)
    else:
        _unmask_openai_response(body, engine)
    return body


def _unmask_openai_response(body: Dict[str, Any], engine: MaskEngine) -> None:
    for choice in body.get("choices") or []:
        if not isinstance(choice, dict):
            continue
        for holder in (choice.get("message"), choice.get("delta")):
            if not isinstance(holder, dict):
                continue
            if "content" in holder:
                holder["content"] = _unmask_content(holder["content"], engine)
            if isinstance(holder.get("reasoning_content"), str):
                holder["reasoning_content"] = engine.unmask_text(holder["reasoning_content"])
            for tc in holder.get("tool_calls") or []:
                fn = tc.get("function") if isinstance(tc, dict) else None
                if isinstance(fn, dict) and isinstance(fn.get("arguments"), str):
                    fn["arguments"] = unmask_json_field(fn["arguments"], engine)
        if isinstance(choice.get("text"), str):
            choice["text"] = engine.unmask_text(choice["text"])


def _unmask_completions_response(body: Dict[str, Any], engine: MaskEngine) -> None:
    for choice in body.get("choices") or []:
        if isinstance(choice, dict) and isinstance(choice.get("text"), str):
            choice["text"] = engine.unmask_text(choice["text"])


def _unmask_anthropic_response(body: Dict[str, Any], engine: MaskEngine) -> None:
    for block in body.get("content") or []:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text" and isinstance(block.get("text"), str):
            block["text"] = engine.unmask_text(block["text"])
        elif btype == "thinking" and isinstance(block.get("thinking"), str):
            block["thinking"] = engine.unmask_text(block["thinking"])
        elif btype == "tool_use" and isinstance(block.get("input"), (dict, list)):
            block["input"] = engine.unmask_value(block["input"])


def _unmask_responses_response(body: Dict[str, Any], engine: MaskEngine) -> None:
    if isinstance(body.get("output_text"), str):
        body["output_text"] = engine.unmask_text(body["output_text"])
    if isinstance(body.get("instructions"), str):
        body["instructions"] = engine.unmask_text(body["instructions"])
    for item in body.get("output") or []:
        if not isinstance(item, dict):
            continue
        itype = item.get("type")
        if itype == "message":
            for part in item.get("content") or []:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    part["text"] = engine.unmask_text(part["text"])
        elif itype == "function_call" and isinstance(item.get("arguments"), str):
            item["arguments"] = unmask_json_field(item["arguments"], engine)
        elif itype == "reasoning":
            for part in item.get("summary") or []:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    part["text"] = engine.unmask_text(part["text"])


# --------------------------------------------------------------------------- #
# 汇总
# --------------------------------------------------------------------------- #
def summarize_hits(hits: List[MaskHit]) -> Dict[str, Any]:
    by_type: Dict[str, int] = {}
    for hit in hits:
        key = hit.slot_type or "UNKNOWN"
        by_type[key] = by_type.get(key, 0) + 1
    return {
        "count": len(hits),
        "paths": sum(1 for h in hits if h.kind == "path"),
        "credentials": sum(1 for h in hits if h.kind == "credential"),
        "by_type": by_type,
    }
