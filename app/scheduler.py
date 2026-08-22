"""
签到调度与执行编排。
支持手动触发和定时（APScheduler cron）触发，统一走 run_checkin 流程。
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from . import database, notifier
from .anti_ban import AntiBanPolicy, node_rotation
from .weibo_client import CheckinOptions, cookie_to_string, normalize_cookie, run_account_checkin

log = logging.getLogger("weibo.scheduler")

scheduler = BackgroundScheduler(timezone="Asia/Shanghai")
_running_lock = threading.Lock()
_current_run = None          # 当前运行状态（供前端轮询）
_last_run_summary = None     # 最近一次任务汇总


def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def get_current_run() -> dict | None:
    """返回正在进行/最近一次的运行状态（可序列化）。"""
    if _current_run is not None:
        return _current_run
    return _last_run_summary


def get_schedule_status() -> dict:
    """返回定时签到的启用状态、Cron 表达式和下次执行时间。"""
    enabled = database.get_setting("schedule_enabled", "1") == "1"
    cron_expr = database.get_setting("schedule_cron", "0 7 * * *")
    job = scheduler.get_job("weibo_checkin")
    next_run = job.next_run_time.isoformat() if job and job.next_run_time else None
    return {
        "enabled": enabled,
        "cron": cron_expr,
        "scheduled": job is not None,
        "next_run_time": next_run,
        "timezone": "Asia/Shanghai",
    }


# ========================= 核心签到流程 =========================

def run_checkin(trigger_type: str = "manual") -> dict:
    """
    执行一次完整签到：遍历启用账号 → 防封等待 → 签到 → 记录日志 → 汇总。
    返回汇总 dict。线程安全（同一时刻只允许一个运行）。
    """
    global _current_run, _last_run_summary
    if not _running_lock.acquire(blocking=False):
        log.warning("已有签到任务在运行，跳过本次触发")
        return {"status": "skipped", "message": "已有任务在运行"}

    task_id = uuid.uuid4().hex[:12]
    database.create_task(task_id, trigger_type)
    started = _now_str()
    _current_run = {"task_id": task_id, "status": "running",
                    "trigger_type": trigger_type, "started_at": started,
                    "progress": 0, "accounts_total": 0, "accounts_done": 0}
    opts = CheckinOptions.from_settings(database.get_setting)
    policy = AntiBanPolicy.from_settings()

    log.info("开始签到任务 %s（%s）：%s", task_id, trigger_type, policy.describe())

    try:
        accounts = database.get_enabled_accounts()
        _current_run["accounts_total"] = len(accounts)
        if not accounts:
            summary = _finish(task_id, "success", "没有启用的账号", started,
                              {"accounts": 0, "total": 0, "success": 0,
                               "fail": 0, "detail": []})
            return summary

        overall = []
        total = success = fail = 0
        for idx, acc in enumerate(accounts, start=1):
            # 防封等待（首个账号也可能等待）
            if idx > 1 or policy.should_wait():
                policy.wait_between_accounts(idx, len(accounts))

            proxy_index = node_rotation(len(opts.proxies), acc.get("proxy_index", 0) or idx)
            raw_cookie = acc.get("cookie") or acc.get("cookie_raw") or ""
            cookie_dict = normalize_cookie(raw_cookie)

            log.info("👤 账号 %d/%d：%s", idx, len(accounts), acc.get("name"))
            result = run_account_checkin(cookie_dict, opts, proxy_index=proxy_index)

            # 回写刷新后的 Cookie（转成字符串存储）
            if result.get("cookie"):
                new_cookie_str = cookie_to_string(result["cookie"])
                database.update_account(acc["id"], {"cookie": new_cookie_str})
                # 若账号之前只有 cookie（非 raw），也同步更新 raw
                if not acc.get("cookie_raw"):
                    database.update_account(acc["id"], {"cookie_raw": new_cookie_str})

            # 更新账号状态
            database.touch_account_result(
                acc["id"], result["status"],
                result.get("message") or result.get("status"),
            )

            # 记录日志
            database.add_log({
                "account_id": acc["id"],
                "account_name": acc.get("name", "未命名账号"),
                "task_id": task_id,
                "status": result["status"],
                "channel": result.get("channel", "direct"),
                "total": result.get("total", 0),
                "success": result.get("success", 0),
                "fail": result.get("fail", 0),
                "detail": result.get("results", []),
                "message": result.get("message", ""),
            })

            overall.append({
                "name": acc.get("name", "未命名账号"),
                "status": result["status"],
                "message": result.get("message", ""),
                "channel": result.get("channel", "direct"),
                "total": result.get("total", 0),
                "success": result.get("success", 0),
                "fail": result.get("fail", 0),
                "results": result.get("results", []),
            })
            total += result.get("total", 0)
            success += result.get("success", 0)
            fail += result.get("fail", 0)

            _current_run["accounts_done"] = idx
            _current_run["progress"] = round(idx / len(accounts) * 100)

        # 汇总
        status = "failed" if fail > 0 and success == 0 else ("partial" if fail > 0 else "success")
        summary = _finish(task_id, status, "完成", started, {
            "accounts": len(overall), "total": total, "success": success,
            "fail": fail, "detail": overall,
            "time": _now_str(), "task_id": task_id, "trigger_type": trigger_type,
        })

        # TG 通知
        if database.get_setting("tg_enabled", "0") == "1":
            notifier.send_checkin_report(summary)
        return summary
    except Exception as exc:
        log.exception("签到任务异常")
        summary = _finish(task_id, "failed", f"异常：{exc}", started, {
            "accounts": 0, "total": 0, "success": 0, "fail": 0, "detail": [],
            "time": _now_str(), "task_id": task_id, "trigger_type": trigger_type,
            "error": str(exc),
        })
        return summary
    finally:
        _running_lock.release()


def _finish(task_id: str, status: str, message: str, started: str,
            summary: dict) -> dict:
    global _current_run, _last_run_summary
    finished = _now_str()
    database.finish_task(task_id, status, message)
    summary["status"] = status
    summary["finished_at"] = finished
    summary["message"] = message
    database.set_settings({"last_checkin_time": finished})
    _current_run = None
    _last_run_summary = summary
    return summary


# ========================= 定时调度 =========================

def _fire_scheduled():
    log.info("定时任务触发，开始签到")
    run_checkin(trigger_type="schedule")


def reload_schedule() -> None:
    """根据当前设置重建定时任务。"""
    global scheduler
    # 清空现有任务
    for job in list(scheduler.get_jobs()):
        job.remove()

    enabled = database.get_setting("schedule_enabled", "1") == "1"
    cron_expr = database.get_setting("schedule_cron", "0 7 * * *")
    if not enabled:
        log.info("定时签到已关闭")
        return

    try:
        # APScheduler 的 from_crontab 严格支持标准 5 段 Cron。
        if len(cron_expr.strip().split()) != 5:
            raise ValueError("Cron 表达式必须为 5 段")
        trigger = CronTrigger.from_crontab(cron_expr, timezone="Asia/Shanghai")
        scheduler.add_job(_fire_scheduled, trigger, id="weibo_checkin",
                          name=f"微博定时签到 ({cron_expr})",
                          misfire_grace_time=300, coalesce=True)
        log.info("已配置定时签到：%s", cron_expr)
    except Exception as exc:
        log.error("定时表达式无效 %r：%s", cron_expr, exc)


def start_scheduler() -> None:
    """启动调度器（幂等）。"""
    if not scheduler.running:
        scheduler.start()
    reload_schedule()
    log.info("调度器已启动")


def stop_scheduler() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
        log.info("调度器已停止")
