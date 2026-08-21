"""
微博超话签到管理面板 - FastAPI 入口。
启动时初始化数据库、调度器，托管前端静态文件。
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from . import database, scheduler
from .api import accounts as accounts_api
from .api import tasks as tasks_api

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("weibo.main")

STATIC_DIR = Path(__file__).resolve().parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    database.init_db()
    scheduler.start_scheduler()
    log.info("微博签到管理面板已就绪（数据库：%s）", database.DB_PATH)
    yield
    scheduler.stop_scheduler()


app = FastAPI(
    title="微博超话签到管理面板",
    description="微博超话批量签到 · 自动调度 · TG 通知 · 防封策略",
    version="1.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(accounts_api.router)
app.include_router(tasks_api.router)


@app.get("/api/health")
def health():
    return {
        "ok": True,
        "version": app.version,
        "time": database.get_setting("last_checkin_time", "never"),
    }


# 前端静态资源（构建好的单页）
if STATIC_DIR.exists():
    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")

# 为任务运行提供便捷的独立接口（供 run.py 调用）
from .scheduler import run_checkin  # noqa: E402


# 命令行直接跑一次签到（不启动 web）
def cli_checkin():
    database.init_db()
    summary = run_checkin("cli")
    # 打印汇总
    print("\n=== 签到汇总 ===")
    print(f"状态: {summary['status']}")
    print(f"账号: {summary.get('accounts', 0)}")
    print(f"超话: 总数 {summary.get('total', 0)} | 成功 {summary.get('success', 0)} | 失败 {summary.get('fail', 0)}")
    for acc in summary.get("detail", []):
        print(f"  - {acc['name']} [{acc['status']}] ({acc.get('channel')}): {acc.get('message', '')}")
    return summary
