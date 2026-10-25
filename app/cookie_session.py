"""Shared Weibo cookie validation, renewal and persistence.

Renewal relies on Weibo issuing fresh credentials while the session is still
valid. A permanently expired session must be signed in again by the user.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

import requests

from . import database, notifier
from .weibo_client import (
    HEADERS, CheckinOptions, NetworkError, cookie_to_string,
    cookie_changes, merge_refreshed_cookies, normalize_cookie, verify_cookie,
)

log = logging.getLogger("weibo.cookie_session")
_last_notification: dict[int, tuple[str, str]] = {}


def cookie_expiry(cookies: dict) -> str:
    """Return a display expiry when Weibo supplies an absolute/relative ALF."""
    try:
        value = int(cookies.get("ALF", ""))
        if value <= 0:
            return ""
        dt = datetime.fromtimestamp(value) if value > 1_000_000_000 else datetime.now() + timedelta(seconds=value)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError, OverflowError, OSError):
        return ""


def persist_cookie_result(account: dict, cookies: dict, valid: bool,
                          status: str, message: str = "") -> dict:
    """Atomically update credentials and observability, never exposing secrets."""
    now = database._now()
    updates = {
        "cookie_status": status,
        "cookie_checked_at": now,
        "cookie_error": message,
        "cookie_failure_count": 0 if valid else int(account.get("cookie_failure_count") or 0) + 1,
    }
    expiry = cookie_expiry(cookies)
    if expiry:
        updates["cookie_expires_at"] = expiry
    if valid:
        cookie = cookie_to_string(cookies)
        updates.update(cookie=cookie, cookie_raw=cookie, cookie_refreshed_at=now)
    database.update_account(account["id"], updates)
    return database.get_account(account["id"]) or {**account, **updates}


def refresh_account_cookie(account_id: int) -> dict:
    """Visit the login-aware config endpoint and save every renewed cookie."""
    account = database.get_account(account_id)
    if not account:
        return {"ok": False, "status": "missing", "message": "账号不存在"}
    original = normalize_cookie(account.get("cookie") or account.get("cookie_raw"))
    if not original:
        updated = persist_cookie_result(account, original, False, "expired", "Cookie 为空，请重新登录")
        return {"ok": False, "status": "expired", "message": "Cookie 为空，请重新登录", "account": updated}

    opts = CheckinOptions.from_settings(database.get_setting)
    proxy = (account.get("proxy") or "").strip() or None
    channel = "socks" if proxy else "direct"
    session = requests.Session()
    session.headers.update(HEADERS)
    previous_status = account.get("cookie_status") or "unknown"
    try:
        valid, _ = verify_cookie(session, original, channel=channel, proxy=proxy,
                                  force=opts.proxy_force, allow_fallback=opts.proxy_fallback)
        merged, _ = merge_refreshed_cookies(session, original)
        changed = cookie_changes(session, original)
        if not valid:
            updated = persist_cookie_result(account, merged, False, "expired", "登录态已失效，请重新扫码登录")
            result = {"ok": False, "valid": False, "status": "expired",
                      "message": "登录态已失效，请重新扫码登录", "changed": changed, "account": updated}
        else:
            status = "expiring" if _is_expiring(merged) else "healthy"
            updated = persist_cookie_result(account, merged, True, status)
            result = {"ok": True, "valid": True, "status": status,
                      "message": "Cookie 已续签" if changed else "登录态有效，已保活",
                      "changed": changed, "account": updated}
    except (NetworkError, requests.RequestException) as exc:
        # Network failures do not prove that a cookie is invalid.
        updated = persist_cookie_result(account, original, False, "network", str(exc)[:300])
        result = {"ok": False, "valid": None, "status": "network",
                  "message": "网络异常，将自动重试", "account": updated}
    except Exception as exc:
        log.exception("Cookie 保活异常 account=%s", account_id)
        updated = persist_cookie_result(account, original, False, "error", "保活执行异常")
        result = {"ok": False, "valid": None, "status": "error",
                  "message": "保活执行异常，请查看服务日志", "account": updated}

    state = result["status"]
    signature = (state, result["message"])
    if state in ("expired", "network") and _last_notification.get(account_id) != signature and previous_status != state:
        notifier.send_text_message(f"账号「{account.get('name', account_id)}」{result['message']}。")
        _last_notification[account_id] = signature
    elif result["ok"]:
        _last_notification.pop(account_id, None)
    return result


def _is_expiring(cookies: dict) -> bool:
    expiry = cookie_expiry(cookies)
    if not expiry:
        return False
    days = _setting_int("cookie_renew_before_days", 3, 1, 30)
    return datetime.strptime(expiry, "%Y-%m-%d %H:%M:%S") <= datetime.now() + timedelta(days=days)


def should_refresh(account: dict, now: float | None = None) -> bool:
    status = account.get("cookie_status") or "unknown"
    if status == "expired":
        return False  # Automatic retries cannot recover a revoked login.
    if status in ("network", "error"):
        count = min(int(account.get("cookie_failure_count") or 0), 6)
        delay = 3600 * (2 ** count)
    else:
        hours = _setting_int("cookie_refresh_interval_hours", 12, 1, 72)
        delay = hours * 3600
    if _is_expiring(normalize_cookie(account.get("cookie") or account.get("cookie_raw"))):
        delay = min(delay, 3600)
    checked = account.get("cookie_checked_at") or ""
    if not checked:
        return True
    try:
        elapsed = (datetime.now() - datetime.strptime(checked, "%Y-%m-%d %H:%M:%S")).total_seconds()
    except ValueError:
        return True
    return elapsed >= delay


def run_cookie_maintenance() -> dict:
    """Scheduled, sequential renewal to avoid bursts against Weibo."""
    if database.get_setting("cookie_auto_refresh", "1") != "1":
        return {"checked": 0, "healthy": 0, "failed": 0, "disabled": True}
    accounts = [a for a in database.get_accounts() if should_refresh(a)]
    results = [refresh_account_cookie(a["id"]) for a in accounts]
    healthy = sum(1 for r in results if r.get("ok"))
    log.info("Cookie 保活：检查 %d，成功 %d，失败 %d", len(results), healthy, len(results) - healthy)
    return {"checked": len(results), "healthy": healthy, "failed": len(results) - healthy}


def _setting_int(key: str, default: int, minimum: int, maximum: int) -> int:
    try:
        return max(minimum, min(maximum, int(database.get_setting(key, str(default)) or default)))
    except (ValueError, TypeError):
        return default
