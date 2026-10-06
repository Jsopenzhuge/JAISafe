"""SQLite 存储层：渠道 / 密钥 / 日志 / 设置 / 会话。

不依赖任何 ORM，直接使用标准库 sqlite3，保证轻量。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.environ.get("JAI_DATA_DIR") or os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "gateway.db")

_lock = threading.RLock()
_conn: Optional[sqlite3.Connection] = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS channels (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT    NOT NULL,
    type          TEXT    NOT NULL DEFAULT 'openai',
    base_url      TEXT    NOT NULL,
    api_key       TEXT    NOT NULL DEFAULT '',
    models        TEXT    NOT NULL DEFAULT '',
    model_map     TEXT    NOT NULL DEFAULT '{}',
    extra_headers TEXT    NOT NULL DEFAULT '{}',
    priority      INTEGER NOT NULL DEFAULT 0,
    timeout       INTEGER NOT NULL DEFAULT 300,
    stream_usage  INTEGER NOT NULL DEFAULT 1,
    enabled       INTEGER NOT NULL DEFAULT 1,
    mask_mode     TEXT    NOT NULL DEFAULT 'inherit',
    remark        TEXT    NOT NULL DEFAULT '',
    created_at    TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS api_keys (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT    NOT NULL DEFAULT '',
    key        TEXT    NOT NULL UNIQUE,
    enabled    INTEGER NOT NULL DEFAULT 1,
    quota      REAL    NOT NULL DEFAULT 0,
    used       REAL    NOT NULL DEFAULT 0,
    remark     TEXT    NOT NULL DEFAULT '',
    created_at TEXT    NOT NULL,
    last_used  TEXT
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    token      TEXT PRIMARY KEY,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS logs (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at        TEXT,
    created_ts        REAL,
    method            TEXT,
    path              TEXT,
    client_format     TEXT,
    upstream_format   TEXT,
    channel_id        INTEGER,
    channel_name      TEXT,
    api_key_name      TEXT,
    client_ip         TEXT,
    request_model     TEXT,
    upstream_model    TEXT,
    stream            INTEGER DEFAULT 0,
    status            INTEGER DEFAULT 0,
    duration_ms       INTEGER DEFAULT 0,
    retries           INTEGER DEFAULT 0,
    prompt_tokens     INTEGER DEFAULT 0,
    completion_tokens INTEGER DEFAULT 0,
    total_tokens      INTEGER DEFAULT 0,
    upstream_url      TEXT,
    request_headers   TEXT,
    request_body      TEXT,
    upstream_request  TEXT,
    response_headers  TEXT,
    response_body     TEXT,
    stream_raw        TEXT,
    error             TEXT,
    mask_mode         TEXT,
    mask_session      TEXT,
    mask_summary      TEXT,
    mask_map          TEXT
);

CREATE INDEX IF NOT EXISTS idx_logs_ts ON logs(created_ts DESC);
CREATE INDEX IF NOT EXISTS idx_logs_model ON logs(request_model);

CREATE TABLE IF NOT EXISTS stats (
    day      TEXT PRIMARY KEY,
    requests INTEGER NOT NULL DEFAULT 0,
    errors   INTEGER NOT NULL DEFAULT 0,
    tokens   INTEGER NOT NULL DEFAULT 0
);

-- 脱敏（本地上下文遮蔽）——
-- 映射必须持久化：网关是无状态请求处理器，还原「上一轮的 handle」需要它。
CREATE TABLE IF NOT EXISTS mask_sessions (
    session_id  TEXT PRIMARY KEY,
    session_key TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS mask_slots (
    session_id   TEXT NOT NULL,
    public_value TEXT NOT NULL,
    raw_value    TEXT NOT NULL,
    slot_type    TEXT NOT NULL,
    origin       TEXT NOT NULL DEFAULT 'detected',
    created_at   TEXT NOT NULL,
    PRIMARY KEY (session_id, public_value)
);

CREATE INDEX IF NOT EXISTS idx_mask_slots_session ON mask_slots(session_id);
"""

