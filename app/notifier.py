"""
Telegram 通知推送模块。
支持通过 Bot Token + Chat ID 发送文本消息，并记录到数据库。
"""
from __future__ import annotations

import logging

import requests

from . import database

log = logging.getLogger("weibo.notifier")

TELEGRAM_API = "https://api.telegram.org"


def _get_tg_config() -> tuple[str, str, bool]:
    token = database.get_setting("tg_bot_token", "").strip()
    user_id = database.get_setting("tg_user_id", "").strip()
    enabled = database.get_setting("tg_enabled", "0") == "1"
    return token, user_id, enabled


def send_telegram(text: str, title: str = "微博签到") -> bool:
    """发送 TG 消息。成功返回 True，失败记录到数据库并返回 False。"""
    token, user_id, enabled = _get_tg_config()
    if not enabled:
        log.info("TG 推送未启用，跳过")
        return False
    if not token or not user_id:
        error = "TG 未配置 Bot Token 或 Chat ID"
        database.add_notification(title, text, False, error)
        log.warning(error)
        return False

    full = f"{title}\n{text}" if title and not text.startswith(title) else text
    try:
        resp = requests.post(
            f"{TELEGRAM_API}/bot{token}/sendMessage",
            data={
                "chat_id": user_id,
                "text": full,
                "disable_web_page_preview": True,
            },
            timeout=20,
        )
        ok = resp.status_code == 200 and resp.json().get("ok") is True
        error = "" if ok else f"HTTP {resp.status_code} {resp.text[:200]}"
        database.add_notification(title, text, ok, error)
        return ok
    except Exception as exc:
        database.add_notification(title, text, False, str(exc))
        log.exception("TG 推送异常")
        return False


def send_checkin_report(task_summary: dict) -> bool:
    """发送一份签到汇总报告。task_summary 由调度器构建。"""
    lines = [
        "🕒 " + task_summary.get("time", ""),
        "━━━━━━━━━━━━━━━━━━",
        f"👤 账号：{task_summary.get('accounts', 0)} 个",
        f"📋 超话：{task_summary.get('total', 0)} 个 ｜ "
        f"✅ 成功：{task_summary.get('success', 0)} ｜ "
        f"❌ 失败：{task_summary.get('fail', 0)}",
    ]
    accounts = task_summary.get("detail", [])
    for idx, acc in enumerate(accounts, start=1):
        icon = {"success": "✅", "partial": "⚠️", "failed": "❌"}.get(
            acc.get("status"), "ℹ️"
        )
        signed = [r.get("name") for r in acc.get("results", [])
                  if r.get("success") and r.get("message") != "今日已签到"]
        already = [r.get("name") for r in acc.get("results", [])
                   if r.get("success") and r.get("message") == "今日已签到"]
        fails = [r for r in acc.get("results", []) if not r.get("success")]
        lines.append("")
        lines.append(f"{icon} {idx}. {acc.get('name', '未知')}")
        if signed:
            lines.append("   🎉 本次签到：" + "、".join(signed))
        if already:
            lines.append("   ☑️ 今日已签：" + "、".join(already))
        if fails:
            lines.append("   ❌ 失败：" + "、".join(
                f"{r.get('name')}（{r.get('message', '未知')}）" for r in fails))
        if not acc.get("results"):
            lines.append(f"   ℹ️ {acc.get('message', '')}")

    all_ok = task_summary.get("fail", 0) == 0 and not any(
        a.get("status") == "failed" for a in accounts
    )
    title = "✅ 微博超话签到完成" if all_ok else "⚠️ 微博超话签到有异常"
    return send_telegram("\n".join(lines), title)


def send_text_message(text: str) -> bool:
    """发送一条自定义 TG 消息（测试用）。"""
    return send_telegram(text, "微博签到")
