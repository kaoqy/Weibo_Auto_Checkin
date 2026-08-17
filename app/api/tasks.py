"""任务与调度 API。"""
from __future__ import annotations

import threading

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .. import auth, database, scheduler, notifier

router = APIRouter(prefix="/api", tags=["tasks"])


class CronIn(BaseModel):
    cron: str = "0 7 * * *"
    enabled: bool = True


class RunAccountsIn(BaseModel):
    account_ids: list[int]


@router.post("/checkin/run-accounts")
def run_selected_accounts(data: RunAccountsIn, user: dict = Depends(auth.require_admin)):
    """手动签到指定账号（可多选）。立即返回，后台线程执行。"""
    ids = data.account_ids
    if not ids:
        raise HTTPException(400, "请至少选择一个账号")
    # 校验账号存在
    acc = database.get_account(ids[0])
    if not acc:
        raise HTTPException(404, "账号不存在")

    def _worker():
        scheduler.run_checkin("manual", account_ids=ids)
    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    return {"ok": True, "message": f"已为 {len(ids)} 个账号启动手动签到", "count": len(ids)}


@router.post("/checkin/run")
def run_now(user: dict = Depends(auth.require_admin)):
    """手动触发一次签到（立即返回，签到在后台线程执行）。"""
    def _worker():
        scheduler.run_checkin("manual")
    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    return {"ok": True, "message": "签到任务已启动"}


@router.get("/checkin/status")
def checkin_status(user: dict = Depends(auth.require_admin)):
    run = scheduler.get_current_run()
    if run is None:
        return {"running": False}
    running = run.get("status") == "running"
    return {
        "running": running,
        "run": run,
    }


@router.get("/checkin/last")
def last_run(user: dict = Depends(auth.require_admin)):
    return {"summary": scheduler._last_run_summary}


@router.get("/schedule/next")
def schedule_next(user: dict = Depends(auth.require_admin)):
    """下一次定时签到时间（v7.1），面板用于展示倒计时。"""
    return scheduler.next_run_info()


@router.get("/tasks")
def list_tasks(limit: int = 20, user: dict = Depends(auth.require_admin)):
    return database.get_tasks(limit)


@router.get("/logs")
def list_logs(limit: int = 50, account_id: int | None = None, user: dict = Depends(auth.require_admin)):
    return database.get_logs(limit, account_id)


@router.get("/logs/grouped")
def list_logs_grouped(limit: int = 20, user: dict = Depends(auth.require_admin)):
    """按日期分组、单次执行归并的日志。"""
    return database.get_logs_grouped(limit)


@router.get("/logs/stats")
def log_stats(user: dict = Depends(auth.require_admin)):
    return database.get_log_stats()


@router.get("/logs/trend")
def log_trend(days: int = 7, user: dict = Depends(auth.require_admin)):
    """近 N 天签到趋势（仪表盘迷你图表）。"""
    days = max(1, min(days, 30))
    return database.get_daily_trend(days)


@router.delete("/logs")
def clear_all_logs(user: dict = Depends(auth.require_admin)):
    """清空全部签到日志。"""
    removed = database.clear_logs()
    return {"ok": True, "removed": removed}


@router.post("/logs/purge")
def purge_logs(days: int | None = None, user: dict = Depends(auth.require_admin)):
    """按保留天数清理旧日志（不传则用设置值）。"""
    if days is None:
        days = int(database.get_setting("log_retention_days", "30") or 0)
    removed = database.purge_old_logs(days)
    return {"ok": True, "removed": removed, "days": days}


@router.get("/quote")
def daily_quote(refresh: bool = False, user: dict = Depends(auth.require_admin)):
    """每日一言（前端仪表盘展示 / TG 推送复用）。"""
    from .. import hitokoto
    if refresh:
        hitokoto.clear_cache()
    text, source = hitokoto.fetch_quote()
    return {"text": text, "source": source}


@router.post("/notify/quote")
def push_quote(user: dict = Depends(auth.require_admin)):
    """单独推送一条每日一言到 TG。"""
    ok = notifier.send_daily_quote()
    return {"ok": ok}


@router.get("/settings")
def get_settings(user: dict = Depends(auth.require_admin)):
    return database.get_settings()


@router.post("/settings")
def update_settings(values: dict, user: dict = Depends(auth.require_admin)):
    # 只允许更新已知 key
    known = {
        "tg_bot_token", "tg_user_id", "tg_enabled",
        "tg_quote_enabled", "tg_only_on_change", "tg_silent",
        "schedule_enabled", "schedule_cron",
        "anti_ban_enabled", "anti_ban_wait_min", "anti_ban_wait_max",
        "anti_ban_window_hour",
        "proxies", "proxy_force", "proxy_fallback",
        "checkin_delay_min", "checkin_delay_max",
        "log_retention_days",
    }
    updates = {k: v for k, v in values.items() if k in known}
    database.set_settings(updates)
    # 若涉及调度，重建定时任务
    if "schedule_enabled" in updates or "schedule_cron" in updates:
        scheduler.reload_schedule()
    return database.get_settings()


@router.get("/proxies/geo")
def proxies_geo(user: dict = Depends(auth.require_admin)):
    """检测配置的所有 SOCKS5 代理的归属地。
    返回 [{url, ok, country, country_code, region, city, display}]
    """
    from .. import proxy_geo
    from ..weibo_client import parse_proxies

    urls = parse_proxies(database.get_setting("proxies", ""))
    result = []
    for u in urls:
        info = proxy_geo.detect(u)
        result.append({
            "url": u,
            "ok": info.get("ok", False),
            "country": info.get("country", ""),
            "country_code": info.get("country_code", ""),
            "region": info.get("region", ""),
            "city": info.get("city", ""),
            "display": proxy_geo.display(u),
            "short": proxy_geo.short_label(u),
        })
    return result


@router.post("/settings/reload-schedule")
def reload_schedule(user: dict = Depends(auth.require_admin)):
    scheduler.reload_schedule()
    return {"ok": True}


@router.post("/notify/test")
def test_notify(user: dict = Depends(auth.require_admin)):
    """发送一条测试 TG 消息。"""
    ok = notifier.send_text_message("✅ 这是一条来自微博签到管理面板的测试消息")
    return {"ok": ok}


@router.get("/notifications")
def notifications(limit: int = 10, user: dict = Depends(auth.require_admin)):
    return database.get_notifications(limit)
