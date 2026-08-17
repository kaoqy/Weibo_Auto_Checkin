"""
认证模块：密码哈希、会话 token、登录/登出逻辑、FastAPI 认证中间件。
默认管理员账号在首次启动时创建（不设密码则生成随机密码并打印）。
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import threading
import time
from datetime import datetime

from fastapi import Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from . import database

COOKIE_NAME = "wcm_token"
SESSION_TTL_HOURS = 168  # 7 天


# ========================= 密码哈希（PBKDF2） =========================

def hash_password(password: str, salt: str | None = None) -> str:
    """返回 'salt$hash' 格式的 PBKDF2-SHA256 哈希。"""
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), salt.encode(), iterations=120_000
    )
    return f"{salt}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt, expected = stored.split("$", 1)
    except ValueError:
        return False
    dk = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), salt.encode(), iterations=120_000
    )
    return hmac.compare_digest(dk.hex(), expected)


# ========================= 默认管理员 =========================

def ensure_default_admin() -> None:
    """首次启动若无用户且未显式配置，则创建默认管理员。
    若设置 WCM_ADMIN_PASSWORD 用其创建；否则不建（由初始化页设置）。
    """
    if database.count_users() > 0:
        return
    password = os.environ.get("WCM_ADMIN_PASSWORD", "").strip()
    if not password:
        # 未提供密码：留待初始化页设置，不自动创建
        return
    username = os.environ.get("WCM_ADMIN_USER", "admin").strip() or "admin"
    database.create_user(username, hash_password(password))
    database.set_settings({"admin_initialized": "1"})
    print(f"  👤 默认管理员已创建: {username}（密码来自 WCM_ADMIN_PASSWORD）")


# ========================= Token =========================

def generate_token() -> str:
    return secrets.token_urlsafe(32)


def auth_enabled() -> bool:
    return database.get_setting("auth_enabled", "1") == "1"


# ========================= Cookie 安全（v7.2） =========================

def is_secure_request(request: Request) -> bool:
    """判断当前请求是否走 HTTPS（含反向代理场景）。

    面板通常部署在 nginx/openresty 反代后面，应用自己看到的是明文 HTTP，
    必须靠 `X-Forwarded-Proto` 判断真实协议，否则永远加不上 Secure 标记。
    """
    proto = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
    if proto:
        return proto == "https"
    if (request.headers.get("x-forwarded-ssl") or "").lower() == "on":
        return True
    return request.url.scheme == "https"


def set_session_cookie(response, token: str, request: Request | None = None) -> None:
    """统一设置会话 Cookie：HttpOnly + SameSite=Lax，HTTPS 下追加 Secure。

    Secure 只在确认是 HTTPS 时加 —— 若在纯 HTTP 部署下无条件加上，
    浏览器会直接丢弃 Cookie，导致谁都登不进去。
    """
    secure = is_secure_request(request) if request is not None else False
    response.set_cookie(
        COOKIE_NAME, token,
        max_age=SESSION_TTL_HOURS * 3600,
        httponly=True, samesite="lax", path="/",
        secure=secure,
    )


# ========================= 登录失败限流（v7.2） =========================

LOGIN_MAX_FAILS = 5           # 窗口内允许的失败次数
LOGIN_WINDOW_SECONDS = 300    # 统计窗口
LOGIN_LOCK_SECONDS = 300      # 触发后锁定时长

_login_fails: dict[str, list[float]] = {}
_login_locks: dict[str, float] = {}
_login_lock_guard = threading.Lock()


def client_ip(request: Request) -> str:
    """取真实客户端 IP（反代场景下用 X-Forwarded-For 第一段）。"""
    xff = request.headers.get("x-forwarded-for") or ""
    if xff:
        return xff.split(",")[0].strip()
    real = request.headers.get("x-real-ip")
    if real:
        return real.strip()
    return request.client.host if request.client else "unknown"


def login_lock_remaining(ip: str) -> int:
    """该 IP 剩余锁定秒数；0 表示未锁定。"""
    now = time.time()
    with _login_lock_guard:
        until = _login_locks.get(ip, 0)
        if until > now:
            return int(until - now) + 1
        if until:
            _login_locks.pop(ip, None)
    return 0


def record_login_failure(ip: str) -> int:
    """记录一次登录失败；触发锁定则返回锁定秒数，否则 0。"""
    now = time.time()
    with _login_lock_guard:
        hits = [t for t in _login_fails.get(ip, []) if now - t < LOGIN_WINDOW_SECONDS]
        hits.append(now)
        _login_fails[ip] = hits
        if len(hits) >= LOGIN_MAX_FAILS:
            _login_locks[ip] = now + LOGIN_LOCK_SECONDS
            _login_fails[ip] = []
            return LOGIN_LOCK_SECONDS
    return 0


def reset_login_failures(ip: str) -> None:
    """登录成功后清掉该 IP 的失败计数。"""
    with _login_lock_guard:
        _login_fails.pop(ip, None)
        _login_locks.pop(ip, None)


# ========================= FastAPI 依赖 =========================

def get_current_user(request: Request) -> dict:
    """依赖注入：从 cookie 取 token 校验用户。未启用登录时放行。"""
    if not auth_enabled():
        return {"id": 0, "username": "local", "anonymous": True}
    token = request.cookies.get(COOKIE_NAME, "")
    user = database.get_session_user(token)
    if not user:
        raise HTTPException(status_code=401, detail="未登录或会话已过期")
    return user


def login_required(user: dict = Depends(get_current_user)) -> dict:
    return user


def require_admin(user: dict = Depends(get_current_user)) -> dict:
    """管理操作需要登录（登录用户即管理员）。"""
    if user.get("anonymous"):
        raise HTTPException(status_code=401, detail="需要登录")
    return user
