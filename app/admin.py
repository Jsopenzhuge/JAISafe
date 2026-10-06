"""管理后台 API（供 WebUI 调用）。"""
from __future__ import annotations

import time
from typing import Any, Dict, Optional

import httpx
from fastapi import (APIRouter, Body, Cookie, Depends, HTTPException, Query, Response)

from . import db
from .formats import CHANNEL_TYPES
from .masking import MaskEngine
from .masking import config as mask_config
from .masking import slots as mask_slots
from .relay import build_upstream_headers, build_url

router = APIRouter(prefix="/admin/api")

COOKIE_NAME = "jai_admin"
SESSION_DAYS = 7

SAFE_SETTING_KEYS = [
    "require_api_key", "log_retention_days", "max_body_log",
    "log_request_body", "log_response_body", "site_name",
]

MASK_SETTING_KEYS = [
    "mask_mode", "mask_paths", "mask_credentials", "mask_workspace_roots",
    "mask_repo_roots", "mask_include_home", "mask_preserve_segments",
    "mask_sensitive_keywords", "mask_internal_suffixes", "mask_outside_roots",
    "mask_credential_mode", "mask_propagate", "mask_session_source",
    "mask_store_map", "mask_retention_days",
]


def require_admin(jai_admin: Optional[str] = Cookie(default=None)) -> str:
    if not db.check_session(jai_admin or ""):
        raise HTTPException(status_code=401, detail="未登录或会话已过期")
    return jai_admin or ""


# --------------------------------------------------------------------------- #
# 会话
# --------------------------------------------------------------------------- #
@router.post("/login")
async def login(response: Response, payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    password = str(payload.get("password") or "")
    if not db.verify_password(password):
        raise HTTPException(status_code=401, detail="密码错误")
    db.purge_sessions()
    token = db.create_session(SESSION_DAYS)
    response.set_cookie(COOKIE_NAME, token, max_age=SESSION_DAYS * 86400,
                        httponly=True, samesite="lax", path="/")
    return {"ok": True}


@router.post("/logout")
async def logout(response: Response,
                 jai_admin: Optional[str] = Cookie(default=None)) -> Dict[str, Any]:
    if jai_admin:
        db.drop_session(jai_admin)
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"ok": True}


@router.get("/session")
async def session_info(jai_admin: Optional[str] = Cookie(default=None)) -> Dict[str, Any]:
    return {
        "authenticated": db.check_session(jai_admin or ""),
        "site_name": db.get_setting("site_name", "JAISafe LLM Gateway"),
        "channel_types": CHANNEL_TYPES,
    }


@router.post("/password")
async def change_password(payload: Dict[str, Any] = Body(...),
                          admin: str = Depends(require_admin)) -> Dict[str, Any]:
    old = str(payload.get("old_password") or "")
    new = str(payload.get("new_password") or "")
    if not db.verify_password(old):
        raise HTTPException(status_code=400, detail="原密码错误")
    if len(new) < 4:
        raise HTTPException(status_code=400, detail="新密码至少 4 位")
    db.set_setting("admin_password", db.hash_password(new))
    db.execute("DELETE FROM sessions")
    return {"ok": True, "relogin": True}


# --------------------------------------------------------------------------- #
# 概览
# --------------------------------------------------------------------------- #
@router.get("/overview")
async def overview(admin: str = Depends(require_admin)) -> Dict[str, Any]:
    logs = db.list_logs(limit=12)
    return {
        "stats": db.log_stats(),
        "channels": db.list_channels(),
        "models": db.model_usage(),
        "channel_usage": db.channel_usage(),
        "recent": logs["items"],
        "keys": len(db.list_keys()),
        "require_api_key": db.get_setting("require_api_key", "0") == "1"
        or db.count_enabled_keys() > 0,
    }


# --------------------------------------------------------------------------- #
# 渠道
# --------------------------------------------------------------------------- #
@router.get("/channels")
async def get_channels(admin: str = Depends(require_admin)) -> Dict[str, Any]:
    return {"items": db.list_channels(), "types": CHANNEL_TYPES}


@router.post("/channels")
async def add_channel(payload: Dict[str, Any] = Body(...),
                      admin: str = Depends(require_admin)) -> Dict[str, Any]:
    if not payload.get("base_url"):
        raise HTTPException(status_code=400, detail="base_url 不能为空")
    cid = db.create_channel(payload)
    return {"ok": True, "id": cid}


