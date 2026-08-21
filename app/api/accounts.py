"""账号管理 API。"""
from __future__ import annotations

import json
import secrets
import time

import requests
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import database

router = APIRouter(prefix="/api/accounts", tags=["accounts"])

# B 方案：直接调用微博扫码登录接口，不启动 Playwright、Chromium 或 Selenium。
# 登录会话仅短暂保存在内存中，服务重启后自动失效。
_qr_sessions: dict[str, dict] = {}
_QR_SESSION_TTL = 300


def _cleanup_qr_sessions() -> None:
    now = time.time()
    expired = [
        session_id for session_id, item in _qr_sessions.items()
        if now - item["created_at"] > _QR_SESSION_TTL
    ]
    for session_id in expired:
        _qr_sessions.pop(session_id, None)


def _new_weibo_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Linux; Android 13; Mobile) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0 Mobile Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://passport.weibo.com/",
    })
    return session


def _cookie_string(session: requests.Session) -> str:
    return "; ".join(
        f"{cookie.name}={cookie.value}" for cookie in session.cookies
    )


def _parse_jsonp_response(response: requests.Response) -> dict:
    """解析微博接口返回的 JSON 或 JSONP 数据。"""
    text = response.text.strip()
    if not text:
        raise ValueError("微博接口返回空响应")
    if text.startswith("{"):
        return response.json()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("微博接口返回的数据格式无效")
    return json.loads(text[start:end + 1])


class AccountIn(BaseModel):
    name: str = "未命名账号"
    cookie: str = ""
    cookie_raw: str = ""
    enabled: bool = True
    proxy_index: int = 0
    remark: str = ""


class AccountUpdate(BaseModel):
    name: str | None = None
    cookie: str | None = None
    cookie_raw: str | None = None
    enabled: bool | None = None
    proxy_index: int | None = None
    remark: str | None = None


class QrLoginFinish(BaseModel):
    session_id: str
    name: str = "扫码登录账号"
    enabled: bool = True
    proxy_index: int = 0
    remark: str = "扫码登录"


@router.post("/qr/start")
def start_qr_login():
    """创建微博扫码登录会话，返回可直接展示的二维码图片地址。"""
    _cleanup_qr_sessions()
    session = _new_weibo_session()
    try:
        response = session.get(
            "https://login.sina.com.cn/sso/qrcode/image",
            params={
                "entry": "weibo",
                "size": 180,
                "callback": "STK_1",
            },
            timeout=15,
        )
        response.raise_for_status()
        payload = _parse_jsonp_response(response)
    except (requests.RequestException, ValueError) as exc:
        raise HTTPException(502, f"获取微博登录二维码失败：{exc}") from exc

    data = payload.get("data") or {}
    qrid = data.get("qrid")
    image = data.get("image")
    if not qrid or not image:
        raise HTTPException(502, "微博未返回有效二维码")
    if image.startswith("//"):
        image = "https:" + image

    session_id = secrets.token_urlsafe(24)
    _qr_sessions[session_id] = {
        "created_at": time.time(),
        "session": session,
        "qrid": qrid,
        "alt": data.get("alt", ""),
        "login_url": "",
    }
    return {
        "session_id": session_id,
        "image": image,
        "expires_in": _QR_SESSION_TTL,
        "status": "waiting",
        "message": "请使用微博客户端扫码",
    }


@router.get("/qr/{session_id}/status")
def get_qr_login_status(session_id: str):
    """轮询扫码状态；确认登录后在会话中保存微博 Cookie。"""
    _cleanup_qr_sessions()
    item = _qr_sessions.get(session_id)
    if not item:
        raise HTTPException(404, "二维码会话不存在或已过期")

    session: requests.Session = item["session"]
    try:
        response = session.get(
            "https://login.sina.com.cn/sso/qrcode/check",
            params={
                "entry": "weibo",
                "qrid": item["qrid"],
                "callback": "STK_" + str(int(time.time() * 1000)),
            },
            timeout=15,
        )
        response.raise_for_status()
        payload = _parse_jsonp_response(response)
    except (requests.RequestException, ValueError) as exc:
        raise HTTPException(502, f"查询扫码状态失败：{exc}") from exc

    retcode = str(payload.get("retcode", ""))
    data = payload.get("data") or {}
    if retcode in {"50114001", "50114002"}:
        return {
            "status": "waiting",
            "message": "等待扫码" if retcode == "50114001" else "已扫码，请在手机上确认",
        }
    if retcode != "20000000":
        return {
            "status": "expired" if retcode == "50114003" else "failed",
            "message": payload.get("msg") or "二维码已失效，请重新获取",
        }

    login_url = data.get("url") or data.get("alt") or item.get("alt")
    if login_url:
        try:
            session.get(login_url, timeout=15, allow_redirects=True)
        except requests.RequestException as exc:
            raise HTTPException(502, f"完成微博登录失败：{exc}") from exc

    cookie = _cookie_string(session)
    if not cookie:
        return {"status": "failed", "message": "登录成功但未能获取 Cookie"}
    item["cookie"] = cookie
    item["login_url"] = login_url or ""
    return {
        "status": "confirmed",
        "message": "扫码登录成功，可以保存账号",
        "cookie_length": len(cookie),
    }


