"""
认证模块：密码哈希、会话 token、登录/登出逻辑、FastAPI 认证中间件。
默认管理员账号在首次启动时创建（不设密码则生成随机密码并打印）。
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
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