@router.put("/channels/{cid}")
async def edit_channel(cid: int, payload: Dict[str, Any] = Body(...),
                       admin: str = Depends(require_admin)) -> Dict[str, Any]:
    if not db.get_channel(cid):
        raise HTTPException(status_code=404, detail="渠道不存在")
    db.update_channel(cid, payload)
    return {"ok": True}


@router.delete("/channels/{cid}")
async def remove_channel(cid: int, admin: str = Depends(require_admin)) -> Dict[str, Any]:
    db.delete_channel(cid)
    return {"ok": True}


@router.post("/channels/{cid}/test")
async def test_channel(cid: int, admin: str = Depends(require_admin)) -> Dict[str, Any]:
    channel = db.get_channel(cid)
    if not channel:
        raise HTTPException(status_code=404, detail="渠道不存在")
    return await probe_channel(channel)


async def probe_channel(channel: Dict[str, Any]) -> Dict[str, Any]:
    ctype = channel.get("type") or "openai"
    models = channel.get("model_list") or []
    if ctype == "anthropic":
        model = models[0] if models else "claude-3-5-haiku-20241022"
        endpoint = "messages"
        body: Dict[str, Any] = {
            "model": model, "max_tokens": 16,
            "messages": [{"role": "user", "content": "ping"}],
        }
    elif ctype == "openai_responses":
        model = models[0] if models else "gpt-4o-mini"
        endpoint = "responses"
        body = {"model": model, "input": "ping", "max_output_tokens": 16}
    else:
        model = models[0] if models else "gpt-4o-mini"
        endpoint = "chat/completions"
        body = {"model": model, "max_tokens": 16,
                "messages": [{"role": "user", "content": "ping"}]}

    url = build_url(channel.get("base_url") or "", endpoint)
    headers = build_upstream_headers(channel, False)
    started = time.time()
    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            resp = await client.post(url, headers=headers, json=body)
        return {
            "ok": resp.status_code < 400,
            "status": resp.status_code,
            "elapsed_ms": int((time.time() - started) * 1000),
            "url": url,
            "model": model,
            "body": resp.text[:2000],
        }
    except Exception as exc:
        return {
            "ok": False, "status": 0,
            "elapsed_ms": int((time.time() - started) * 1000),
            "url": url, "model": model, "body": f"连接失败: {exc}",
        }


# --------------------------------------------------------------------------- #
# 密钥
# --------------------------------------------------------------------------- #
@router.get("/keys")
async def get_keys(admin: str = Depends(require_admin)) -> Dict[str, Any]:
    return {
        "items": db.list_keys(),
        "effective_require": db.count_enabled_keys() > 0
        or db.get_setting("require_api_key", "0") == "1",
    }


@router.post("/keys")
async def add_key(payload: Dict[str, Any] = Body(...),
                  admin: str = Depends(require_admin)) -> Dict[str, Any]:
    kid = db.create_key(payload)
    return {"ok": True, "id": kid}


@router.put("/keys/{kid}")
async def edit_key(kid: int, payload: Dict[str, Any] = Body(...),
                   admin: str = Depends(require_admin)) -> Dict[str, Any]:
    db.update_key(kid, payload)
    return {"ok": True}


@router.delete("/keys/{kid}")
async def remove_key(kid: int, admin: str = Depends(require_admin)) -> Dict[str, Any]:
    db.delete_key(kid)
    return {"ok": True}


# --------------------------------------------------------------------------- #
# 日志
# --------------------------------------------------------------------------- #
@router.get("/logs")
async def get_logs(limit: int = Query(30, ge=1, le=200), offset: int = Query(0, ge=0),
                   keyword: str = "", status: str = "", channel: str = "",
                   admin: str = Depends(require_admin)) -> Dict[str, Any]:
    return db.list_logs(limit=limit, offset=offset, keyword=keyword, status=status,
                        channel=channel)


@router.get("/logs/{log_id}")
async def get_log_detail(log_id: int, admin: str = Depends(require_admin)) -> Dict[str, Any]:
    row = db.get_log(log_id)
    if not row:
        raise HTTPException(status_code=404, detail="日志不存在")
    return row