#: 老库升级用：logs 表后加的列
LOG_MIGRATIONS = {
    "mask_mode": "TEXT",
    "mask_session": "TEXT",
    "mask_summary": "TEXT",
    "mask_map": "TEXT",
    "mask_preview": "TEXT",
}

#: 老库升级用：其它表后加的列
TABLE_MIGRATIONS = {
    "channels": {"mask_mode": "TEXT NOT NULL DEFAULT 'inherit'"},
}

DEFAULT_SETTINGS = {
    "admin_password": "",          # 首次启动时生成（默认 admin）
    "require_api_key": "0",
    "log_retention_days": "7",
    "max_body_log": "262144",      # 单条日志正文最大字节数
    "log_request_body": "1",
    "log_response_body": "1",
    "site_name": "JAISafe LLM Gateway",
    # --- 脱敏 ---------------------------------------------------------- #
    "mask_mode": "off",            # off | dry_run | enforce
    "mask_paths": "1",
    "mask_credentials": "1",
    "mask_workspace_roots": "",
    "mask_repo_roots": "",
    "mask_include_home": "1",
    "mask_preserve_segments": "3",
    "mask_sensitive_keywords": "",
    "mask_internal_suffixes": ".internal,.intranet,.corp,.local,.lan",
    "mask_outside_roots": "0",
    "mask_credential_mode": "fps",  # fps | placeholder
    "mask_propagate": "1",
    "mask_session_source": "auto",  # auto | header | api_key | ip | global
    "mask_store_map": "1",          # 是否记录映射（关闭则无法追溯/还原）
    "mask_retention_days": "7",
}


def _connect() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        os.makedirs(DATA_DIR, exist_ok=True)
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA synchronous=NORMAL")
    return _conn


def init_db() -> None:
    with _lock:
        conn = _connect()
        conn.executescript(SCHEMA)
        conn.commit()
        _migrate(conn)
        for k, v in DEFAULT_SETTINGS.items():
            conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)", (k, v))
        row = conn.execute("SELECT value FROM settings WHERE key='admin_password'").fetchone()
        if not row or not row["value"]:
            conn.execute(
                "UPDATE settings SET value=? WHERE key='admin_password'",
                (hash_password(os.environ.get("JAI_ADMIN_PASSWORD", "admin")),),
            )
        conn.commit()
        _seed_from_env(conn)


def _migrate(conn: sqlite3.Connection) -> None:
    """为老库补齐后加的列（SQLite 无 IF NOT EXISTS 的 ADD COLUMN）。"""
    targets = {"logs": LOG_MIGRATIONS, **TABLE_MIGRATIONS}
    for table, columns in targets.items():
        try:
            existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        except Exception:
            continue
        if not existing:
            continue
        for column, ctype in columns.items():
            if column not in existing:
                try:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ctype}")
                except Exception:
                    pass
    conn.commit()


def _seed_from_env(conn: sqlite3.Connection) -> None:
    """首次启动且没有任何渠道时，尝试从环境变量初始化一个渠道。"""
    count = conn.execute("SELECT COUNT(*) AS c FROM channels").fetchone()["c"]
    if count:
        return
    base = os.environ.get("OPENAI_BASE_URL")
    key = os.environ.get("OPENAI_API_KEY")
    if base and key:
        conn.execute(
            "INSERT INTO channels(name,type,base_url,api_key,models,model_map,extra_headers,"
            "priority,timeout,stream_usage,enabled,remark,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("默认渠道(env)", "openai", base, key, "", "{}", "{}", 0, 300, 1, 1,
             "由 OPENAI_BASE_URL / OPENAI_API_KEY 自动创建", now_str()),
        )
        conn.commit()


def now_str() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())


# --------------------------------------------------------------------------- #
# 通用执行
# --------------------------------------------------------------------------- #
def execute(sql: str, params: tuple = ()) -> sqlite3.Cursor:
    with _lock:
        conn = _connect()
        cur = conn.execute(sql, params)
        conn.commit()
        return cur


def query(sql: str, params: tuple = ()) -> List[Dict[str, Any]]:
    with _lock:
        conn = _connect()
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]


