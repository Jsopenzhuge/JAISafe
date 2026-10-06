"""流式还原。

难点：handle 是普通字符串，会被 SSE 分片切开
（`[WORKSPACE_ROOT_1]/src` 可能分两片到达）。因此维护一个滑动缓冲：
只有当缓冲区里出现**完整** handle，且缓冲区尾部不可能是某个 handle 的
**前缀**时，才把字符下发给客户端。

另有一个容易踩的坑：模型流的 `tool_calls[].function.arguments` 是 JSON 文本
片段，把真值原样插回去会注入非法转义（Windows 路径的 `\\U`）。
所以参数通道用 `json_mode=True`，替换值先做 JSON 转义。
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from .engine import MaskEngine

_ESCAPE_MAP = {"\\": "\\\\", '"': '\\"'}


def _json_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


class RebindStream:
    """按字段维护的滑动窗口还原器。"""

    def __init__(self, engine: MaskEngine, json_mode: bool = False) -> None:
        self.engine = engine
        self.json_mode = json_mode
        self.buf = ""
        self._version = -1
        self._rx: Optional["re.Pattern[str]"] = None
        self._table: Dict[str, str] = {}
        self._prefixes: set = set()
        self._max_len = 0

    # -- 内部 -------------------------------------------------------------- #
    def _prepare(self) -> None:
        store = self.engine.store
        if store.version == self._version:
            return
        handles = store.all_handles()
        self._table = {h: store.by_handle.get(h, "") for h in handles}
        self._prefixes = store.prefixes()
        self._max_len = max((len(h) for h in handles), default=0)
        self._rx = (re.compile("|".join(re.escape(h) for h in handles))
                    if handles else None)
        self._version = store.version

    def _hold_len(self, tail: str) -> int:
        """tail 末尾有多少字符可能是某个 handle 的前缀，必须留着等后续分片。"""
        limit = min(len(tail), max(self._max_len - 1, 0))
        for k in range(limit, 0, -1):
            if tail[-k:] in self._prefixes:
                return k
        return 0

    def _replacement(self, handle: str) -> str:
        raw = self._table.get(handle, handle)
        return _json_escape(raw) if self.json_mode else raw

    def _drain(self, final: bool) -> str:
        out: List[str] = []
        buf = self.buf
        pos = 0
        while True:
            m = self._rx.search(buf, pos) if self._rx else None
            if m is None:
                tail = buf[pos:]
                hold = 0 if final else self._hold_len(tail)
                if len(tail) > hold:
                    out.append(tail[:len(tail) - hold])
                pos = len(buf) - hold
                break
            gap = buf[pos:m.start()]
            hold = 0 if final else self._hold_len(gap)
            if hold:
                if len(gap) > hold:
                    out.append(gap[:len(gap) - hold])
                pos = m.start() - hold
                break
            out.append(gap)
            out.append(self._replacement(m.group(0)))
            pos = m.end()
        self.buf = buf[pos:]
        return "".join(out)

    # -- 接口 -------------------------------------------------------------- #
    def push(self, text: Any) -> str:
        if not isinstance(text, str) or not text:
            return text if isinstance(text, str) else ""
        self._prepare()
        if not self._rx:
            return text  # 本会话还没有任何 handle，直通，不缓冲
        self.buf += text
        return self._drain(False)

    def close(self) -> str:
        self._prepare()
        if not self.buf:
            return ""
        return self._drain(True)


class CanonicalChunkUnmasker:
    """在 canonical（OpenAI chat chunk）层还原，一次覆盖三种上游协议。"""

    def __init__(self, engine: MaskEngine) -> None:
        self.engine = engine
        self.text = RebindStream(engine, json_mode=False)
        self.reasoning = RebindStream(engine, json_mode=False)
        self.args = RebindStream(engine, json_mode=True)

    def push(self, chunk: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(chunk, dict):
            return chunk
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta")
            if not isinstance(delta, dict):
                continue
            if isinstance(delta.get("content"), str):
                delta["content"] = self.text.push(delta["content"])
            if isinstance(delta.get("reasoning_content"), str):
                delta["reasoning_content"] = self.reasoning.push(delta["reasoning_content"])
            for tc in delta.get("tool_calls") or []:
                fn = tc.get("function") if isinstance(tc, dict) else None
                if isinstance(fn, dict) and isinstance(fn.get("arguments"), str):
                    fn["arguments"] = self.args.push(fn["arguments"])
        return chunk

    def flush(self) -> Optional[Dict[str, Any]]:
        """冲刷三个通道的残留，返回一个待下发的补充分片（无残留则 None）。"""
        text = self.text.close()
        reasoning = self.reasoning.close()
        args = self.args.close()
        if not (text or reasoning or args):
            return None
        delta: Dict[str, Any] = {}
        if text:
            delta["content"] = text
        if reasoning:
            delta["reasoning_content"] = reasoning
        if args:
            delta["tool_calls"] = [{"index": 0, "function": {"arguments": args}}]
        return {
            "id": None, "object": "chat.completion.chunk", "created": None, "model": None,
            "choices": [{"index": 0, "delta": delta, "finish_reason": None, "logprobs": None}],
        }
