"""脱敏引擎：路径抽象 + 凭证 FPS + 还原。

顺序与 SlotGuard 的 `abstract_text` 一致：先路径，后凭证。邮箱/PII 走凭证的
FPS 通道（保持形态），不再单独生成 `[EMAIL_n]` 槽位。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import paths as pathutil
from .patterns import (CATEGORY_LABELS, CAT_HOST, INTERNAL_HOST_SUFFIXES,
                       CredentialMatch, detect_credentials)
from .slots import MaskHit, SlotStore

# --------------------------------------------------------------------------- #
# 策略
# --------------------------------------------------------------------------- #
DEFAULT_SENSITIVE_KEYWORDS = [
    # 英文（SlotGuard 内置）
    "layoff", "salary", "compensation", "legal", "contract", "acquisition",
    "secret", "private",
    # 中文常见
    "裁员", "薪资", "工资", "合同", "收购", "机密", "私人", "辞退",
    "绩效", "期权", "融资",
]

CRED_MODE_FPS = "fps"
CRED_MODE_PLACEHOLDER = "placeholder"


@dataclass
class MaskPolicy:
    mask_paths: bool = True
    mask_credentials: bool = True
    repo_roots: List[str] = field(default_factory=list)
    workspace_roots: List[str] = field(default_factory=list)
    include_home: bool = True
    preserve_suffix_segments: int = 3
    abstract_sensitive_filenames: bool = True
    sensitive_keywords: List[str] = field(default_factory=lambda: list(DEFAULT_SENSITIVE_KEYWORDS))
    internal_host_suffixes: Tuple[str, ...] = INTERNAL_HOST_SUFFIXES
    mask_outside_roots: bool = False
    credential_mode: str = CRED_MODE_FPS
    propagate_known: bool = True
    min_propagate_len: int = 8

    def all_roots(self) -> List[str]:
        roots = list(self.repo_roots) + list(self.workspace_roots)
        if self.include_home:
            home = pathutil.home_dir()
            if home:
                roots.append(home)
        return roots

    def repo_only(self) -> List[str]:
        return list(self.repo_roots)

    def workspace_only(self) -> List[str]:
        return list(self.workspace_roots)


# --------------------------------------------------------------------------- #
# 路径与敏感词
# --------------------------------------------------------------------------- #
PATH_RE = re.compile(
    r"(?P<posix>(?<![\w/\\:])/(?:[^/\s\[\]()<>\"'|]+/)+[^/\s\[\]()<>\"'|]+)"
    r"|(?P<win>(?<![A-Za-z0-9])[A-Za-z]:[\\/]+(?:[^\\/\s\[\]()<>\"'|]+[\\/]+)*[^\\/\s\[\]()<>\"'|]*)"
    r"|(?P<unc>\\\\[^\\/\s\[\]()<>\"'|]+[\\/][^\\/\s\[\]()<>\"'|]+(?:[\\/]+[^\\/\s\[\]()<>\"'|]+)*)"
)

_TRAILING_JUNK = ".,;:!?"


def _strip_junk(token: str) -> str:
    while token and token[-1] in _TRAILING_JUNK:
        token = token[:-1]
    return token


def contains_sensitive_keyword(text: str, keywords: Sequence[str]) -> bool:
    tokens = [t for t in re.split(r"[^0-9A-Za-z\u4e00-\u9fff]+", text.lower()) if t]
    for kw in keywords:
        kwl = kw.lower()
        if any(tok == kwl for tok in tokens) or (kwl and kwl in text.lower() and not kwl.isascii()):
            return True
    return False


def looks_like_personal_doc(text: str) -> bool:
    segs = [s for s in re.split(r"[-_ ]", text) if s]
    has_year = any(len(s) == 4 and s.isdigit() for s in segs)
    has_connector = any(c in text for c in ("for_", "_for_", "-for-", "for-", "-for"))
    return has_year and has_connector


def is_sensitive_filename(component: str, policy: MaskPolicy) -> bool:
    stem = component.rsplit(".", 1)[0] if "." in component else component
    stem = stem.lower()
    return contains_sensitive_keyword(stem, policy.sensitive_keywords) \
        or looks_like_personal_doc(stem)


def is_sensitive_segment(component: str, policy: MaskPolicy) -> bool:
    lowered = component.lower()
    has_marker = "-" in component or "_" in component or any(c.isdigit() for c in component)
    return has_marker and contains_sensitive_keyword(lowered, policy.sensitive_keywords)


# --------------------------------------------------------------------------- #
# 引擎
# --------------------------------------------------------------------------- #
class MaskEngine:
    def __init__(self, policy: MaskPolicy, store: SlotStore) -> None:
        self.policy = policy
        self.store = store
        self._unmask_re: Optional["re.Pattern[str]"] = None
        self._unmask_map: Dict[str, str] = {}
        self._unmask_dirty = True
        self._unmask_version = -1

    # -- 路径 -------------------------------------------------------------- #
    def abstract_path(self, raw_path: str) -> Optional[str]:
        p = self.policy
        if not pathutil.is_absolute(raw_path) or pathutil.has_parent_ref(raw_path):
            return None
        style_win = pathutil.detect_style(raw_path) == pathutil.STYLE_WIN
        sep = "\\" if style_win else "/"

        repo = pathutil.longest_root(raw_path, p.repo_only())
        workspace = pathutil.longest_root(raw_path, p.workspace_only())
        home = pathutil.home_dir() if p.include_home else None

        root: Optional[str] = None
        slot_type = ""
        collapse = False
        if repo:
            root, slot_type, collapse = repo, "REPO_ROOT", False
        elif workspace:
            root, slot_type, collapse = workspace, "WORKSPACE_ROOT", False
        elif home and pathutil.is_under(raw_path, home):
            root, slot_type, collapse = home, "USER_HOME", True
        elif p.mask_outside_roots and pathutil.is_absolute(raw_path):
            root, slot_type, collapse = None, "", True
        else:
            return None

        parts = pathutil.relative_parts(raw_path, root) if root else pathutil.split_path(raw_path)[1]
        if root:
            root_handle, _ = self.store.intern_slot(slot_type, root)
        else:
            root_handle = ""

        if collapse and parts:
            keep = max(0, int(p.preserve_suffix_segments))
            hidden, kept = parts[:max(0, len(parts) - keep)], parts[len(parts) - keep:]
            out_parts: List[str] = []
            if hidden:
                handle, _ = self.store.intern_slot("SENSITIVE_PATH_SEGMENT", sep.join(hidden))
                out_parts.append(handle)
            out_parts.extend(self._abstract_components(kept))
            head = root_handle
        else:
            out_parts = self._abstract_components(parts)
            head = root_handle

        if not out_parts:
            return head or None
        body = sep.join(out_parts)
        return f"{head}{sep}{body}" if head else body

    def _abstract_components(self, components: List[str]) -> List[str]:
        p = self.policy
        out: List[str] = []
        last = len(components) - 1
        for idx, comp in enumerate(components):
            if idx == last and p.abstract_sensitive_filenames and is_sensitive_filename(comp, p):
                handle, _ = self.store.intern_slot("SENSITIVE_DOC", comp)
                ext = ""
                if "." in comp:
                    ext = "." + comp.rsplit(".", 1)[1]
                if ext:
                    self.store.add_alias(handle + ext, comp, "SENSITIVE_DOC_ALIAS")
                out.append(handle + ext)
                continue
            if idx != last and is_sensitive_segment(comp, p):
                handle, _ = self.store.intern_slot("SENSITIVE_PATH_SEGMENT", comp)
                out.append(handle)
                continue
            out.append(comp)
        return out

    # -- 凭证 -------------------------------------------------------------- #
    def _credential_replacement(self, raw: str, category: str) -> str:
        if self.policy.credential_mode == CRED_MODE_PLACEHOLDER:
            handle, _ = self.store.intern_slot(f"CRED_{category.upper()}", raw,
                                               origin="credential")
            return handle
        synthetic, _ = self.store.intern_credential(raw, category)
        return synthetic

    # -- 主流程 ------------------------------------------------------------ #
    def mask_text(self, text: str) -> Tuple[str, List[MaskHit]]:
        if not text or not isinstance(text, str):
            return text, []
        spans: List[Tuple[int, int, str, MaskHit]] = []

        if self.policy.mask_paths:
            for m in PATH_RE.finditer(text):
                token = _strip_junk(m.group(0))
                if len(token) < 4:
                    continue
                replacement = self.abstract_path(token)
                if not replacement or replacement == token:
                    continue
                start = m.start()
                spans.append((start, start + len(token), replacement,
                              MaskHit("path", "PATH", replacement, token)))

        if self.policy.mask_credentials:
            for hit in detect_credentials(text, self.policy.internal_host_suffixes):
                if hit.category == CAT_HOST and not any(
                        hit.raw.lower().endswith(s) for s in self.policy.internal_host_suffixes):
                    continue
                replacement = self._credential_replacement(hit.raw, hit.category)
                if replacement == hit.raw:
                    continue
                spans.append((hit.start, hit.end, replacement,
                              MaskHit("credential", hit.category.upper(), replacement,
                                      hit.raw, hit.category)))

        if self.policy.propagate_known:
            spans.extend(self._propagation_spans(text, spans))

        masked = self._splice(text, spans)
        hits = [s[3] for s in self._dedupe(spans)]
        return masked, hits

    def _propagation_spans(self, text: str,
                           existing: List[Tuple[int, int, str, MaskHit]]
                           ) -> List[Tuple[int, int, str, MaskHit]]:
        """已在本会话出现过的真值，即使正则没命中也要替换（子串扫描）。

        这是 SlotGuard 中真正起作用的跨轮机制 —— 它的 SEG edges/redirections
        经核实是死代码，实际依赖的就是这段扫描。
        """
        out: List[Tuple[int, int, str, MaskHit]] = []
        for (slot_type, raw), handle in list(self.store.by_raw.items()):
            if len(raw) < self.policy.min_propagate_len:
                continue
            start = 0
            while True:
                idx = text.find(raw, start)
                if idx < 0:
                    break
                end = idx + len(raw)
                if not any(idx < e and s < end for s, e, _, _ in existing):
                    out.append((idx, end, handle,
                                MaskHit("slot", slot_type, handle, raw)))
                start = end
        return out

    @staticmethod
    def _dedupe(spans: List[Tuple[int, int, str, MaskHit]]
                ) -> List[Tuple[int, int, str, MaskHit]]:
        ordered = sorted(spans, key=lambda s: (s[0], -(s[1] - s[0])))
        kept: List[Tuple[int, int, str, MaskHit]] = []
        last_end = -1
        for span in ordered:
            if span[0] < last_end:
                continue
            last_end = span[1]
            kept.append(span)
        return kept

    @classmethod
    def _splice(cls, text: str, spans: List[Tuple[int, int, str, MaskHit]]) -> str:
        kept = cls._dedupe(spans)
        if not kept:
            return text
        out = text
        for start, end, replacement, _hit in reversed(kept):
            out = out[:start] + replacement + out[end:]
        return out

    # -- 还原 -------------------------------------------------------------- #
    def _build_unmask(self) -> None:
        # 槽位表在「脱敏过程中」会增长，必须按 version 判断缓存是否过期，
        # 否则同一请求内先脱敏再还原时，新产生的 handle 不会被识别。
        if (not self._unmask_dirty and self._unmask_re is not None
                and self._unmask_version == self.store.version):
            return
        handles = self.store.all_handles()  # 已按长度倒序，长 handle 优先
        self._unmask_map = {h: self.store.by_handle.get(h, "") for h in handles}
        self._unmask_re = (re.compile("|".join(re.escape(h) for h in handles))
                           if handles else None)
        self._unmask_dirty = False
        self._unmask_version = self.store.version

    def unmask_text(self, text: str) -> str:
        if not text or not isinstance(text, str):
            return text
        if not self.store.all_handles():
            return text
        self._build_unmask()
        rx = self._unmask_re
        if rx is None:
            return text
        table = self._unmask_map
        return rx.sub(lambda m: table.get(m.group(0), m.group(0)), text)

    def invalidate(self) -> None:
        self._unmask_dirty = True

    # -- JSON 值遍历 ------------------------------------------------------- #
    def mask_value(self, value: Any, hits: List[MaskHit],
                   skip_keys: Tuple[str, ...] = ()) -> Any:
        return _walk(value, lambda s: self._mask_collect(s, hits), skip_keys)

    def unmask_value(self, value: Any, skip_keys: Tuple[str, ...] = ()) -> Any:
        return _walk(value, self.unmask_text, skip_keys)

    def _mask_collect(self, text: str, hits: List[MaskHit]) -> str:
        masked, found = self.mask_text(text)
        hits.extend(found)
        return masked


def _walk(value: Any, fn, skip_keys: Tuple[str, ...] = ()) -> Any:
    if isinstance(value, str):
        return fn(value)
    if isinstance(value, list):
        return [_walk(v, fn, skip_keys) for v in value]
    if isinstance(value, dict):
        return {k: (v if k in skip_keys else _walk(v, fn, skip_keys))
                for k, v in value.items()}
    return value


def mask_json_field(text: str, engine: MaskEngine, hits: List[MaskHit]) -> str:
    """对「本身是 JSON 的字符串字段」（如 tool_call arguments）做结构化脱敏。

    直接对转义后的文本做正则匹配会因 `\\\\` 双反斜杠而漏掉 Windows 路径，
    因此优先解析 JSON、逐字符串脱敏、再序列化；解析失败则退化为纯文本脱敏。
    """
    stripped = text.strip()
    if not stripped or stripped[0] not in "{[":
        return engine._mask_collect(text, hits)
    try:
        parsed = json.loads(stripped)
    except Exception:
        return engine._mask_collect(text, hits)
    masked = engine.mask_value(parsed, hits)
    try:
        return json.dumps(masked, ensure_ascii=False)
    except Exception:
        return engine._mask_collect(text, hits)


def unmask_json_field(text: str, engine: MaskEngine) -> str:
    stripped = text.strip() if isinstance(text, str) else ""
    if not stripped or stripped[0] not in "{[":
        return engine.unmask_text(text)
    try:
        parsed = json.loads(stripped)
    except Exception:
        return engine.unmask_text(text)
    out = engine.unmask_value(parsed)
    try:
        return json.dumps(out, ensure_ascii=False)
    except Exception:
        return engine.unmask_text(text)