def query_one(sql: str, params: tuple = ()) -> Optional[Dict[str, Any]]:
    rows = query(sql, params)
    return rows[0] if rows else None


# --------------------------------------------------------------------------- #
# 设置
# --------------------------------------------------------------------------- #
def get_setting(key: str, default: str = "") -> str:
    row = query_one("SELECT value FROM settings WHERE key=?", (key,))
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    execute(
        "INSERT INTO settings(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def all_settings() -> Dict[str, str]:
    return {r["key"]: r["value"] for r in query("SELECT key,value FROM settings")}


# --------------------------------------------------------------------------- #
# 密码 / 会话
# --------------------------------------------------------------------------- #
def hash_password(password: str, salt: Optional[str] = None) -> str:
    salt = salt or secrets.token_hex(8)
    digest = hashlib.sha256((salt + password).encode("utf-8")).hexdigest()
    return f"{salt}${digest}"


def verify_password(password: str) -> bool:
    stored = get_setting("admin_password")
    if "$" not in stored:
        return False
    salt, _ = stored.split("$", 1)
    return hmac.compare_digest(hash_password(password, salt), stored)


def create_session(days: int = 7) -> str:
    token = secrets.token_urlsafe(32)
    now = time.time()
    execute(
        "INSERT INTO sessions(token,created_at,expires_at) VALUES(?,?,?)",
        (token, now, now + days * 86400),
    )
    return token


def check_session(token: str) -> bool:
    if not token:
        return False
    row = query_one("SELECT expires_at FROM sessions WHERE token=?", (token,))
    if not row:
        return False
    if row["expires_at"] < time.time():
        execute("DELETE FROM sessions WHERE token=?", (token,))
        return False
    return True


def drop_session(token: str) -> None:
    execute("DELETE FROM sessions WHERE token=?", (token,))


def purge_sessions() -> None:
    execute("DELETE FROM sessions WHERE expires_at < ?", (time.time(),))


# --------------------------------------------------------------------------- #
# 渠道
# --------------------------------------------------------------------------- #
def list_channels(enabled_only: bool = False) -> List[Dict[str, Any]]:
    sql = "SELECT * FROM channels"
    if enabled_only:
        sql += " WHERE enabled=1"
    sql += " ORDER BY priority DESC, id ASC"
    rows = query(sql)
    for r in rows:
        r["model_map"] = _safe_json(r.get("model_map"), {})
        r["extra_headers"] = _safe_json(r.get("extra_headers"), {})
        r["model_list"] = [m.strip() for m in (r.get("models") or "").split(",") if m.strip()]
    return rows


def get_channel(cid: int) -> Optional[Dict[str, Any]]:
    r = query_one("SELECT * FROM channels WHERE id=?", (cid,))
    if r:
        r["model_map"] = _safe_json(r.get("model_map"), {})
        r["extra_headers"] = _safe_json(r.get("extra_headers"), {})
        r["model_list"] = [m.strip() for m in (r.get("models") or "").split(",") if m.strip()]
    return r


def create_channel(data: Dict[str, Any]) -> int:
    cur = execute(
        "INSERT INTO channels(name,type,base_url,api_key,models,model_map,extra_headers,"
        "priority,timeout,stream_usage,enabled,mask_mode,remark,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            data.get("name") or "未命名渠道",
            data.get("type") or "openai",
            (data.get("base_url") or "").rstrip("/"),
            data.get("api_key") or "",
            data.get("models") or "",
            json.dumps(data.get("model_map") or {}, ensure_ascii=False)
            if not isinstance(data.get("model_map"), str) else (data.get("model_map") or "{}"),
            json.dumps(data.get("extra_headers") or {}, ensure_ascii=False)
            if not isinstance(data.get("extra_headers"), str) else (data.get("extra_headers") or "{}"),
            int(data.get("priority") or 0),
            int(data.get("timeout") or 300),
            1 if data.get("stream_usage", 1) else 0,
            1 if data.get("enabled", 1) else 0,
            data.get("mask_mode") or "inherit",
            data.get("remark") or "",
            now_str(),
        ),
    )
    return int(cur.lastrowid)


