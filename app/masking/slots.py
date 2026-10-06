"""槽位存储：handle -> raw 映射，按会话隔离并持久化到 SQLite。

持久化是**正确性要求**而非仅为了回溯：网关是无状态的请求处理器，
要还原「上一轮产生的 handle」或让同一真值在跨轮拿到同一 handle，就必须把
映射存下来。代价是本机 SQLite 里会出现明文真值 —— 这是可逆脱敏的固有代价，
因此提供独立保留期与一键清空（见 README「脱敏数据的存放与风险」）。
"""
from __future__ import annotations

import json
import re
import secrets
import threading
import time
from typing import Dict, List, Optional, Tuple

from .. import db
from .fps import format_preserving_synthetic
from .paths import PLACEHOLDER_BASE_RE

SESSION_TABLE = "mask_sessions"
SLOT_TABLE = "mask_slots"

_CACHE: Dict[str, "SlotStore"] = {}
_CACHE_LOCK = threading.RLock()
_CACHE_MAX = 64

_INDEX_RE = re.compile(r"_(\d+)\]$")


class MaskHit:
    __slots__ = ("kind", "slot_type", "handle", "raw", "category")

    def __init__(self, kind: str, slot_type: str, handle: str, raw: str,
                 category: str = "") -> None:
        self.kind = kind
        self.slot_type = slot_type
        self.handle = handle
        self.raw = raw
        self.category = category

    def to_dict(self) -> Dict[str, str]:
        return {"kind": self.kind, "slot_type": self.slot_type, "handle": self.handle,
                "raw": self.raw, "category": self.category}


class SlotStore:
    """单个脱敏会话的槽位表。"""

    def __init__(self, session_id: str, session_key: bytes) -> None:
        self.session_id = session_id
        self.session_key = session_key
        self.by_handle: Dict[str, str] = {}       # handle -> raw
        self.by_raw: Dict[Tuple[str, str], str] = {}  # (slot_type, raw) -> handle
        self.kinds: Dict[str, str] = {}           # handle -> slot_type
        self.counters: Dict[str, int] = {}
        self._prefixes: Optional[set] = None
        self._cred_counter = 0
        self._loaded = False
        self.version = 0  # 每次新增 handle 自增，供流式还原判断是否需要重建匹配表

    # -- 载入 / 落盘 ------------------------------------------------------- #
    def load(self) -> None:
        if self._loaded:
            return
        rows = db.query(
            f"SELECT public_value, raw_value, slot_type FROM {SLOT_TABLE} WHERE session_id=?",
            (self.session_id,),
        )
        for row in rows:
            handle = row["public_value"]
            raw = row["raw_value"]
            slot_type = row["slot_type"]
            self.by_handle[handle] = raw
            self.by_raw[(slot_type, raw)] = handle
            self.kinds[handle] = slot_type
            if slot_type.startswith("CRED_"):
                self._cred_counter += 1
            else:
                m = _INDEX_RE.search(handle)
                if m:
                    idx = int(m.group(1))
                    self.counters[slot_type] = max(self.counters.get(slot_type, 0), idx)
        self._loaded = True
        self._prefixes = None

    def _persist(self, handle: str, raw: str, slot_type: str, origin: str) -> None:
        try:
            db.execute(
                f"INSERT OR REPLACE INTO {SLOT_TABLE}"
                "(session_id,public_value,raw_value,slot_type,origin,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (self.session_id, handle, raw, slot_type, origin, db.now_str()),
            )
            db.touch_mask_session(self.session_id)
        except Exception:
            pass  # 落盘失败不影响本次请求，仅影响重启后的还原

    # -- 写入 -------------------------------------------------------------- #
    def intern_slot(self, slot_type: str, raw: str,
                    origin: str = "detected") -> Tuple[str, bool]:
        """路径类槽位：`[TYPE_n]`，同一真值在同一会话内保持同一 handle。"""
        self.load()
        key = (slot_type, raw)
        if key in self.by_raw:
            return self.by_raw[key], False
        idx = self.counters.get(slot_type, 0) + 1
        self.counters[slot_type] = idx
        handle = f"[{slot_type}_{idx}]"
        self.by_handle[handle] = raw
        self.by_raw[key] = handle
        self.kinds[handle] = slot_type
        self._prefixes = None
        self.version += 1
        self._persist(handle, raw, slot_type, origin)
        return handle, True

    def intern_credential(self, raw: str, category: str,
                          origin: str = "pattern") -> Tuple[str, bool]:
        """凭证：格式保持的合成值，同一真值在同一会话内保持同一合成值。"""
        self.load()
        slot_type = f"CRED_{category.upper()}"
        key = (slot_type, raw)
        if key in self.by_raw:
            return self.by_raw[key], False
        self._cred_counter += 1
        synthetic = format_preserving_synthetic(category, raw, self.session_key,
                                               self._cred_counter)
        # 极小概率与已有 handle 撞车，退避重算
        guard = 0
        while synthetic in self.by_handle and guard < 8:
            self._cred_counter += 1
            synthetic = format_preserving_synthetic(category, raw, self.session_key,
                                                   self._cred_counter)
            guard += 1
        self.by_handle[synthetic] = raw
        self.by_raw[key] = synthetic
        self.kinds[synthetic] = slot_type
        self._prefixes = None
        self.version += 1
        self._persist(synthetic, raw, slot_type, origin)
        return synthetic, True

    def add_alias(self, alias: str, raw: str, slot_type: str) -> None:
        """给同一真值登记一个「等价写法」，仅用于还原。

        典型场景：`[SENSITIVE_DOC_1]` 在文本里出现为 `[SENSITIVE_DOC_1].pdf`
        （保留扩展名以便模型识别文件类型）。把带扩展名的完整形态也登记为
        可匹配对象后，还原时就不会残留 `.pdf`，流式匹配也无需特判。
        """
        self.load()
        if not alias or alias in self.by_handle:
            return
        self.by_handle[alias] = raw
        self.kinds[alias] = slot_type
        self._prefixes = None
        self.version += 1
        self._persist(alias, raw, slot_type, "alias")

    # -- 读取 -------------------------------------------------------------- #
    def raw_for(self, handle: str) -> Optional[str]:
        self.load()
        return self.by_handle.get(handle)

    def handle_of(self, slot_type: str, raw: str) -> Optional[str]:
        self.load()
        return self.by_raw.get((slot_type, raw))

    def all_handles(self) -> List[str]:
        self.load()
        # 长 handle 优先，避免短 handle 抢先匹配
        return sorted(self.by_handle.keys(), key=len, reverse=True)

    def prefixes(self) -> set:
        """所有 handle 的所有前缀，用于流式还原时判断「是否需要继续等待」。"""
        if self._prefixes is None:
            pre: set = set()
            for handle in self.by_handle:
                for i in range(1, len(handle) + 1):
                    pre.add(handle[:i])
            self._prefixes = pre
        return self._prefixes

    def mappings(self) -> List[Dict[str, str]]:
        self.load()
        out = []
        for handle, raw in self.by_handle.items():
            out.append({"handle": handle, "raw": raw, "slot_type": self.kinds.get(handle, "")})
        out.sort(key=lambda x: x["handle"])
        return out

    def is_empty(self) -> bool:
        self.load()
        return not self.by_handle


