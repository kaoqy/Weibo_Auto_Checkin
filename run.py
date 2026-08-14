#!/usr/bin/env python3
"""
微博超话签到管理面板 - 启动脚本

用法:
    python run.py                 # 启动 Web 管理面板（默认 0.0.0.0:8000）
    python run.py --port 9000     # 指定端口
    python run.py --host 127.0.0.1
    python run.py checkin         # 命令行直接跑一次签到（不启动 Web）
"""
from __future__ import annotations

import argparse
import os
import sys

# 确保能 import app
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def main():
    parser = argparse.ArgumentParser(description="微博超话签到管理面板")
    parser.add_argument("command", nargs="?", default="web",
                        help="web=启动面板, checkin=直接跑一次签到")
    parser.add_argument("--host", default=os.environ.get("APP_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("APP_PORT", "8000")))
    parser.add_argument("--reload", action="store_true", help="开发热重载")
    args = parser.parse_args()

    if args.command == "checkin":
        from app.main import cli_checkin
        return cli_checkin()

    import uvicorn
    print("=" * 56)
    print("  微博超话签到管理面板")
    print(f"  地址: http://{args.host}:{args.port}")
    print("=" * 56)
    uvicorn.run(
        "app.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
