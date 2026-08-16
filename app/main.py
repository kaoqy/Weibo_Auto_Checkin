"""
微博超话签到管理面板 - FastAPI 入口。
启动时初始化数据库、调度器，托管前端静态文件。
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import auth, database, scheduler
from .api import accounts as accounts_api
from .api import auth as auth_api
from .api import proxies as proxies_api
from .api import tasks as tasks_api

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("weibo.main")

STATIC_DIR = Path(__file__).resolve().parent / "static"

# 不需要登录的公开路径
PUBLIC_API_PREFIXES = (
    "/api/auth/login", "/api/auth/me", "/api/health",
    "/api/auth/init", "/api/auth/needs-init",
)
PUBLIC_STATIC = ("/login.html", "/style.css", "/favicon.ico")


@asynccontextmanager
async def lifespan(app: FastAPI):
    database.init_db()
    auth.ensure_default_admin()
    scheduler.start_scheduler()
    log.info("微博签到管理面板已就绪（数据库：%s）", database.DB_PATH)
    yield
    scheduler.stop_scheduler()
    # 释放扫码浏览器内存
    try:
        from . import weibo_login
        await weibo_login.close_browser()
    except Exception:
        pass


app = FastAPI(
    title="微博超话签到管理面板",
    description="微博超话批量签到 · 自动调度 · TG 通知 · 防封策略",
    version="5.5.0",
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
app.include_router(auth_api.router)
app.include_router(proxies_api.router)


@app.get("/api/health")
def health():
    return {
        "ok": True,
        "version": app.version,
        "time": database.get_setting("last_checkin_time", "never"),
        "auth_enabled": auth.auth_enabled(),
    }


@app.middleware("http")
async def auth_guard(request: Request, call_next):
    """未启用登录则放行；启用时保护 API 和页面，未登录重定向/401；
    无任何用户时（首次部署）引导到初始化页。"""
    path = request.url.path
    # 首次部署：无用户则强制到 init 页（除 init 相关接口和静态资源外）
    needs_init = database.count_users() == 0
    if needs_init:
        if path in ("/init.html", "/style.css", "/api/auth/init", "/api/auth/needs-init", "/api/health", "/favicon.ico"):
            return await call_next(request)
        if path.startswith("/api/"):
            from fastapi.responses import JSONResponse
            return JSONResponse(status_code=403, content={"detail": "系统未初始化"})
        return RedirectResponse("/init.html", status_code=302)

    # 登录相关与健康检查放行
    if path.startswith(PUBLIC_API_PREFIXES):
        return await call_next(request)
    # 静态资源：登录页/样式放行
    if path in PUBLIC_STATIC or path == "/login.html":
        return await call_next(request)
    # 静态文件（app.js 等）需要登录才能访问页面依赖；但登录页本身要能加载
    if not auth.auth_enabled():
        return await call_next(request)

    token = request.cookies.get(auth.COOKIE_NAME, "")
    user = auth.database.get_session_user(token) if token else None

    if request.url.path.startswith("/api/"):
        # API：未登录返回 401 JSON
        if not user:
            from fastapi.responses import JSONResponse
            return JSONResponse(status_code=401, content={"detail": "未登录或会话已过期"})
        return await call_next(request)

    # 页面：未登录重定向到登录页
    if not user:
        return RedirectResponse("/login.html", status_code=302)
    return await call_next(request)


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
