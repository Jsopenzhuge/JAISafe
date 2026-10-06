"""针对「已运行实例」的联调检查（HTTP 层，非进程内）。

设计原则：**不破坏既有数据**。
  * 只创建 / 清理带 `__livetest__` 前缀的渠道与密钥
  * 不改动全局设置——先快照，测完原样恢复
  * 不清空日志；用「日志号是否增长」判断是否落库
  * 需要独立脱敏会话时用 `X-Mask-Session` 请求头，不碰其它会话

用法:
    python tests/mock_upstream.py          # 终端 A（默认 127.0.0.1:18080）
    python run.py --port 18000             # 终端 B
    python tests/live_check.py             # 终端 C

建议用一个独立的实例（JAI_DATA_DIR 指向临时目录）跑，避免污染正在使用的配置。
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict, List, Optional, Tuple

import httpx

BASE = os.environ.get("JAI_BASE", "http://127.0.0.1:18000")
MOCK = os.environ.get("JAI_MOCK", "http://127.0.0.1:18080")
PASSWORD = os.environ.get("JAI_ADMIN_PASSWORD", "admin")
PREFIX = "__livetest__"
SESSION = PREFIX + "session"

PASSED = 0
FAILED: List[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASSED
    if cond:
        PASSED += 1
    else:
        FAILED.append(f"{name} {detail}")
        print(f"  [FAIL] {name} {detail}")


def sse_text(raw: str, fmt: str) -> str:
    parts: List[str] = []
    for line in raw.split("\n"):
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            d = json.loads(payload)
        except Exception:
            continue
        if fmt == "openai":
            for ch in d.get("choices") or []:
                parts.append((ch.get("delta") or {}).get("content") or "")
        elif fmt == "openai_completions":
            for ch in d.get("choices") or []:
                parts.append(ch.get("text") or "")
        elif fmt == "anthropic":
            if d.get("type") == "content_block_delta":
                dl = d.get("delta") or {}
                if dl.get("type") == "text_delta":
                    parts.append(dl.get("text") or "")
        elif fmt == "openai_responses":
            if d.get("type") == "response.output_text.delta":
                parts.append(d.get("delta") or "")
    return "".join(parts)


def newest_log_id(client: httpx.Client) -> int:
    items = client.get("/admin/api/logs", params={"limit": 1}).json().get("items") or []
    return int(items[0]["id"]) if items else 0


def cleanup(client: httpx.Client) -> None:
    for ch in client.get("/admin/api/channels").json()["items"]:
        if str(ch["name"]).startswith(PREFIX):
            client.delete(f"/admin/api/channels/{ch['id']}")
    for k in client.get("/admin/api/keys").json()["items"]:
        if str(k["name"]).startswith(PREFIX):
            client.delete(f"/admin/api/keys/{k['id']}")
    client.delete(f"/admin/api/mask/sessions/{SESSION}")


def main() -> int:
    client = httpx.Client(base_url=BASE, timeout=90.0, follow_redirects=True)

    print("== WebUI 静态资源 ==")
    r = client.get("/")
    check("首页 200", r.status_code == 200, str(r.status_code))
    check("首页包含挂载点", 'id="app"' in r.text and "/static/app.js" in r.text)
    check("app.js 200", client.get("/static/app.js").status_code == 200)
    check("style.css 200", client.get("/static/style.css").status_code == 200)

    print("== 管理端登录 ==")
    check("未登录 session 为 false",
          client.get("/admin/api/session").json().get("authenticated") is False)
    check("未登录 overview 401", client.get("/admin/api/overview").status_code == 401)
    check("错误密码 401",
          client.post("/admin/api/login", json={"password": "wrong-password"}).status_code == 401)
    check("正确密码 200",
          client.post("/admin/api/login", json={"password": PASSWORD}).status_code == 200)
    check("登录后 session 为 true",
          client.get("/admin/api/session").json().get("authenticated") is True)

    # ---- 快照既有配置，测试结束后恢复 -------------------------------- #
    saved_settings = client.get("/admin/api/settings").json()
    saved_mask = client.get("/admin/api/mask").json()["settings"]
    baseline_logs = newest_log_id(client)
    baseline_stats = client.get("/admin/api/overview").json()["stats"]["total_requests"]

    try:
        print("== 准备测试资源（__livetest__ 前缀） ==")
        cleanup(client)
        specs = [
            ("openai", "openai", f"{PREFIX}m-openai"),
            ("anthropic", "anthropic", f"{PREFIX}m-anthropic"),
            ("responses", "openai_responses", f"{PREFIX}m-responses"),
        ]
        for name, ctype, model in specs:
            r = client.post("/admin/api/channels", json={
                "name": PREFIX + name, "type": ctype, "base_url": MOCK,
                "api_key": "sk-livetest", "models": model,
                "model_map": {model: "livetest-up"}, "priority": 0,
                "timeout": 60, "stream_usage": 1, "enabled": 1, "mask_mode": "inherit",
            })
            check(f"创建渠道 {name}", r.status_code == 200, r.text[:200])
        r = client.post("/admin/api/keys", json={"name": PREFIX + "key",
                                                 "key": "sk-livetest-key"})
        check("创建测试密钥", r.status_code == 200, r.text[:200])
        headers = {"Authorization": "Bearer sk-livetest-key"}

        print("== 渠道连通性 ==")
        for ch in client.get("/admin/api/channels").json()["items"]:
            if str(ch["name"]).startswith(PREFIX):
                d = client.post(f"/admin/api/channels/{ch['id']}/test").json()
                check(f"测试渠道 {ch['name']}", d.get("ok") is True, str(d)[:200])

        print("== 四种协议（非流式 / 流式） ==")
        cases: List[Tuple[str, str, Dict[str, Any]]] = [
            ("openai", "/v1/chat/completions", {
                "model": f"{PREFIX}m-openai",
                "messages": [{"role": "user", "content": "hi"}]}),
            ("anthropic", "/v1/messages", {
                "model": f"{PREFIX}m-anthropic", "max_tokens": 64,
                "messages": [{"role": "user", "content": "hi"}]}),
            ("openai_responses", "/v1/responses", {
                "model": f"{PREFIX}m-responses", "input": "hi"}),
            ("openai_completions", "/v1/completions", {
                "model": f"{PREFIX}m-openai", "prompt": "hi"}),
        ]
        for fmt, path, body in cases:
            r = client.post(path, json=body, headers=headers)
            check(f"{fmt} 非流式 200", r.status_code == 200, r.text[:200])
            payload = dict(body)
            payload["stream"] = True
            with client.stream("POST", path, json=payload, headers=headers) as resp:
                raw = "".join(resp.iter_text())
            check(f"{fmt} 流式 200", resp.status_code == 200, raw[:200])
            check(f"{fmt} 流式内容", "你好" in sse_text(raw, fmt), raw[:200])

        r = client.get("/v1/models", headers=headers)
        check("/v1/models 200", r.status_code == 200)
        check("/v1/models 含测试模型",
              any(m["id"] == f"{PREFIX}m-openai" for m in r.json().get("data", [])))

        print("== 推理等级 ==")
        r = client.post("/v1/chat/completions", headers=headers, json={
            "model": f"{PREFIX}m-openai", "reasoning_effort": "high",
            "messages": [{"role": "user", "content": "hi"}]})
        check("reasoning_effort 200", r.status_code == 200, r.text[:200])
        check("返回 reasoning_content",
              "让我先" in (r.json()["choices"][0]["message"].get("reasoning_content") or ""),
              r.text[:200])
        r = client.post("/v1/messages", headers=headers, json={
            "model": f"{PREFIX}m-openai", "max_tokens": 40000,
            "thinking": {"type": "enabled", "budget_tokens": 16384},
            "messages": [{"role": "user", "content": "hi"}]})
        check("thinking 转回 thinking 块",
              any(b.get("type") == "thinking" for b in r.json().get("content") or []),
              r.text[:200])

        print("== 脱敏 ==")
        mask_headers = dict(headers)
        mask_headers["X-Mask-Session"] = SESSION
        check("保存脱敏设置（测试用）", client.post("/admin/api/mask/settings", json={
            "mask_mode": "enforce",
            "mask_workspace_roots": r"C:\livetest\ws",
            "mask_include_home": "0", "mask_store_map": "1",
            "mask_session_source": "header",
        }).status_code == 200)
        preview = client.post("/admin/api/mask/preview", json={
            "text": r"读取 C:\livetest\ws\a\b.pdf，token 是 ghp_" + "Z" * 36}).json()
        check("试跑命中路径", any(h["kind"] == "path" for h in preview.get("hits", [])),
              str(preview)[:200])
        check("试跑命中凭证",
              any(h["kind"] == "credential" for h in preview.get("hits", [])),
              str(preview.get("hits"))[:200])

        before = newest_log_id(client)
        probe = "ECHO:0:路径 " + r"C:\livetest\ws\a.py" + " 令牌 ghp_" + "Z" * 36
        r = client.post("/v1/chat/completions", headers=mask_headers, json={
            "model": f"{PREFIX}m-openai",
            "messages": [{"role": "user", "content": probe}]})
        check("脱敏请求 200", r.status_code == 200, r.text[:200])
        restored = r.json()["choices"][0]["message"]["content"]
        check("客户端拿回真值",
              r"\a.py" in restored and "ghp_" + "Z" * 36 in restored,
              repr(restored[:200]))
        after = newest_log_id(client)
        check("产生了新日志", after > before, f"{before} -> {after}")
        detail = client.get(f"/admin/api/logs/{after}").json()
        check("日志含脱敏模式", detail.get("mask_mode") == "enforce",
              str(detail.get("mask_mode")))
        check("日志含句柄映射", "WORKSPACE_ROOT" in (detail.get("mask_map") or ""),
              str(detail.get("mask_map"))[:200])
        check("上游请求体已脱敏",
              "ghp_" + "Z" * 36 not in (detail.get("upstream_request") or "")
              and "[WORKSPACE_ROOT_1]" in (detail.get("upstream_request") or ""),
              str(detail.get("upstream_request"))[:200])
        lookup = client.get("/admin/api/mask/lookup",
                            params={"handle": "[WORKSPACE_ROOT_1]",
                                    "session_id": SESSION}).json()
        check("按句柄反查真值", lookup.get("raw") == r"C:\livetest\ws", str(lookup))

        print("== 日志与统计 ==")
        logs = client.get("/admin/api/logs", params={"limit": 5}).json()
        check("日志列表非空", logs["total"] >= 1, str(logs["total"]))
        stats = client.get("/admin/api/overview").json()["stats"]
        check("统计随请求增长", stats["total_requests"] > baseline_stats,
              f"{baseline_stats} -> {stats['total_requests']}")

        print("== 设置读写 ==")
        check("保存设置", client.post("/admin/api/settings", json={
            "log_retention_days": saved_settings.get("log_retention_days", "7")}
        ).status_code == 200)
    finally:
        print("== 清理测试资源并恢复配置 ==")
        try:
            cleanup(client)
            client.post("/admin/api/settings", json=saved_settings)
            client.post("/admin/api/mask/settings", json=saved_mask)
            still = [c["name"] for c in client.get("/admin/api/channels").json()["items"]
                     if str(c["name"]).startswith(PREFIX)]
            left = [k["name"] for k in client.get("/admin/api/keys").json()["items"]
                    if str(k["name"]).startswith(PREFIX)]
            check("测试渠道已清理", not still, str(still))
            check("测试密钥已清理", not left, str(left))
            check("脱敏设置已恢复",
                  client.get("/admin/api/mask").json()["settings"].get("mask_mode")
                  == saved_mask.get("mask_mode"),
                  f"{saved_mask.get('mask_mode')} -> "
                  f"{client.get('/admin/api/mask').json()['settings'].get('mask_mode')}")
        except Exception as exc:  # noqa: BLE001
            FAILED.append(f"cleanup failed: {exc}")
            print(f"  [FAIL] cleanup failed: {exc}")

    print("== 退出登录 ==")
    check("logout 200", client.post("/admin/api/logout").status_code == 200)
    check("退出后未鉴权", client.get("/admin/api/overview").status_code == 401)

    print(f"\n通过: {PASSED}  失败: {len(FAILED)}")
    for f in FAILED:
        print("  - " + f)
    return 0 if not FAILED else 1


if __name__ == "__main__":
    sys.exit(main())
