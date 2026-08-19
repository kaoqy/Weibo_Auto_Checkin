"""账号管理 API。"""
from __future__ import annotations

from datetime import datetime

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


class AccountBatchImport(BaseModel):
    """批量导入账号。

    每行一个账号，支持以下格式：
      cookie（只用 cookie，自动命名为「微博账号1」「微博账号2」…）
      name|cookie（用 | 分隔名称和 cookie）
      name|Cookie|Cookie|…|Cookie（cookie 含多个键值对，带 | 分隔，前 N-1 个是 name）
    以 # 开头的行视为注释，自动跳过。
    """
    content: str


def _parse_account_line(line: str, index: int) -> dict | None:
    """解析单行账号数据，返回 account dict 或 None（跳过）。"""
    raw = line.strip()
    # 跳过空行和注释
    if not raw or raw.startswith("#"):
        return None
    # 按最后一个 | 分组：前面全是 name，最后一个是 cookie
    # 但如果 cookie 里本身含 |（key=value; key2=value2 格式），
    # 就不好办了。实际场景中 cookie 通常是 ; 分隔的字符串，| 极少出现。
    # 简单策略：先尝试 name|cookie 模式（name 非空），
    # 若 cookie 含 | 且 name 部分为空则退化为纯 cookie。
    parts = raw.split("|")
    if len(parts) >= 2 and parts[0].strip():
        name = parts[0].strip()
        cookie = "|".join(parts[1:]).strip()
        if cookie:
            return {"name": name, "cookie_raw": cookie}
    # 纯 cookie（无显式 name）
    cookie_only = raw
    return {
        "name": f"微博账号{index}",
        "cookie_raw": cookie_only,
    }


@router.post("/batch-import")
def batch_import_accounts(data: AccountBatchImport, user: dict = Depends(auth.require_admin)):
    """批量导入多行账号（每行一个）。"""
    lines = data.content.strip().split("\n")
    results = {"added": 0, "skipped": 0, "errors": []}
    counter = 1
    for line in lines:
        parsed = _parse_account_line(line, counter)
        if not parsed:
            continue
        cookie = parsed.get("cookie_raw", "").strip()
        if not cookie:
            results["skipped"] += 1
            results["errors"].append(f"第 {counter} 行：Cookie 为空，已跳过")
            counter += 1
            continue
        try:
            acc_id = database.add_account({
                "name": parsed.get("name", f"微博账号{results['added'] + 1}"),
                "cookie_raw": cookie,
                "enabled": True,
                "remark": "批量导入",
            })
            results["added"] += 1
        except Exception as exc:
            results["skipped"] += 1
            results["errors"].append(f"第 {counter} 行：{exc}")
        counter += 1
    return results


# ========================= JSON 导出/导入 =========================

@router.get("/export")
def export_accounts(user: dict = Depends(auth.require_admin)):
    """导出所有账号为 JSON 文件（不包含代理密码等敏感字段）。"""
    accounts = database.get_accounts()
    export_list = []
    for acc in accounts:
        export_list.append({
            "name": acc.get("name", ""),
            "cookie": acc.get("cookie") or acc.get("cookie_raw") or "",
            "enabled": bool(acc.get("enabled", 1)),
            "remark": acc.get("remark", ""),
        })
    return {
        "version": "1.0.1",
        "exported_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "count": len(export_list),
        "accounts": export_list,
    }


class AccountImportData(BaseModel):
    """从 JSON 文件导入账号。"""
    accounts: list[dict]
    overwrite: bool = False  # 同名账号是否覆盖


@router.post("/import")
def import_accounts(data: AccountImportData, user: dict = Depends(auth.require_admin)):
    """从 JSON 文件导入账号。"""
    # 收集现有账号名，用于去重
    existing_names = {a["name"] for a in database.get_accounts()}
    results = {"added": 0, "skipped": 0, "updated": 0, "errors": []}
    for idx, item in enumerate(data.accounts, start=1):
        name = (item.get("name") or "").strip()
        cookie = (item.get("cookie") or "").strip()
        if not cookie:
            results["skipped"] += 1
            results["errors"].append(f"第 {idx} 项：Cookie 为空，已跳过")
            continue
        if not name:
            name = f"微博账号{datetime.now().strftime('%H%M%S%f')[:10]}_{idx}"
        if name in existing_names and not data.overwrite:
            results["skipped"] += 1
            continue
        try:
            if name in existing_names and data.overwrite:
                # 查找并更新现有账号
                for acc in database.get_accounts():
                    if acc["name"] == name:
                        database.update_account(acc["id"], {
                            "cookie": cookie,
                            "cookie_raw": cookie,
                            "enabled": bool(item.get("enabled", True)),
                            "remark": item.get("remark", "从文件导入"),
                        })
                        results["updated"] += 1
                        break
            else:
                database.add_account({
                    "name": name,
                    "cookie": cookie,
                    "cookie_raw": cookie,
                    "enabled": bool(item.get("enabled", True)),
                    "remark": item.get("remark", "从文件导入"),
                })
                existing_names.add(name)
                results["added"] += 1
        except Exception as exc:
            results["skipped"] += 1
            results["errors"].append(f"第 {idx} 项：{exc}")
    return results


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