@router.post("/qr/finish")
def finish_qr_login(data: QrLoginFinish):
    """把扫码登录取得的 Cookie 保存为签到账号。"""
    _cleanup_qr_sessions()
    item = _qr_sessions.get(data.session_id)
    if not item:
        raise HTTPException(404, "二维码会话不存在或已过期")
    cookie = item.get("cookie")
    if not cookie:
        raise HTTPException(409, "扫码登录尚未完成")

    account = {
        "name": data.name.strip() or "扫码登录账号",
        "cookie": cookie,
        "cookie_raw": cookie,
        "enabled": data.enabled,
        "proxy_index": data.proxy_index,
        "remark": data.remark,
    }
    account_id = database.add_account(account)
    _qr_sessions.pop(data.session_id, None)
    return {"id": account_id, "name": account["name"], "ok": True}


def _public(acc: dict) -> dict:
    # 隐藏完整 cookie 中的敏感字段？这里保留，但前端仅展示长度/状态。
    acc = dict(acc)
    cookie = acc.get("cookie") or acc.get("cookie_raw") or ""
    acc["cookie_length"] = len(cookie) if cookie else 0
    acc["cookie_preview"] = (cookie[:20] + "…") if len(cookie) > 20 else cookie
    return acc


@router.get("")
def list_accounts():
    accounts = database.get_accounts()
    return [_public(a) for a in accounts]


@router.post("")
def create_account(data: AccountIn):
    acc_id = database.add_account(data.model_dump())
    return {"id": acc_id, **data.model_dump()}


@router.get("/{account_id}")
def get_one(account_id: int):
    acc = database.get_account(account_id)
    if not acc:
        raise HTTPException(404, "账号不存在")
    return _public(acc)


@router.put("/{account_id}")
def update_one(account_id: int, data: AccountUpdate):
    if not database.get_account(account_id):
        raise HTTPException(404, "账号不存在")
    payload = {k: v for k, v in data.model_dump().items() if v is not None}
    database.update_account(account_id, payload)
    return _public(database.get_account(account_id))


@router.delete("/{account_id}")
def delete_one(account_id: int):
    if not database.delete_account(account_id):
        raise HTTPException(404, "账号不存在")
    return {"ok": True}


@router.post("/{account_id}/verify")
def verify_one(account_id: int):
    """校验账号 Cookie 是否有效（只校验，不签到）。"""
    from ..weibo_client import CheckinOptions, normalize_cookie, verify_cookie
    import requests as _req  # noqa

    acc = database.get_account(account_id)
    if not acc:
        raise HTTPException(404, "账号不存在")
    cookie = normalize_cookie(acc.get("cookie") or acc.get("cookie_raw") or "")
    if not cookie:
        return {"valid": False, "message": "Cookie 为空"}
    opts = CheckinOptions.from_settings(database.get_setting)
    proxy = None
    channel = "direct"
    if opts.proxies:
        idx = (acc.get("proxy_index") or 0) % len(opts.proxies)
        proxy = opts.proxies[idx]
        channel = "socks"
    session = __import__("requests").Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) "
                      "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148",
        "Referer": "https://m.weibo.cn/",
        "Accept": "application/json, text/plain, */*",
        "X-Requested-With": "XMLHttpRequest",
    })
    try:
        logged_in, _st = verify_cookie(
            session, cookie, channel=channel, proxy=proxy,
            force=opts.proxy_force, allow_fallback=opts.proxy_fallback,
        )
    except Exception as exc:
        return {"valid": False, "message": f"请求异常：{exc}"}
    return {"valid": logged_in,
            "message": "Cookie 有效" if logged_in else "Cookie 无效或已过期",
            "channel": channel}
