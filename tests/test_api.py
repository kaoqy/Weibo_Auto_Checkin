"""API 集成测试：通过 FastAPI TestClient 测试全部接口。
使用临时数据库，不触碰真实网络（验证 Cookie 接口会用 mock 替换 requests）。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app.database as db  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_path = tmp_path / "test_api.db"
    monkeypatch.setattr(db, "DB_PATH", db_path)
    db._local.conn = None
    db.init_db()

    from fastapi.testclient import TestClient
    with TestClient(app) as c:
        yield c

    db._local.conn = None


def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_account_crud(client):
    # 创建
    r = client.post("/api/accounts", json={
        "name": "小号A", "cookie_raw": "SUB=abc; SCF=x", "enabled": True,
    })
    assert r.status_code == 200
    acc_id = r.json()["id"]

    # 列表
    r = client.get("/api/accounts")
    assert len(r.json()) == 1
    assert r.json()[0]["name"] == "小号A"

    # 详情
    r = client.get(f"/api/accounts/{acc_id}")
    assert r.status_code == 200
    assert r.json()["cookie_length"] > 0

    # 更新
    r = client.put(f"/api/accounts/{acc_id}", json={"name": "小号B",
                                                    "enabled": False})
    assert r.json()["name"] == "小号B"
    assert r.json()["enabled"] == 0

    # 404
    assert client.get("/api/accounts/9999").status_code == 404

    # 删除
    assert client.delete(f"/api/accounts/{acc_id}").status_code == 200
    assert client.get("/api/accounts").json() == []


def test_settings_api(client):
    # 默认值
    r = client.get("/api/settings")
    assert r.json()["schedule_cron"] == "0 7 * * *"

    # 更新
    r = client.post("/api/settings", json={
        "tg_bot_token": "tok", "tg_enabled": "1", "schedule_cron": "0 8 * * *",
    })
    assert r.json()["tg_bot_token"] == "tok"
    assert r.json()["schedule_cron"] == "0 8 * * *"

    # 未知 key 应被忽略
    r = client.post("/api/settings", json={"hacker_key": "x"})
    assert "hacker_key" not in r.json()


def test_logs_stats_api(client):
    # 先建账号和手动插日志
    acc_id = client.post("/api/accounts", json={"name": "A", "cookie": ""}).json()["id"]
    db.add_log({
        "account_id": acc_id, "account_name": "A", "task_id": "t",
        "status": "success", "channel": "direct",
        "total": 1, "success": 1, "fail": 0, "detail": [], "message": "",
    })
    stats = client.get("/api/logs/stats").json()
    assert stats["total"] == 1
    assert stats["success"] == 1


def test_verify_endpoint_invalid_cookie(client):
    """校验接口应返回 valid=False 并给出提示（真实调用会被 mock 拦截）。"""
    acc_id = client.post("/api/accounts", json={
        "name": "A", "cookie_raw": "SUB=x",
    }).json()["id"]

    # mock 微博验证接口：返回未登录
    import app.api.accounts as acc_mod
    import requests
    from unittest.mock import patch

    class FakeResp:
        status_code = 200
        def json(self):
            return {"data": {"login": False, "st": None}}

    with patch.object(requests.Session, "request",
                      return_value=FakeResp()):
        r = client.post(f"/api/accounts/{acc_id}/verify")
        assert r.json()["valid"] is False
        assert "无效" in r.json()["message"]


def test_checkin_flow_with_mock(client):
    """用 mock 微博接口测试完整签到流程（成功路径）。"""
    acc_id = client.post("/api/accounts", json={
        "name": "成功号", "cookie_raw": "SUB=valid; SCF=x",
    }).json()["id"]

    from unittest.mock import patch
    from app import scheduler
    from app.weibo_client import _bundle

    fake_result = _bundle(
        "success", "签到完成", 2, 2, 0,
        [{"name": "超话1", "success": True, "message": "已签到"},
         {"name": "超话2", "success": True, "message": "今日已签到"}],
        {"SUB": "refreshed", "SCF": "x"}, ["SUB"], "direct",
    )

    with patch.object(scheduler, "run_account_checkin", return_value=fake_result):
        summary = scheduler.run_checkin("manual")

    assert summary["status"] == "success"
    assert summary["total"] == 2
    assert summary["success"] == 2

    # 账号状态已更新
    acc = db.get_account(acc_id)
    assert acc["last_status"] == "success"
    # Cookie 已回写为字符串
    assert "refreshed" in (acc["cookie"] or "")


def test_static_files(client):
    assert client.get("/").status_code == 200
    assert "text/html" in client.get("/").headers["content-type"]
    assert client.get("/app.js").status_code == 200
    assert client.get("/style.css").status_code == 200


def test_qr_finish_requires_completed_login(client):
    """未完成扫码时不能创建账号。"""
    from app.api import accounts

    accounts._qr_sessions["pending-test"] = {
        "created_at": __import__("time").time(),
        "session": __import__("requests").Session(),
        "qrid": "test-qrid",
        "alt": "",
        "login_url": "",
    }
    response = client.post("/api/accounts/qr/finish", json={
        "session_id": "pending-test",
        "name": "扫码账号",
    })
    assert response.status_code == 409


def test_qr_finish_saves_account(client):
    """扫码确认后的 Cookie 应保存到新账号。"""
    import time
    import requests
    from app.api import accounts

    session = requests.Session()
    session.cookies.set("SUB", "qr-login-cookie", domain=".weibo.com")
    accounts._qr_sessions["confirmed-test"] = {
        "created_at": time.time(),
        "session": session,
        "qrid": "test-qrid",
        "alt": "",
        "login_url": "",
        "cookie": "SUB=qr-login-cookie",
    }
    response = client.post("/api/accounts/qr/finish", json={
        "session_id": "confirmed-test",
        "name": "扫码账号",
    })
    assert response.status_code == 200
    account = db.get_account(response.json()["id"])
    assert account["name"] == "扫码账号"
    assert "qr-login-cookie" in (account["cookie"] or "")
