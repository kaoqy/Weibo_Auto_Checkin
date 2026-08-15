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
from .anti_ban import AntiBanPolicy
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
        results_lock = threading.Lock()

        # 按账号指定的 socks 分组；不同 socks → 不同组（可并行）
        groups: dict[str, list[dict]] = {}
        group_order: list[str] = []
        for acc in accounts:
            proxy = (acc.get("proxy") or "").strip()
            key = proxy if proxy else "__direct__"   # 无指定归到直连组
            if key not in groups:
                groups[key] = []
                group_order.append(key)
            groups[key].append(acc)

        n_groups = len(group_order)
        parallel = n_groups > 1  # 多个不同 socks 才并行
        log.info("按代理分组：%d 组%s（%s）", n_groups,
                 "，并行签到" if parallel else "，依次签到",
                 "、".join(groups[k][0].get("name", "?") if groups[k] else k for k in group_order))

        def _process_one(acc: dict, idx_in_group: int, group_size: int, group_label: str):
            """处理单个账号签到。返回结果 dict 或 None（并发下由主线程记账）。"""
            # 防封等待（组内账号间）
            if idx_in_group > 0 or policy.should_wait():
                wait = policy.wait_between_accounts(idx_in_group + 1, group_size)
                if wait and group_label:
                    log.info("  [%s] 防封等待 %.0fs 后处理 %s", group_label, wait, acc.get("name"))

            raw_cookie = acc.get("cookie") or acc.get("cookie_raw") or ""
            cookie_dict = normalize_cookie(raw_cookie)
            proxy_url = acc.get("proxy") or None

            log.info("👤 [%s] 账号：%s", group_label, acc.get("name"))
            result = run_account_checkin(cookie_dict, opts, proxy_url=proxy_url)

            # 回写刷新后的 Cookie
            if result.get("cookie"):
                new_cookie_str = cookie_to_string(result["cookie"])
                database.update_account(acc["id"], {"cookie": new_cookie_str})
                if not acc.get("cookie_raw"):
                    database.update_account(acc["id"], {"cookie_raw": new_cookie_str})

            # 更新账号状态
            database.touch_account_result(
                acc["id"], result["status"],
                result.get("message") or result.get("status"),
            )

            entry = {
                "name": acc.get("name", "未命名账号"),
                "status": result["status"],
                "message": result.get("message", ""),
                "channel": result.get("channel", "direct") + (f"[{group_label}]" if group_label else ""),
                "total": result.get("total", 0),
                "success": result.get("success", 0),
                "fail": result.get("fail", 0),
                "results": result.get("results", []),
            }

            # 记录日志
            database.add_log({
                "account_id": acc["id"],
                "account_name": acc.get("name", "未命名账号"),
                "task_id": task_id,
                "status": result["status"],
                "channel": result.get("channel", "direct") + (f"[{group_label}]" if group_label else ""),
                "total": result.get("total", 0),
                "success": result.get("success", 0),
                "fail": result.get("fail", 0),
                "detail": result.get("results", []),
                "message": result.get("message", ""),
            })

            with results_lock:
                overall.append(entry)
                nonlocal_map["total"] += entry["total"]
                nonlocal_map["success"] += entry["success"]
                nonlocal_map["fail"] += entry["fail"]
                nonlocal_map["done"] += 1
                _current_run["accounts_done"] = nonlocal_map["done"]
                _current_run["progress"] = round(nonlocal_map["done"] / len(accounts) * 100)
            return entry

        # 用可变字典做并发计数器/合计
        nonlocal_map = {"total": 0, "success": 0, "fail": 0, "done": 0}

        def _run_group(accounts_in_group: list[dict], label: str):
            """依次处理组内账号。"""
            for i, acc in enumerate(accounts_in_group, start=0):
                _process_one(acc, i, len(accounts_in_group), label)

        if parallel:
            # 不同 socks 组并行（每个组一个线程）
            threads = []
            for label in group_order:
                t = threading.Thread(
                    target=_run_group, args=(groups[label], label),
                    daemon=True,
                )
                t.start()
                threads.append(t)
            for t in threads:
                t.join()
        else:
            # 只有一个组：依次签到
            label = group_order[0] if group_order else ""
            _run_group(groups.get(label, []), label)

        total, success, fail = nonlocal_map["total"], nonlocal_map["success"], nonlocal_map["fail"]

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
        # 兼容 5 段(标准) 或 6 段(青龙 style: 秒 分 时 日 月 周) cron
        parts = cron_expr.strip().split()
        fixed = cron_expr.strip()
        if len(parts) == 6:
            # 青龙/常见写法多一个「秒」段：0 10 0 * * * → 去掉秒 → 10 0 * * *
            fixed = " ".join(parts[1:])
            log.info("检测到 6 段 cron，已转为标准 5 段：%s", fixed)
        trigger = CronTrigger.from_crontab(fixed, timezone="Asia/Shanghai")
        scheduler.add_job(_fire_scheduled, trigger, id="weibo_checkin",
                          name=f"微博定时签到 ({fixed})",
                          misfire_grace_time=300, coalesce=True)
        log.info("已配置定时签到：%s", fixed)
    except Exception as exc:
        log.error("定时表达式无效 %r：%s", cron_expr, exc)


def start_scheduler() -> None:
    """启动调度器（幂等）。"""
    if not scheduler.running:
        scheduler.start()
    # 定期清理空闲扫码浏览器，释放内存（资源占用优化）
    if not scheduler.get_job("browser_sweep"):
        scheduler.add_job(
            _sweep_browser_idle, trigger="interval", minutes=3,
            id="browser_sweep", max_instances=1, coalesce=True,
        )
    reload_schedule()
    log.info("调度器已启动")


def _sweep_browser_idle():
    """清理空闲 Playwright 浏览器（释放内存）。

    说明：_sweep_idle_browser 现在是 async 且绑定主事件循环；APScheduler 在
    独立线程执行，跨线程直接 await playwright 会报 “cannot switch to a different
    thread”。因此空闲清理改由扫码请求自身在 check_qrcode/finalize_login 开头统一
    await _sweep_idle_browser() 完成，这里保留空占位（定时任务本身无害）。
    """
    pass


def stop_scheduler() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
        log.info("调度器已停止")