# --------------------------------------------------------------------------- #
# 会话
# --------------------------------------------------------------------------- #
def _cache_key(session_id: str) -> str:
    return session_id


def get_store(session_id: str) -> SlotStore:
    """取得（必要时创建）会话对应的槽位表。"""
    with _CACHE_LOCK:
        store = _CACHE.get(_cache_key(session_id))
        if store is not None:
            return store
        row = db.query_one(f"SELECT session_key FROM {SESSION_TABLE} WHERE session_id=?",
                           (session_id,))
        if row and row["session_key"]:
            key = bytes.fromhex(row["session_key"])
        else:
            key = secrets.token_bytes(32)
            db.execute(
                f"INSERT OR REPLACE INTO {SESSION_TABLE}"
                "(session_id,session_key,created_at,updated_at) VALUES(?,?,?,?)",
                (session_id, key.hex(), db.now_str(), db.now_str()),
            )
        store = SlotStore(session_id, key)
        if len(_CACHE) >= _CACHE_MAX:
            _CACHE.pop(next(iter(_CACHE)))
        _CACHE[_cache_key(session_id)] = store
        return store


def drop_store(session_id: str) -> None:
    with _CACHE_LOCK:
        _CACHE.pop(_cache_key(session_id), None)


def clear_session(session_id: str) -> int:
    """删除一个会话的全部映射（旧 handle 将无法还原）。"""
    drop_store(session_id)
    cur = db.execute(f"DELETE FROM {SLOT_TABLE} WHERE session_id=?", (session_id,))
    db.execute(f"DELETE FROM {SESSION_TABLE} WHERE session_id=?", (session_id,))
    return cur.rowcount


def clear_all() -> int:
    with _CACHE_LOCK:
        _CACHE.clear()
    cur = db.execute(f"DELETE FROM {SLOT_TABLE}")
    db.execute(f"DELETE FROM {SESSION_TABLE}")
    return cur.rowcount


def list_sessions() -> List[Dict[str, object]]:
    rows = db.query(
        f"SELECT s.session_id, s.created_at, s.updated_at, "
        f"(SELECT COUNT(*) FROM {SLOT_TABLE} t WHERE t.session_id=s.session_id) AS slots "
        f"FROM {SESSION_TABLE} s ORDER BY s.updated_at DESC LIMIT 200"
    )
    return rows


def purge_sessions(days: int) -> int:
    if days <= 0:
        return 0
    cutoff = db.now_str() if days == 0 else time.strftime(
        "%Y-%m-%d %H:%M:%S", time.localtime(time.time() - days * 86400))
    rows = db.query(f"SELECT session_id FROM {SESSION_TABLE} WHERE updated_at < ?", (cutoff,))
    removed = 0
    for row in rows:
        removed += clear_session(row["session_id"])
    return removed


def session_summary() -> Dict[str, object]:
    row = db.query_one(f"SELECT COUNT(*) AS c FROM {SESSION_TABLE}") or {"c": 0}
    slot = db.query_one(f"SELECT COUNT(*) AS c FROM {SLOT_TABLE}") or {"c": 0}
    by_type = db.query(
        f"SELECT slot_type, COUNT(*) AS c FROM {SLOT_TABLE} GROUP BY slot_type ORDER BY c DESC"
    )
    return {"sessions": row["c"], "slots": slot["c"], "by_type": by_type}


def _placeholder_index(handle: str) -> int:
    m = PLACEHOLDER_BASE_RE.search(handle)
    return int(m.group(2)) if m else 0
