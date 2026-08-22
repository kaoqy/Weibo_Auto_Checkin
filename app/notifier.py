"""
Telegram 通知推送模块。
支持通过 Bot Token + Chat ID 发送文本消息，并记录到数据库。
"""
from __future__ import annotations

import logging

import requests

from . import database
from . import hitokoto

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
    silent = database.get_setting("tg_silent", "0") == "1"
    try:
        resp = requests.post(
            f"{TELEGRAM_API}/bot{token}/sendMessage",
            data={
                "chat_id": user_id,
                "text": full,
                "disable_web_page_preview": True,
                "disable_notification": "true" if silent else "false",
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
    accounts = task_summary.get("detail", [])
    all_ok = task_summary.get("fail", 0) == 0 and not any(
        a.get("status") == "failed" for a in accounts
    )

    # 仅异常推送模式：全部成功时不打扰
    if all_ok and database.get_setting("tg_only_on_change", "0") == "1":
        log.info("TG 仅异常推送已开启且本次全部成功，跳过推送")
        return False

    total = task_summary.get("total", 0)
    success = task_summary.get("success", 0)
    fail = task_summary.get("fail", 0)
    rate = round(success / total * 100) if total else 0
    trigger = task_summary.get("trigger_type", "")
    trigger_label = {"schedule": "⏰ 定时触发", "manual": "👆 手动触发"}.get(trigger, "")

    lines = [
        "🕒 " + task_summary.get("time", ""),
    ]
    if trigger_label:
        lines.append(trigger_label)
    lines += [
        "━━━━━━━━━━━━━━━━━━",
        f"👤 账号：{task_summary.get('accounts', 0)} 个",
        f"📋 超话：{total} 个 ｜ ✅ 成功：{success} ｜ ❌ 失败：{fail}",
        f"📈 成功率：{rate}%  {_progress_bar(rate)}",
    ]
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

    # 每日一言（可关闭）
    if database.get_setting("tg_quote_enabled", "1") == "1":
        lines.append("")
        lines.append("━━━━━━━━━━━━━━━━━━")
        lines.append(hitokoto.format_quote())

    title = "✅ 微博超话签到完成" if all_ok else "⚠️ 微博超话签到有异常"
    return send_telegram("\n".join(lines), title)


def _progress_bar(percent: int, width: int = 10) -> str:
    """用方块画一个简单进度条。"""
    filled = max(0, min(width, round(percent / 100 * width)))
    return "█" * filled + "░" * (width - filled)


def send_text_message(text: str) -> bool:
    """发送一条自定义 TG 消息（测试用）。"""
    if database.get_setting("tg_quote_enabled", "1") == "1":
        text = text + "\n\n" + hitokoto.format_quote()
    return send_telegram(text, "微博签到")


def send_daily_quote() -> bool:
    """单独推送一条每日一言。"""
    return send_telegram(hitokoto.format_quote(), "🌟 每日一言")