def update_channel(cid: int, data: Dict[str, Any]) -> None:
    fields = []
    params: List[Any] = []
    simple = ["name", "type", "base_url", "api_key", "models", "remark", "mask_mode"]
    for f in simple:
        if f in data:
            val = data[f]
            if f == "base_url" and isinstance(val, str):
                val = val.rstrip("/")
            fields.append(f"{f}=?")
            params.append(val)
    if "model_map" in data:
        v = data["model_map"]
        fields.append("model_map=?")
        params.append(v if isinstance(v, str) else json.dumps(v or {}, ensure_ascii=False))
    if "extra_headers" in data:
        v = data["extra_headers"]
        fields.append("extra_headers=?")
        params.append(v if isinstance(v, str) else json.dumps(v or {}, ensure_ascii=False))
    for f in ["priority", "timeout", "stream_usage", "enabled"]:
        if f in data:
            fields.append(f"{f}=?")
            params.append(int(data[f] or 0))
    if not fields:
        return
    params.append(cid)
    execute(f"UPDATE channels SET {','.join(fields)} WHERE id=?", tuple(params))


def delete_channel(cid: int) -> None:
    execute("DELETE FROM channels WHERE id=?", (cid,))


# --------------------------------------------------------------------------- #
# API 密钥
# --------------------------------------------------------------------------- #
def list_keys() -> List[Dict[str, Any]]:
    return query("SELECT * FROM api_keys ORDER BY id DESC")


def get_key_by_value(key: str) -> Optional[Dict[str, Any]]:
    return query_one("SELECT * FROM api_keys WHERE key=?", (key,))


def create_key(data: Dict[str, Any]) -> int:
    key = data.get("key") or ("sk-jai-" + secrets.token_urlsafe(24))
    cur = execute(
        "INSERT INTO api_keys(name,key,enabled,remark,created_at) VALUES(?,?,?,?,?)",
        (data.get("name") or "默认密钥", key, 1 if data.get("enabled", 1) else 0,
         data.get("remark") or "", now_str()),
    )
    return int(cur.lastrowid)


def update_key(kid: int, data: Dict[str, Any]) -> None:
    fields, params = [], []
    for f in ["name", "remark", "key"]:
        if f in data:
            fields.append(f"{f}=?")
            params.append(data[f])
    if "enabled" in data:
        fields.append("enabled=?")
        params.append(1 if data["enabled"] else 0)
    if not fields:
        return
    params.append(kid)
    execute(f"UPDATE api_keys SET {','.join(fields)} WHERE id=?", tuple(params))


def delete_key(kid: int) -> None:
    execute("DELETE FROM api_keys WHERE id=?", (kid,))


def count_enabled_keys() -> int:
    row = query_one("SELECT COUNT(*) AS c FROM api_keys WHERE enabled=1")
    return int(row["c"]) if row else 0


def touch_key(key: str, cost: float = 0.0) -> None:
    execute("UPDATE api_keys SET last_used=?, used=used+? WHERE key=?", (now_str(), cost, key))


# --------------------------------------------------------------------------- #
# 日志
# --------------------------------------------------------------------------- #
LOG_FIELDS = [
    "created_at", "created_ts", "method", "path", "client_format", "upstream_format",
    "channel_id", "channel_name", "api_key_name", "client_ip", "request_model",
    "upstream_model", "stream", "status", "duration_ms", "retries", "prompt_tokens",
    "completion_tokens", "total_tokens", "upstream_url", "request_headers", "request_body",
    "upstream_request", "response_headers", "response_body", "stream_raw", "error",
    "mask_mode", "mask_session", "mask_summary", "mask_map", "mask_preview",
]


def touch_mask_session(session_id: str) -> None:
    execute("UPDATE mask_sessions SET updated_at=? WHERE session_id=?",
            (now_str(), session_id))


def insert_log(entry: Dict[str, Any]) -> int:
    values = [entry.get(f) for f in LOG_FIELDS]
    cur = execute(
        f"INSERT INTO logs({','.join(LOG_FIELDS)}) VALUES({','.join('?' * len(LOG_FIELDS))})",
        tuple(values),
    )
    return int(cur.lastrowid)


