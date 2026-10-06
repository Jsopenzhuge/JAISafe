"""跨平台路径工具（POSIX / Windows 盘符 / UNC）。

SlotGuard 原实现只识别 POSIX 绝对路径（`path_regex` 是 `(^|[\\s(])(/...)`），
在 Windows 上完全失效。这里补齐三种形态，并用统一的「规范键」做前缀比较，
使网关在任意宿主系统上都能处理三种形态的路径。
"""
from __future__ import annotations

import os
import re
from typing import List, Optional, Sequence, Tuple

STYLE_WIN = "win"
STYLE_POSIX = "posix"

_DRIVE_RE = re.compile(r"^([A-Za-z]):[\\/]")
_UNC_RE = re.compile(r"^[\\/]{2}([^\\/]+)[\\/]([^\\/]+)")


def detect_style(path: str) -> str:
    return STYLE_WIN if (_DRIVE_RE.match(path) or _UNC_RE.match(path)) else STYLE_POSIX


def split_path(path: str) -> Tuple[str, List[str]]:
    """返回 (root, parts)。root 形如 `/`、`C:\\`、`\\\\server\\share\\` 或 `''`。"""
    if not path:
        return "", []
    drive = _DRIVE_RE.match(path)
    if drive:
        rest = path[drive.end():]
        return path[:drive.end()], _parts(rest)
    unc = _UNC_RE.match(path)
    if unc:
        rest = path[unc.end():]
        root = path[:unc.end()].rstrip("\\/") + "\\"
        return root, _parts(rest)
    if path.startswith("/"):
        return "/", _parts(path[1:])
    if path.startswith("\\"):
        return "\\", _parts(path[1:])
    return "", _parts(path)


def _parts(rest: str) -> List[str]:
    return [p for p in re.split(r"[\\/]+", rest) if p and p != "."]


def is_absolute(path: str) -> bool:
    return bool(_DRIVE_RE.match(path) or _UNC_RE.match(path)
                or path.startswith("/") or path.startswith("\\"))


def has_parent_ref(path: str) -> bool:
    return ".." in _parts(path)


def canonical(path: str) -> str:
    """用于前缀比较的规范键：统一分隔符；Windows 形态额外小写。"""
    root, parts = split_path(path)
    style = detect_style(path)
    body = "/".join(parts)
    if style == STYLE_WIN:
        root = root.replace("\\", "/").lower()
        body = body.lower()
        return (root + body).rstrip("/")
    return ("/" + body) if root == "/" else body


def is_under(path: str, root: str) -> bool:
    """path 是否位于 root 之下（含相等）。风格不同直接判否。"""
    if not root or not path:
        return False
    if detect_style(path) != detect_style(root):
        return False
    p, r = canonical(path), canonical(root)
    if not r:
        return False
    return p == r or p.startswith(r.rstrip("/") + "/")


def longest_root(path: str, roots: Sequence[str]) -> Optional[str]:
    """在 roots 中找匹配 path 的最长前缀。"""
    best: Optional[str] = None
    best_len = -1
    for root in roots:
        if not root:
            continue
        if is_under(path, root):
            length = len(canonical(root))
            if length > best_len:
                best, best_len = root, length
    return best


def relative_parts(path: str, root: str) -> List[str]:
    """path 相对 root 的组件；root 为空时返回去掉根之后的组件。"""
    if not root:
        return split_path(path)[1]
    p, r = canonical(path), canonical(root)
    if not (p == r or p.startswith(r.rstrip("/") + "/")):
        return []
    tail = p[len(r.rstrip("/")):].lstrip("/")
    return [seg for seg in tail.split("/") if seg]


def home_dir() -> Optional[str]:
    """当前运行用户的 home（供脱敏判定，不用于展开 `~`）。"""
    for key in ("USERPROFILE", "HOME"):
        val = os.environ.get(key)
        if val:
            return val
    drive = os.environ.get("HOMEDRIVE")
    path = os.environ.get("HOMEPATH")
    if drive and path:
        return drive + path
    return None


# --------------------------------------------------------------------------- #
# 占位符形态
# --------------------------------------------------------------------------- #
#: `[REPO_ROOT_1]` / `[SENSITIVE_DOC_2].pdf`
PLACEHOLDER_RE = re.compile(r"\[([A-Z][A-Z0-9_]*)_(\d+)\](?:\.[A-Za-z0-9]{1,16})?")
PLACEHOLDER_BASE_RE = re.compile(r"\[([A-Z][A-Z0-9_]*)_(\d+)\]")


def looks_like_placeholder(text: str) -> bool:
    return bool(re.fullmatch(r"\[[A-Z][A-Z0-9_]*_\d+\](?:\.[^/\\]+)?", text))
