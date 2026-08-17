"""认证 API：登录、登出、当前用户、修改密码。"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from .. import auth, database

log = logging.getLogger("weibo.authapi")

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginIn(BaseModel):
    username: str
    password: str


class ChangePasswordIn(BaseModel):
    old_password: str
    new_password: str


class QrImportAccount(BaseModel):
    qrid: str
    name: str = ""


class InitIn(BaseModel):
    username: str
    password: str


@router.post("/login")
def login(data: LoginIn, request: Request, response: Response):
    if not auth.auth_enabled():
        return {"ok": True, "message": "登录未启用"}
    ip = auth.client_ip(request)
    # v7.2：域名公开在外，登录必需限流，否则可以无成本爆破
    locked = auth.login_lock_remaining(ip)
    if locked:
        log.warning("登录已锁定 ip=%s 剩余 %ss", ip, locked)
        raise HTTPException(
            status_code=429,
            detail=f"尝试次数过多，请 {locked} 秒后再试",
            headers={"Retry-After": str(locked)},
        )
    user = database.get_user_by_name(data.username.strip())
    if not user or not auth.verify_password(data.password, user["password_hash"]):
        lock = auth.record_login_failure(ip)
        if lock:
            log.warning("登录失败过多，已锁定 ip=%s %ss", ip, lock)
            raise HTTPException(
                status_code=429,
                detail=f"失败次数过多，已锁定 {lock} 秒",
                headers={"Retry-After": str(lock)},
            )
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    auth.reset_login_failures(ip)
    token = auth.generate_token()
    database.create_session(token, user["id"], auth.SESSION_TTL_HOURS)
    auth.set_session_cookie(response, token, request)
    return {"ok": True, "username": user["username"]}


@router.post("/logout")
def logout(request: Request, response: Response):
    token = request.cookies.get(auth.COOKIE_NAME, "")
    if token:
        database.delete_session(token)
    response.delete_cookie(auth.COOKIE_NAME, path="/")
    return {"ok": True}


@router.get("/me")
def me(user: dict = Depends(auth.login_required)):
    return {
        "ok": True,
        "username": user.get("username", ""),
        "anonymous": user.get("anonymous", False),
        "auth_enabled": auth.auth_enabled(),
    }


@router.post("/change-password")
def change_password(
    data: ChangePasswordIn,
    user: dict = Depends(auth.require_admin),
):
    db_user = database.get_user(user["id"])
    if not auth.verify_password(data.old_password, db_user["password_hash"]):
        raise HTTPException(status_code=400, detail="原密码错误")
    if len(data.new_password) < 6:
        raise HTTPException(status_code=400, detail="新密码至少 6 位")
    database.update_user_password(db_user["id"], auth.hash_password(data.new_password))
    # 密码修改后踢掉旧会话
    database.delete_user_sessions(user["id"])
    return {"ok": True, "message": "密码已修改，请重新登录"}


# ========================= 初始化（首次部署） =========================

@router.post("/init")
def init_first_user(data: InitIn, request: Request):
    """首次部署：设置管理员账号密码（仅当系统无任何用户时可用）。"""
    if database.count_users() > 0:
        raise HTTPException(status_code=400, detail="系统已初始化，不能重复设置")
    username = data.username.strip()
    if not username or len(username) < 2:
        raise HTTPException(status_code=400, detail="用户名至少 2 个字符")
    if len(data.password) < 6:
        raise HTTPException(status_code=400, detail="密码至少 6 位")
    database.create_user(username, auth.hash_password(data.password))
    database.set_settings({"admin_initialized": "1"})
    # 初始化后自动登录
    token = auth.generate_token()
    db_user = database.get_user_by_name(username)
    database.create_session(token, db_user["id"], auth.SESSION_TTL_HOURS)

    from fastapi.responses import RedirectResponse
    resp = RedirectResponse("/", status_code=302)
    auth.set_session_cookie(resp, token, request)
    return resp


@router.get("/needs-init")
def needs_init():
    """判断是否需要初始化（无任何用户时返回 true）。"""
    return {"needs_init": database.count_users() == 0}


# ========================= 扫码登录添加账号 =========================
@router.get("/qrcode")
async def qr_generate(user: dict = Depends(auth.require_admin)):
    """获取微博登录二维码（供添加新账号用）。"""
    from .. import weibo_login
    try:
        return await weibo_login.generate_qrcode()
    except Exception as exc:
        log.exception("获取二维码失败")
        raise HTTPException(status_code=502, detail=f"获取二维码失败：{exc}")


@router.get("/qrcode/check")
async def qr_check(qrid: str, user: dict = Depends(auth.require_admin)):
    """轮询二维码扫码状态。"""
    from .. import weibo_login
    return await weibo_login.check_qrcode(qrid)


@router.post("/qrcode/import")
async def qr_import_account(data: QrImportAccount, user: dict = Depends(auth.require_admin)):
    """扫码确认后，将得到的 Cookie 存为新账号。"""
    from .. import weibo_login
    result = await weibo_login.finalize_login(data.qrid)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("message", "获取 Cookie 失败"))
    cookies = result.get("cookies", {})
    cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items() if v)
    # 必须真实登录态（SUB+SCF/SSOLoginState/ALF 并存），区分 m.weibo.cn 发的假 SUB，
    # 避免导入后签到必然失败。
    if not weibo_login._is_real_login(cookies):
        raise HTTPException(status_code=400, detail="未获取到完整登录态，请重新扫码")
    uid = result.get("uid", "")
    name = (data.name or "").strip() or result.get("username") or (
        f"微博用户{uid}" if uid else "扫码账号"
    )
    acc_id = database.add_account({
        "name": name,
        "cookie_raw": cookie_str,
        "enabled": True,
        "remark": f"扫码登录 uid={uid}" if uid else "扫码登录",
    })
    return {"ok": True, "id": acc_id, "name": name, "cookie_length": len(cookie_str), "uid": uid}