def update_log(log_id: int, entry: Dict[str, Any]) -> None:
    fields = [f for f in LOG_FIELDS if f in entry]
    if not fields:
        return
    params = [entry[f] for f in fields]
    params.append(log_id)
    execute(f"UPDATE logs SET {','.join(f + '=?' for f in fields)} WHERE id=?", tuple(params))


def list_logs(limit: int = 50, offset: int = 0, keyword: str = "", status: str = "",
              channel: str = "") -> Dict[str, Any]:
    where, params = [], []
    if keyword:
        where.append("(request_model LIKE ? OR path LIKE ? OR response_body LIKE ? OR error LIKE ?)")
        like = f"%{keyword}%"
        params += [like, like, like, like]
    if status == "ok":
        where.append("status >= 200 AND status < 300")
    elif status == "error":
        where.append("(status >= 400 OR status = 0)")
    if channel:
        where.append("channel_name = ?")
        params.append(channel)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    total = query_one(f"SELECT COUNT(*) AS c FROM logs{clause}", tuple(params))["c"]
    rows = query(
        "SELECT id,created_at,method,path,client_format,upstream_format,channel_name,"
        "request_model,upstream_model,stream,status,duration_ms,retries,prompt_tokens,"
        "completion_tokens,total_tokens,error,api_key_name,mask_mode,mask_summary "
        f"FROM logs{clause} ORDER BY id DESC LIMIT ? OFFSET ?",
        tuple(params + [limit, offset]),
    )
    return {"total": total, "items": rows}


def get_log(log_id: int) -> Optional[Dict[str, Any]]:
    return query_one("SELECT * FROM logs WHERE id=?", (log_id,))


def clear_logs() -> None:
    execute("DELETE FROM logs")


def purge_logs(days: int) -> int:
    if days <= 0:
        return 0
    cutoff = time.time() - days * 86400
    cur = execute("DELETE FROM logs WHERE created_ts < ?", (cutoff,))
    return cur.rowcount


def log_stats() -> Dict[str, Any]:
    total = query_one("SELECT COUNT(*) AS c, COALESCE(SUM(total_tokens),0) AS t, "
                      "COALESCE(SUM(prompt_tokens),0) AS p, COALESCE(SUM(completion_tokens),0) AS ct, "
                      "COALESCE(AVG(duration_ms),0) AS avg_ms FROM logs") or {}
    err = query_one("SELECT COUNT(*) AS c FROM logs WHERE status >= 400 OR status = 0") or {"c": 0}
    today = time.strftime("%Y-%m-%d")
    today_row = query_one(
        "SELECT COUNT(*) AS c, COALESCE(SUM(total_tokens),0) AS t FROM logs WHERE created_at LIKE ?",
        (today + "%",),
    ) or {"c": 0, "t": 0}
    return {
        "total_requests": total.get("c", 0),
        "total_tokens": total.get("t", 0),
        "prompt_tokens": total.get("p", 0),
        "completion_tokens": total.get("ct", 0),
        "avg_duration_ms": round(total.get("avg_ms", 0) or 0),
        "errors": err.get("c", 0),
        "today_requests": today_row.get("c", 0),
        "today_tokens": today_row.get("t", 0),
    }


def model_usage() -> List[Dict[str, Any]]:
    return query(
        "SELECT request_model AS model, COUNT(*) AS requests, "
        "COALESCE(SUM(total_tokens),0) AS tokens FROM logs "
        "GROUP BY request_model ORDER BY requests DESC LIMIT 20"
    )


def channel_usage() -> List[Dict[str, Any]]:
    return query(
        "SELECT channel_name AS channel, COUNT(*) AS requests, "
        "COALESCE(SUM(total_tokens),0) AS tokens, "
        "COALESCE(AVG(duration_ms),0) AS avg_ms FROM logs "
        "GROUP BY channel_name ORDER BY requests DESC LIMIT 20"
    )


def _safe_json(text: Any, default: Any) -> Any:
    if isinstance(text, (dict, list)):
        return text
    try:
        return json.loads(text or "")
    except Exception:
        return default
