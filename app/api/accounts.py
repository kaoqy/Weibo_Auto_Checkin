"""账号管理 API。"""
from __future__ import annotations

import requests

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .. import auth, database

router = APIRouter(prefix="/api/accounts", tags=["accounts"])


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


def _public(acc: dict) -> dict:
    # 隐藏完整 cookie 中的敏感字段？这里保留，但前端仅展示长度/状态。
    acc = dict(acc)
    cookie = acc.get("cookie") or acc.get("cookie_raw") or ""
    acc["cookie_length"] = len(cookie) if cookie else 0
    acc["cookie_preview"] = (cookie[:20] + "…") if len(cookie) > 20 else cookie
    return acc


@router.get("")
def list_accounts(user: dict = Depends(auth.require_admin)):
    accounts = database.get_accounts()
    return [_public(a) for a in accounts]


@router.post("")
def create_account(data: AccountIn, user: dict = Depends(auth.require_admin)):
    acc_id = database.add_account(data.model_dump())
    return {"id": acc_id, **data.model_dump()}


@router.get("/{account_id}")
def get_one(account_id: int, user: dict = Depends(auth.require_admin)):
    acc = database.get_account(account_id)
    if not acc:
        raise HTTPException(404, "账号不存在")
    return _public(acc)


@router.put("/{account_id}")
def update_one(account_id: int, data: AccountUpdate, user: dict = Depends(auth.require_admin)):
    if not database.get_account(account_id):
        raise HTTPException(404, "账号不存在")
    payload = {k: v for k, v in data.model_dump().items() if v is not None}
    database.update_account(account_id, payload)
    return _public(database.get_account(account_id))


@router.delete("/{account_id}")
def delete_one(account_id: int, user: dict = Depends(auth.require_admin)):
    if not database.delete_account(account_id):
        raise HTTPException(404, "账号不存在")
    return {"ok": True}


@router.post("/{account_id}/verify")
def verify_one(account_id: int, user: dict = Depends(auth.require_admin)):
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
    session = requests.Session()
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
