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
    proxy: str = ""
    proxy_id: int | None = None      # v7.1：按代理 id 绑定（推荐）
    proxy_index: int | None = None   # 兼容旧字段
    remark: str = ""


class AccountUpdate(BaseModel):
    name: str | None = None
    cookie: str | None = None
    cookie_raw: str | None = None
    enabled: bool | None = None
    proxy: str | None = None
    proxy_id: int | None = None      # v7.1
    proxy_index: int | None = None   # 兼容旧字段
    remark: str | None = None


def _resolve_proxy(payload: dict, current: str = "") -> dict:
    """把前端传来的代理引用解析成服务端真实链接（v7.1）。

    规则：
    - 传 `proxy_id`（>0）→ 用数据库里的真实链接；`proxy_id=0` 表示改为直连。
    - 传 `proxy` 且带 `***`（打码回传）→ 视为“不修改”，保留原值。
    - 其余情况按原样使用（兼容老前端/脚本直接传完整链接）。
    """
    payload = dict(payload)
    pid = payload.pop("proxy_id", None)
    if pid is not None:
        if not pid:
            payload["proxy"] = ""
            return payload
        p = database.get_proxy(int(pid))
        if not p:
            raise HTTPException(400, "指定的代理不存在")
        payload["proxy"] = p.get("url") or database.build_proxy_url(p)
        return payload
    if "proxy" in payload and "***" in (payload.get("proxy") or ""):
        payload["proxy"] = current
    return payload


def _public(acc: dict) -> dict:
    """对外输出：不泄露 cookie 全文，也不泄露代理凭据（v7.1）。"""
    acc = dict(acc)
    cookie = acc.get("cookie") or acc.get("cookie_raw") or ""
    acc["cookie_length"] = len(cookie) if cookie else 0
    acc["cookie_preview"] = (cookie[:20] + "…") if len(cookie) > 20 else cookie
    raw_proxy = acc.get("proxy", "") or ""
    # 代理只对外给“打码链接 + 可读名称 + 引用 id”，不给明文密码。
    acc["proxy"] = database.mask_proxy_url(raw_proxy)
    matched = database.find_proxy_by_url(raw_proxy) if raw_proxy else None
    acc["proxy_id"] = matched["id"] if matched else None
    acc["proxy_label"] = database.proxy_display_label(matched) if matched else ""
    return acc


@router.get("")
def list_accounts(user: dict = Depends(auth.require_admin)):
    accounts = database.get_accounts()
    return [_public(a) for a in accounts]


@router.post("")
def create_account(data: AccountIn, user: dict = Depends(auth.require_admin)):
    payload = _resolve_proxy(data.model_dump())
    acc_id = database.add_account(payload)
    return _public(database.get_account(acc_id))


@router.get("/{account_id}")
def get_one(account_id: int, user: dict = Depends(auth.require_admin)):
    acc = database.get_account(account_id)
    if not acc:
        raise HTTPException(404, "账号不存在")
    return _public(acc)


@router.put("/{account_id}")
def update_one(account_id: int, data: AccountUpdate, user: dict = Depends(auth.require_admin)):
    existing = database.get_account(account_id)
    if not existing:
        raise HTTPException(404, "账号不存在")
    payload = {k: v for k, v in data.model_dump().items() if v is not None}
    payload = _resolve_proxy(payload, existing.get("proxy") or "")
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
    # v7.1：优先用账号自己绑定的代理校验，与实际签到链路保持一致
    proxy = (acc.get("proxy") or "").strip() or None
    channel = "socks" if proxy else "direct"
    if not proxy and opts.proxies:
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