@router.delete("/logs")
async def clear_all_logs(admin: str = Depends(require_admin)) -> Dict[str, Any]:
    db.clear_logs()
    return {"ok": True}


# --------------------------------------------------------------------------- #
# 设置
# --------------------------------------------------------------------------- #
@router.get("/settings")
async def get_settings(admin: str = Depends(require_admin)) -> Dict[str, Any]:
    settings = db.all_settings()
    return {k: settings.get(k, "") for k in SAFE_SETTING_KEYS}


@router.post("/settings")
async def save_settings(payload: Dict[str, Any] = Body(...),
                        admin: str = Depends(require_admin)) -> Dict[str, Any]:
    for key in SAFE_SETTING_KEYS:
        if key in payload:
            db.set_setting(key, str(payload[key]))
    return {"ok": True}


# --------------------------------------------------------------------------- #
# 脱敏（本地上下文遮蔽）
# --------------------------------------------------------------------------- #
@router.get("/mask")
async def get_mask_config(admin: str = Depends(require_admin)) -> Dict[str, Any]:
    settings = db.all_settings()
    return {
        "settings": {k: settings.get(k, "") for k in MASK_SETTING_KEYS},
        "policy": mask_config.policy_preview(),
        "summary": mask_slots.session_summary(),
        "sessions": mask_slots.list_sessions(),
    }


@router.post("/mask/settings")
async def save_mask_settings(payload: Dict[str, Any] = Body(...),
                             admin: str = Depends(require_admin)) -> Dict[str, Any]:
    for key in MASK_SETTING_KEYS:
        if key in payload:
            db.set_setting(key, str(payload[key]))
    return {"ok": True}


@router.post("/mask/preview")
async def preview_mask(payload: Dict[str, Any] = Body(...),
                       admin: str = Depends(require_admin)) -> Dict[str, Any]:
    """用当前策略对一段文本做干跑，方便验证根目录/关键词配置。"""
    text = str(payload.get("text") or "")
    if not text:
        return {"masked": "", "hits": [], "error": "文本为空"}
    try:
        store = mask_slots.get_store("__preview__")
        engine = MaskEngine(mask_config.load_policy(), store)
        masked, hits = engine.mask_text(text)
        return {
            "masked": masked,
            "hits": [h.to_dict() for h in hits],
            "changed": masked != text,
            "unmasked": engine.unmask_text(masked),
        }
    except Exception as exc:
        return {"masked": text, "hits": [], "error": str(exc)}


@router.get("/mask/sessions")
async def list_mask_sessions(admin: str = Depends(require_admin)) -> Dict[str, Any]:
    return {"items": mask_slots.list_sessions(), "summary": mask_slots.session_summary()}


@router.get("/mask/sessions/{session_id}")
async def get_mask_session(session_id: str, admin: str = Depends(require_admin)) -> Dict[str, Any]:
    store = mask_slots.get_store(session_id)
    return {"session_id": session_id, "mappings": store.mappings()}


@router.delete("/mask/sessions/{session_id}")
async def delete_mask_session(session_id: str,
                              admin: str = Depends(require_admin)) -> Dict[str, Any]:
    removed = mask_slots.clear_session(session_id)
    return {"ok": True, "removed": removed}


@router.delete("/mask/sessions")
async def clear_mask_sessions(admin: str = Depends(require_admin)) -> Dict[str, Any]:
    removed = mask_slots.clear_all()
    return {"ok": True, "removed": removed}


@router.get("/mask/lookup")
async def lookup_mask_handle(handle: str = Query(...),
                             session_id: str = Query(""),
                             admin: str = Depends(require_admin)) -> Dict[str, Any]:
    """按 handle 反查真值；未指定会话时在所有会话里找一次。"""
    if session_id:
        store = mask_slots.get_store(session_id)
        raw = store.raw_for(handle)
        return {"handle": handle, "raw": raw, "session_id": session_id}
    for row in mask_slots.list_sessions():
        sid = str(row["session_id"])
        raw = mask_slots.get_store(sid).raw_for(handle)
        if raw is not None:
            return {"handle": handle, "raw": raw, "session_id": sid}
    return {"handle": handle, "raw": None, "session_id": None}
