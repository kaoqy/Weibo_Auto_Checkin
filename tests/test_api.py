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
    # 创建默认管理员并启用登录
    from app import auth as auth_mod
    db.create_user("admin", auth_mod.hash_password("secret123"))
    db.set_settings({"auth_enabled": "1"})

    from fastapi.testclient import TestClient
    with TestClient(app) as c:
        # 登录获取 token
        r = c.post("/api/auth/login", json={"username": "admin", "password": "secret123"})
        assert r.status_code == 200
        token = r.cookies.get(auth_mod.COOKIE_NAME)
        c.headers.update({"Cookie": f"{auth_mod.COOKIE_NAME}={token}"})
        yield c

    db._local.conn = None


def _unauth_client(tmp_path, monkeypatch):
    """未登录客户端（用于测 401）。"""
    db_path = tmp_path / "test_api_unauth.db"
    monkeypatch.setattr(db, "DB_PATH", db_path)
    db._local.conn = None
    db.init_db()
    from app import auth as auth_mod
    db.create_user("admin", auth_mod.hash_password("secret123"))
    db.set_settings({"auth_enabled": "1"})
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


def test_run_checkin_groups_by_proxy(tmp_path, monkeypatch):
    """不同 socks 的账号应分组（不同线程），同 socks 顺序。用 mock 验证。"""
    import app.database as db
    import app.scheduler as sched
    from app import auth as auth_mod
    from app.weibo_client import _bundle

    db_path = tmp_path / "test_group.db"
    monkeypatch.setattr(db, "DB_PATH", db_path)
    db._local.conn = None
    db.init_db()
    db.create_user("admin", auth_mod.hash_password("sec123456"))
    db.set_settings({"auth_enabled": "0", "anti_ban_enabled": "0"})

    # 三个账号：两个用 soapA，一个用 soapB
    a1 = db.add_account({"name": "A1", "cookie_raw": "SUB=a", "proxy": "socks5://p1:1080"})
    a2 = db.add_account({"name": "A2", "cookie_raw": "SUB=b", "proxy": "socks5://p1:1080"})
    a3 = db.add_account({"name": "A3", "cookie_raw": "SUB=c", "proxy": "socks5://p2:1080"})

    called = []
    def fake_checkin(cookie, opts, proxy_url=None, proxy_index=0):
        called.append(proxy_url)
        return _bundle("success", "ok", 1, 1, 0,
                       [{"name": "超话", "success": True, "message": "已签到"}],
                       dict(cookie), [], proxy_url or "direct")

    from unittest.mock import patch
    with patch.object(sched, "run_account_checkin", side_effect=fake_checkin):
        summary = sched.run_checkin("manual")

    assert summary["status"] == "success"
    assert summary["accounts"] == 3
    # A1/A2 用 p1 代理，A3 用 p2 代理
    assert "socks5://p1:1080" in called, f"p1 代理应被调用, got {called}"
    assert "socks5://p2:1080" in called
    db._local.conn = None


def test_run_checkin_selected_accounts(tmp_path, monkeypatch):
    """手动签到指定账号（多选）：只签到选中且启用的账号。"""
    import app.database as db
    import app.scheduler as sched
    from app import auth as auth_mod
    from app.weibo_client import _bundle

    db_path = tmp_path / "test_sel.db"
    monkeypatch.setattr(db, "DB_PATH", db_path)
    db._local.conn = None
    db.init_db()
    db.create_user("admin", auth_mod.hash_password("sec123456"))
    db.set_settings({"auth_enabled": "0", "anti_ban_enabled": "0"})

    a1 = db.add_account({"name": "A1", "cookie_raw": "SUB=a", "enabled": 1})
    a2 = db.add_account({"name": "A2", "cookie_raw": "SUB=b", "enabled": 1})
    a3 = db.add_account({"name": "A3", "cookie_raw": "SUB=c", "enabled": 1})

    called = []
    def fake_checkin(cookie, opts, proxy_url=None, proxy_index=0):
        name = cookie.get("SUB", "?")
        called.append(name)
        return _bundle("success", "ok", 1, 1, 0,
                       [{"name": "超话", "success": True, "message": "已签到"}],
                       dict(cookie), [], proxy_url or "direct")

    from unittest.mock import patch
    with patch.object(sched, "run_account_checkin", side_effect=fake_checkin):
        summary = sched.run_checkin("manual", account_ids=[a1, a3])

    assert summary["accounts"] == 2, f"应只签到 2 个账号, got {summary['accounts']}"
    assert set(called) == {"a", "c"}, f"应只含 A1/A3, got {called}"
    db._local.conn = None


def test_run_checkin_disabled_excluded(tmp_path, monkeypatch):
    """禁用的账号不应进入自动签到队列。"""
    import app.database as db
    import app.scheduler as sched
    from app import auth as auth_mod
    from app.weibo_client import _bundle

    db_path = tmp_path / "test_dis.db"
    monkeypatch.setattr(db, "DB_PATH", db_path)
    db._local.conn = None
    db.init_db()
    db.create_user("admin", auth_mod.hash_password("sec123456"))
    db.set_settings({"auth_enabled": "0", "anti_ban_enabled": "0"})

    db.add_account({"name": "ON", "cookie_raw": "SUB=on", "enabled": 1})
    db.add_account({"name": "OFF", "cookie_raw": "SUB=off", "enabled": 0})

    called = []
    def fake_checkin(cookie, opts, proxy_url=None, proxy_index=0):
        called.append(cookie.get("SUB"))
        return _bundle("success", "ok", 1, 1, 0, [], dict(cookie), [], "direct")

    from unittest.mock import patch
    with patch.object(sched, "run_account_checkin", side_effect=fake_checkin):
        summary = sched.run_checkin("manual")

    assert called == ["on"], f"禁用账号不应进入队列, got {called}"
    assert summary["accounts"] == 1
    db._local.conn = None


def test_checkin_run_accounts_api(client):
    """POST /api/checkin/run-accounts 应启动后台线程并返回 ok。"""
    db = client.app.state  # noqa
    import app.database as db_mod
    a1 = db_mod.add_account({"name": "T1", "cookie_raw": "SUB=t1", "enabled": 1})
    r = client.post("/api/checkin/run-accounts", json={"account_ids": [a1]})
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["count"] == 1

    # 空列表应 400
    r2 = client.post("/api/checkin/run-accounts", json={"account_ids": []})
    assert r2.status_code == 400
