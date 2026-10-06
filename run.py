#!/usr/bin/env python
"""启动脚本。

用法:
    python run.py                      # 默认 127.0.0.1:8000
    python run.py --host 0.0.0.0 --port 8080
    python run.py --reload             # 开发模式
"""
from __future__ import annotations

import argparse
import os
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)


def main() -> None:
    parser = argparse.ArgumentParser(description="JAISafe LLM Gateway")
    parser.add_argument("--host", default=os.environ.get("JAI_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("JAI_PORT", "8000")))
    parser.add_argument("--reload", action="store_true", help="代码变更自动重载（开发用）")
    parser.add_argument("--workers", type=int, default=1,
                        help="工作进程数。SQLite 已开启 WAL，多进程可用；本地使用建议保持 1")
    args = parser.parse_args()

    try:
        import uvicorn  # noqa: F401
    except ImportError:
        print("缺少依赖，请先执行:  pip install -r requirements.txt", file=sys.stderr)
        raise SystemExit(1)

    import uvicorn

    banner = f"""
  JAISafe LLM Gateway
  ───────────────────────────────────────────
  WebUI / 管理后台 : http://{args.host}:{args.port}/
  OpenAI 兼容入口  : http://{args.host}:{args.port}/v1/chat/completions
  Anthropic 入口   : http://{args.host}:{args.port}/v1/messages
  Responses 入口   : http://{args.host}:{args.port}/v1/responses
  默认管理密码     : admin （首次登录后请及时修改）
  ───────────────────────────────────────────
"""
    print(banner)
    if args.reload:
        uvicorn.run("app.main:app", host=args.host, port=args.port, reload=True,
                    reload_dirs=[os.path.join(BASE_DIR, "app")])
    else:
        uvicorn.run("app.main:app", host=args.host, port=args.port, workers=args.workers)


if __name__ == "__main__":
    main()
