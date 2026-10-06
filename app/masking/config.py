"""脱敏配置装配：从 settings / channel 生成 MaskPolicy 与会话标识。"""
from __future__ import annotations

import hashlib
from typing import Any, Dict, List, Optional

from .. import db
from . import paths as pathutil
from .engine import (CRED_MODE_FPS, CRED_MODE_PLACEHOLDER, DEFAULT_SENSITIVE_KEYWORDS,
                     MaskPolicy)

MODE_OFF = "off"
MODE_DRY = "dry_run"
MODE_ENFORCE = "enforce"
MASK_MODES = (MODE_OFF, MODE_DRY, MODE_ENFORCE)


def _split_list(value: str) -> List[str]:
    if not value:
        return []
    parts = []
    for chunk in value.replace("\r", "\n").replace(",", "\n").split("\n"):
        chunk = chunk.strip()
        if chunk:
            parts.append(chunk)
    return parts


def global_mode() -> str:
    mode = (db.get_setting("mask_mode", MODE_OFF) or MODE_OFF).strip().lower()
    return mode if mode in MASK_MODES else MODE_OFF


def effective_mode(channel: Optional[Dict[str, Any]]) -> str:
    """渠道级覆盖：inherit 走全局，off/enforce 覆盖之。"""
    override = ""
    if channel:
        override = (channel.get("mask_mode") or "inherit").strip().lower()
    if override in MASK_MODES:
        return override
    return global_mode()


def load_policy() -> MaskPolicy:
    keywords = _split_list(db.get_setting("mask_sensitive_keywords", ""))
    suffixes = _split_list(db.get_setting("mask_internal_suffixes", ""))
    cred_mode = (db.get_setting("mask_credential_mode", CRED_MODE_FPS) or CRED_MODE_FPS).lower()
    if cred_mode not in (CRED_MODE_FPS, CRED_MODE_PLACEHOLDER):
        cred_mode = CRED_MODE_FPS
    try:
        preserve = int(db.get_setting("mask_preserve_segments", "3") or 3)
    except ValueError:
        preserve = 3
    return MaskPolicy(
        mask_paths=db.get_setting("mask_paths", "1") == "1",
        mask_credentials=db.get_setting("mask_credentials", "1") == "1",
        repo_roots=_split_list(db.get_setting("mask_repo_roots", "")),
        workspace_roots=_split_list(db.get_setting("mask_workspace_roots", "")),
        include_home=db.get_setting("mask_include_home", "1") == "1",
        preserve_suffix_segments=preserve,
        sensitive_keywords=keywords or list(DEFAULT_SENSITIVE_KEYWORDS),
        internal_host_suffixes=tuple(suffixes) if suffixes else (
            ".internal", ".intranet", ".corp", ".local", ".lan"),
        mask_outside_roots=db.get_setting("mask_outside_roots", "0") == "1",
        credential_mode=cred_mode,
        propagate_known=db.get_setting("mask_propagate", "1") == "1",
    )


def policy_preview() -> Dict[str, Any]:
    policy = load_policy()
    return {
        "mask_paths": policy.mask_paths,
        "mask_credentials": policy.mask_credentials,
        "repo_roots": policy.repo_roots,
        "workspace_roots": policy.workspace_roots,
        "include_home": policy.include_home,
        "home": pathutil.home_dir(),
        "preserve_suffix_segments": policy.preserve_suffix_segments,
        "sensitive_keywords": policy.sensitive_keywords,
        "internal_host_suffixes": list(policy.internal_host_suffixes),
        "mask_outside_roots": policy.mask_outside_roots,
        "credential_mode": policy.credential_mode,
        "propagate_known": policy.propagate_known,
    }


# --------------------------------------------------------------------------- #
# 会话标识
# --------------------------------------------------------------------------- #
def resolve_session_id(request: Any, body: Dict[str, Any], client_format: str,
                       relay_key: str = "") -> str:
    """决定 handle 的作用域。

    稳定性比隔离性更重要：同一真值一旦在不同请求里拿到不同 handle，
    模型看到的路径每轮都在变，任务会直接崩。优先级：
      X-Mask-Session 头 > 请求体里的 user / metadata.user_id > API Key > 客户端 IP
    """
    source = (db.get_setting("mask_session_source", "auto") or "auto").lower()
    header_val = ""
    try:
        header_val = (request.headers.get("x-mask-session") or "").strip()
    except Exception:
        header_val = ""

    if source == "global":
        return "global"
    if source == "header":
        return header_val or _ip_part(request)
    if source == "api_key":
        return _key_part(relay_key) or _ip_part(request)
    if source == "ip":
        return _ip_part(request)

    # auto
    if header_val:
        return header_val
    user = ""
    if isinstance(body, dict):
        meta = body.get("metadata")
        if isinstance(meta, dict) and meta.get("user_id"):
            user = str(meta["user_id"])
        elif body.get("user"):
            user = str(body["user"])
    if user:
        return "u-" + hashlib.sha256(user.encode("utf-8")).hexdigest()[:16]
    if relay_key:
        return _key_part(relay_key)
    return _ip_part(request)


def _key_part(relay_key: str) -> str:
    if not relay_key:
        return ""
    return "k-" + hashlib.sha256(relay_key.encode("utf-8")).hexdigest()[:16]


def _ip_part(request: Any) -> str:
    try:
        host = request.client.host if request.client else "anon"
    except Exception:
        host = "anon"
    return "ip-" + host


def store_enabled() -> bool:
    return db.get_setting("mask_store_map", "1") == "1"
