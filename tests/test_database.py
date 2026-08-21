"""测试数据库层。使用独立临时数据库，避免污染真实数据。"""
import os
import sys
import tempfile
from pathlib import Path

import pytest

# 让临时数据库生效：先设置环境/路径
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app.database as db  # noqa: E402


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    """把数据库指向临时文件，并初始化。"""
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(db, "DB_PATH", db_path)
    # 重置线程本地连接
    db._local.conn = None
    db.init_db()
    yield db
    db._local.conn = None


# ---------- 设置 ----------

def test_init_seeds_defaults(tmp_db):
    assert tmp_db.get_setting("schedule_cron") == "0 7 * * *"
    assert tmp_db.get_setting("anti_ban_enabled") == "1"
    assert tmp_db.get_setting("tg_enabled") == "0"


def test_set_and_get_settings(tmp_db):
    tmp_db.set_settings({"tg_bot_token": "abc:123", "tg_user_id": "456"})
    assert tmp_db.get_setting("tg_bot_token") == "abc:123"
    assert tmp_db.get_setting("tg_user_id") == "456"
    # 不覆盖已有 key
    all_s = tmp_db.get_settings()
    assert all_s["schedule_cron"] == "0 7 * * *"


# ---------- 账号 ----------

def test_add_and_get_account(tmp_db):
    acc_id = tmp_db.add_account({
        "name": "小号1",
        "cookie": "SUB=abc",
        "cookie_raw": "SUB=abc; SCF=x",
        "enabled": True,
        "proxy_index": 0,
        "remark": "测试",
    })
    acc = tmp_db.get_account(acc_id)
    assert acc["name"] == "小号1"
    assert acc["cookie"] == "SUB=abc"
    assert acc["enabled"] == 1
    assert acc["last_status"] == "unknown"


def test_update_account(tmp_db):
    acc_id = tmp_db.add_account({"name": "A", "cookie": ""})
    assert tmp_db.update_account(acc_id, {"name": "B", "enabled": False})
    acc = tmp_db.get_account(acc_id)
    assert acc["name"] == "B"
    assert acc["enabled"] == 0


def test_delete_account(tmp_db):
    acc_id = tmp_db.add_account({"name": "A", "cookie": ""})
    assert tmp_db.delete_account(acc_id)
    assert tmp_db.get_account(acc_id) is None


def test_enabled_accounts(tmp_db):
    tmp_db.add_account({"name": "A", "cookie": "", "enabled": True})
    tmp_db.add_account({"name": "B", "cookie": "", "enabled": False})
    enabled = tmp_db.get_enabled_accounts()
    assert [a["name"] for a in enabled] == ["A"]


def test_touch_account_result(tmp_db):
    acc_id = tmp_db.add_account({"name": "A", "cookie": ""})
    tmp_db.touch_account_result(acc_id, "success", "全部成功")
    acc = tmp_db.get_account(acc_id)
    assert acc["last_status"] == "success"
    assert acc["last_message"] == "全部成功"
    assert acc["last_checkin"] is not None


# ---------- 日志 ----------

def test_logs_and_stats(tmp_db):
    acc_id = tmp_db.add_account({"name": "A", "cookie": ""})
    tmp_db.add_log({
        "account_id": acc_id, "account_name": "A", "task_id": "t1",
        "status": "success", "channel": "direct",
        "total": 2, "success": 2, "fail": 0,
        "detail": [{"name": "超话1", "success": True}], "message": "ok",
    })
    tmp_db.add_log({
        "account_id": acc_id, "account_name": "A", "task_id": "t2",
        "status": "failed", "channel": "socks",
        "total": 1, "success": 0, "fail": 1,
        "detail": [], "message": "Cookie 失效",
    })
    logs = tmp_db.get_logs()
    assert len(logs) == 2
    # 最新(id 大)在前：logs[0] 是 t2(空 detail)，logs[1] 是 t1
    assert logs[0]["task_id"] == "t2"
    assert logs[0]["detail"] == []
    # detail 被正确 JSON 解析（t1 的 detail 有数据）
    assert logs[1]["task_id"] == "t1"
    assert logs[1]["detail"][0]["name"] == "超话1"
    # 按账号过滤
    assert len(tmp_db.get_logs(account_id=acc_id)) == 2

    stats = tmp_db.get_log_stats()
    assert stats["total"] == 2
    assert stats["success"] == 1
    assert stats["fail"] == 1
    assert stats["today"] == 2


# ---------- 任务 & 通知 ----------

def test_task_lifecycle(tmp_db):
    tmp_db.create_task("task-xyz", "manual")
    tasks = tmp_db.get_tasks()
    assert tasks[0]["status"] == "running"
    tmp_db.finish_task("task-xyz", "success", "完成")
    tasks = tmp_db.get_tasks()
    assert tasks[0]["status"] == "success"
    assert tasks[0]["finished_at"] is not None


def test_notification_record(tmp_db):
    tmp_db.add_notification("标题", "内容", True)
    tmp_db.add_notification("标题2", "内容2", False, "网络错误")
    notif = tmp_db.get_notifications()
    assert len(notif) == 2
    assert notif[0]["success"] == 0  # 最新在前
    assert notif[1]["success"] == 1
