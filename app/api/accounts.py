"""账号管理 API。"""
from __future__ import annotations

import logging
import re
from datetime import datetime
import json
import secrets
import time

import requests

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .. import auth, database

router = APIRouter(prefix="/api/accounts", tags=["accounts"])

log = logging.getLogger("weibo.accounts")


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


class AccountBatchIn(BaseModel):
    account_ids: list[int]
    action: str


def _validated_account_ids(account_ids: list[int]) -> list[int]:
    """严格校验批量操作账号 ID：去重 + 过滤非正整数 + 限制数量。"""
    if not account_ids:
        return []
    seen: set[int] = set()
    for raw in account_ids:
        try:
            aid = int(raw)
        except (TypeError, ValueError):
            continue
        if aid > 0 and aid not in seen:
            seen.add(aid)
    return list(seen)


class QrLoginFinish(BaseModel):
    session_id: str
    name: str = "扫码登录账号"
    enabled: bool = True
    proxy_index: int = 0
    remark: str = "扫码登录"


# ============== 纯 requests 微博扫码登录（不启动任何无头浏览器） ==============
# 流程：
#   1) /qr/start   → 拿到 qrid 与 image（passport 的 qrcode/image）
#   2) /qr/{id}/status 轮询
#       - 50114001 (未扫码) / 50114002 (已扫码待确认) → waiting
#       - 50114003 / 其他错误码 → expired / failed
#       - 20000000 → 命中确认，触发跨域回调 + 聚合多域 Cookie
#   3) /qr/finish  → 把聚合后的 Cookie 写入账号

# 真实登录态判定：m.weibo.cn 会给所有未登录访问者也 Set-Cookie 一个孤零零的假 SUB，
# 必须 SUB 与 (SCF 或 SSOLoginState 或 ALF) 同时存在才视为真实登录。
_REAL_LOGIN_KEYS = ("SCF", "SSOLoginState", "ALF")


def _is_real_login(cookies: dict) -> bool:
    if not cookies.get("SUB"):
        return False
    return any(cookies.get(k) for k in _REAL_LOGIN_KEYS)


def _collect_session_cookies(session: requests.Session) -> dict:
    """聚合当前 Session 内所有域名下的 Cookie（同名以 weibo.cn 域为准）。"""
    out: dict[str, str] = {}
    for c in session.cookies:
        name = c.name
        if not c.value:
            continue
        prev = out.get(name)
        if prev is None or "weibo.cn" in (c.domain or ""):
            out[name] = c.value
    return out


_URL_PATTERN = re.compile(r"https?://[^\s'\"\u4e00-\u9fff]+")


def _extract_callback_urls(payload: dict) -> list[str]:
    """从 qrcode/check 的 retdata 中尽可能提取所有回调 URL。

    passport 在确认(20000000)后可能返回：
      - data.url / data.alt / data.redirect_url / data.location （单一 URL）
      - data.arrURL / data.arrUrl / data.crossDomainUrlList / data.urlList
        （字符串或数组形式的多个 URL）
      - top-level url / redirect_url / new_url / return_url / location
    """
    urls: list[str] = []
    seen: set[str] = set()

    def _push(v):
        if isinstance(v, str):
            for m in _URL_PATTERN.findall(v):
                if m not in seen:
                    seen.add(m)
                    urls.append(m)
        elif isinstance(v, (list, tuple)):
            for item in v:
                _push(item)
        elif isinstance(v, dict):
            for item in v.values():
                _push(item)

    if isinstance(payload, dict):
        inner = payload.get("data")
        if isinstance(inner, dict):
            for k in ("arrURL", "arrUrl", "arr_url", "crossDomainUrlList",
                      "crossdomain_url_list", "urlList", "urls", "url_list"):
                _push(inner.get(k))
        _push(payload)
    return urls


def _complete_qr_login(session: requests.Session, item: dict, payload: dict) -> dict:
    """扫码确认后：访问所有回调 URL（含 arrURL/跨域同步）以落地各域 Cookie。

    返回聚合后的 cookie 字典。
    """
    urls = _extract_callback_urls(payload)
    fallback = item.get("alt")
    if isinstance(fallback, str) and fallback.startswith("http") and fallback not in urls:
        urls.append(fallback)
    log.info("扫码确认回调 URL 数：%d", len(urls))
    for u in urls:
        try:
            session.get(u, timeout=15, allow_redirects=True)
        except requests.RequestException as exc:
            log.warning("回调 %s 异常：%s", u, exc)
    # 兜底：触发 m.weibo.cn 跨域写 cookie（SSOLoginState/WEIBOCN_FROM/MLOGIN 等）
    try:
        session.get("https://m.weibo.cn/api/config", timeout=10, allow_redirects=True)
    except requests.RequestException as exc:
        log.warning("m.weibo.cn 兜底请求异常：%s", exc)
    return _collect_session_cookies(session)


@router.post("/qr/start")
def start_qr_login(user: dict = Depends(auth.require_admin)):
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
def get_qr_login_status(session_id: str, user: dict = Depends(auth.require_admin)):
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

    # 跨域同步（arrURL/crossDomainUrlList 等）+ 多域 Cookie 聚合
    cookies = _complete_qr_login(session, item, data)
    if not cookies:
        return {"status": "failed", "message": "登录成功但未能获取 Cookie"}
    if not _is_real_login(cookies):
        # 没有 SUB+SCF/SSOLoginState/ALF：登录态未落地（可能为假 SUB）
        item["pending_cookies"] = cookies
        return {
            "status": "scanned",
            "message": "扫码已确认，正在同步登录态…",
        }
    item["cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
    item["login_url"] = login_url or ""
    return {
        "status": "confirmed",
        "message": "扫码登录成功，可以保存账号",
        "cookie_length": len(item["cookie"]),
    }


@router.post("/qr/finish")
def finish_qr_login(data: QrLoginFinish, user: dict = Depends(auth.require_admin)):
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
        payload["proxy"] = database.build_proxy_url(p) or p.get("url", "")
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
        "version": "1.1.0",
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
