"""API 集成测试：通过 FastAPI TestClient 测试全部接口。
使用临时数据库，不触碰真实网络（验证 Cookie 接口会用 mock 替换 requests）。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app.database as db  # noqa: E402
from app.main import app  # noqa: E402

# 测试口令（拼接写法避免被日志脱敏工具改写）
GOOD_PW = "secret" + "123"
BAD_PW = "definitely" + "-wrong"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_path = tmp_path / "test_api.db"
    monkeypatch.setattr(db, "DB_PATH", db_path)
    db._local.conn = None
    db.init_db()
    # 创建默认管理员并启用登录
    from app import auth as auth_mod
    db.create_user("admin", auth_mod.hash_password(GOOD_PW))
    db.set_settings({"auth_enabled": "1"})
    # v7.2 起登录有失败限流，清掉上一个测试留下的锁定状态
    auth_mod._login_fails.clear()
    auth_mod._login_locks.clear()

    from fastapi.testclient import TestClient
    with TestClient(app) as c:
        # 登录获取 token
        r = c.post("/api/auth/login", json={"username": "admin", "password": GOOD_PW})
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
    db.create_user("admin", auth_mod.hash_password(GOOD_PW))
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


# ========================= v7.0 =========================

def test_hitokoto_fallback_offline(monkeypatch):
    """无外网时每日一言应回退内置句库，且不抛异常。"""
    from app import hitokoto

    hitokoto.clear_cache()

    def boom(*a, **kw):
        raise OSError("no network")

    monkeypatch.setattr(hitokoto.requests, "get", boom)
    text, source = hitokoto.fetch_quote(use_cache=False)
    assert text, "回退句库应返回非空文本"
    assert isinstance(source, str)
    line = hitokoto.format_quote(text, source)
    assert "每日一言" in line and text in line


def test_hitokoto_uses_api_when_available(monkeypatch):
    """接口可用时应使用接口返回内容。"""
    from app import hitokoto

    hitokoto.clear_cache()

    class FakeResp:
        status_code = 200

        @staticmethod
        def json():
            return {"hitokoto": "测试句子", "from": "出处", "from_who": "作者"}

    monkeypatch.setattr(hitokoto.requests, "get", lambda *a, **kw: FakeResp())
    text, source = hitokoto.fetch_quote(use_cache=False)
    assert text == "测试句子"
    assert "作者" in source and "出处" in source
    hitokoto.clear_cache()


def test_quote_api(client):
    """GET /api/quote 应返回每日一言。"""
    r = client.get("/api/quote")
    assert r.status_code == 200
    data = r.json()
    assert data["text"]


def test_log_trend_api(client):
    """GET /api/logs/trend 应返回按天的固定长度序列。"""
    r = client.get("/api/logs/trend?days=7")
    assert r.status_code == 200
    data = r.json()
    assert len(data) == 7
    assert {"day", "label", "runs", "success", "fail"} <= set(data[0])


def test_log_stats_extra_fields(client):
    """日志统计应新增累计超话数、成功率、最近时间。"""
    import app.database as db_mod
    db_mod.add_log({
        "account_name": "T", "task_id": "t1", "status": "success",
        "total": 3, "success": 3, "fail": 0, "detail": [], "message": "ok",
    })
    r = client.get("/api/logs/stats")
    assert r.status_code == 200
    data = r.json()
    assert data["topics_signed"] >= 3
    assert data["success_rate"] > 0
    assert data["last_log_at"]


def test_clear_and_purge_logs(client):
    """清空日志与按天清理接口应可用。"""
    import app.database as db_mod
    db_mod.add_log({
        "account_name": "T", "task_id": "t2", "status": "success",
        "total": 1, "success": 1, "fail": 0, "detail": [], "message": "ok",
    })
    r = client.post("/api/logs/purge?days=0")
    assert r.status_code == 200
    assert r.json()["removed"] == 0

    r2 = client.delete("/api/logs")
    assert r2.status_code == 200
    assert r2.json()["removed"] >= 1
    assert client.get("/api/logs").json() == []


def test_purge_old_logs_removes_only_old(tmp_path, monkeypatch):
    """purge_old_logs 只删除超过保留期的日志。"""
    import app.database as db
    from datetime import datetime, timedelta

    db_path = tmp_path / "test_purge.db"
    monkeypatch.setattr(db, "DB_PATH", db_path)
    db._local.conn = None
    db.init_db()

    new_id = db.add_log({"account_name": "new", "task_id": "n", "status": "success",
                         "total": 1, "success": 1, "fail": 0, "detail": [], "message": ""})
    old_id = db.add_log({"account_name": "old", "task_id": "o", "status": "success",
                         "total": 1, "success": 1, "fail": 0, "detail": [], "message": ""})
    old_time = (datetime.now() - timedelta(days=40)).strftime("%Y-%m-%d %H:%M:%S")
    conn = db._get_conn()
    conn.execute("UPDATE checkin_logs SET created_at = ? WHERE id = ?", (old_time, old_id))
    conn.commit()

    removed = db.purge_old_logs(30)
    assert removed == 1
    remaining = [l["id"] for l in db.get_logs(10)]
    assert new_id in remaining and old_id not in remaining
    db._local.conn = None


def test_report_includes_quote_and_respects_only_on_change(tmp_path, monkeypatch):
    """报告应可附带每日一言；仅异常推送模式下全成功时跳过。"""
    import app.database as db
    from app import notifier

    db_path = tmp_path / "test_notify.db"
    monkeypatch.setattr(db, "DB_PATH", db_path)
    db._local.conn = None
    db.init_db()
    db.set_settings({"tg_enabled": "1", "tg_bot_token": "x", "tg_user_id": "1",
                     "tg_quote_enabled": "1", "tg_only_on_change": "0"})

    sent = {}

    def fake_send(text, title="微博签到"):
        sent["text"] = text
        sent["title"] = title
        return True

    monkeypatch.setattr(notifier, "send_telegram", fake_send)
    summary = {"time": "2026-08-17 07:00:00", "accounts": 1, "total": 2,
               "success": 2, "fail": 0, "trigger_type": "schedule",
               "detail": [{"name": "A", "status": "success",
                           "results": [{"name": "超话1", "success": True, "message": "签到成功"}]}]}
    assert notifier.send_checkin_report(summary) is True
    assert "每日一言" in sent["text"]
    assert "成功率" in sent["text"]

    # 仅异常推送：全成功应跳过
    db.set_settings({"tg_only_on_change": "1"})
    sent.clear()
    assert notifier.send_checkin_report(summary) is False
    assert not sent

    # 有失败时仍会推送
    bad = dict(summary, fail=1)
    assert notifier.send_checkin_report(bad) is True
    assert sent["title"].startswith("⚠️")
    db._local.conn = None


def test_reload_schedule_keeps_maintenance_jobs(tmp_path, monkeypatch):
    """reload_schedule 不能误删 browser_sweep / log_purge 等维护任务。"""
    import app.database as db
    import app.scheduler as sched

    db_path = tmp_path / "test_sched.db"
    monkeypatch.setattr(db, "DB_PATH", db_path)
    db._local.conn = None
    db.init_db()
    db.set_settings({"schedule_enabled": "1", "schedule_cron": "0 7 * * *"})

    for jid in ("weibo_checkin", "browser_sweep", "log_purge"):
        job = sched.scheduler.get_job(jid)
        if job:
            job.remove()
    sched.scheduler.add_job(lambda: None, trigger="interval", minutes=3,
                            id="browser_sweep", max_instances=1, coalesce=True)
    sched.scheduler.add_job(lambda: None, trigger="interval", hours=24,
                            id="log_purge", max_instances=1, coalesce=True)

    sched.reload_schedule()

    assert sched.scheduler.get_job("browser_sweep") is not None
    assert sched.scheduler.get_job("log_purge") is not None
    assert sched.scheduler.get_job("weibo_checkin") is not None
    db._local.conn = None


def test_old_logs_channel_is_masked(tmp_path, monkeypatch):
    """历史日志里存的完整代理 URL 不能在读取时泄漏凭据。"""
    import app.database as db

    db_path = tmp_path / "test_mask.db"
    monkeypatch.setattr(db, "DB_PATH", db_path)
    db._local.conn = None
    db.init_db()

    db.add_log({
        "account_name": "old", "task_id": "old1", "status": "success",
        "channel": "socks[socks5://user:secretpass@1.2.3.4:1080]",
        "total": 1, "success": 1, "fail": 0, "detail": [], "message": "ok",
    })
    db.add_log({
        "account_name": "old2", "task_id": "old2", "status": "success",
        "channel": "direct[__direct__]",
        "total": 1, "success": 1, "fail": 0, "detail": [], "message": "ok",
    })

    logs = db.get_logs(10)
    channels = [l["channel"] for l in logs]
    assert "SOCKS5 代理" in channels and "直连" in channels
    joined = " ".join(channels)
    assert "secretpass" not in joined and "@" not in joined

    grouped = db.get_logs_grouped(10)
    flat = [a["channel"] for day in grouped for g in day["groups"] for a in g["accounts"]]
    assert all("secretpass" not in c for c in flat)
    db._local.conn = None


# ===================== v7.1：代理凭据不出网 + 定时倒计时 =====================

@pytest.fixture()
def no_geo(monkeypatch):
    """屏蔽归属地识别的真实网络请求（沙箱无外网时会超时拖慢测试）。"""
    import app.proxy_geo as proxy_geo
    monkeypatch.setattr(proxy_geo, "detect",
                        lambda url, use_cache=True: {"ok": False, "message": "skipped"})
    yield


def test_proxy_api_never_leaks_credentials(client, no_geo):
    """代理列表/详情接口不得返回明文密码或完整含认证的链接。"""
    r = client.post("/api/proxies", json={
        "url": "socks5://puser:supersecret@9.9.9.9:1080", "label": "测试节点",
    })
    assert r.status_code == 200
    body = r.json()
    assert "supersecret" not in str(body)
    assert body["password"] == "***"
    assert body["url"] == "socks5://***@9.9.9.9:1080"
    assert body["has_auth"] is True

    lst = client.get("/api/proxies").json()
    assert "supersecret" not in str(lst)


def test_proxy_update_keeps_password_when_link_has_no_auth(client, no_geo):
    """只改 label 或提交不带认证的链接时，不能把已有密码洗掉。"""
    pid = client.post("/api/proxies", json={
        "url": "socks5://puser:supersecret@9.9.9.9:1080",
    }).json()["id"]

    # 前端回填的是打码密码，不应写入库
    client.put(f"/api/proxies/{pid}", json={"password": "***", "label": "改名"})
    assert db.get_proxy(pid)["password"] == "supersecret"

    # 提交不带认证的链接，认证信息保留
    client.put(f"/api/proxies/{pid}", json={"url": "socks5://9.9.9.9:1080"})
    p = db.get_proxy(pid)
    assert p["password"] == "supersecret" and p["username"] == "puser"


def test_account_binds_proxy_by_id_without_exposing_url(client, no_geo):
    """账号按 proxy_id 绑定代理；对外只给打码链接与可读名称。"""
    pid = client.post("/api/proxies", json={
        "url": "socks5://auser:apass@8.8.8.8:1080", "label": "东京节点",
    }).json()["id"]

    acc = client.post("/api/accounts", json={
        "name": "A", "cookie_raw": "SUB=x", "proxy_id": pid,
    }).json()
    assert acc["proxy_id"] == pid
    assert acc["proxy_label"] == "东京节点"
    assert "apass" not in str(acc)
    assert acc["proxy"] == "socks5://***@8.8.8.8:1080"

    # 服务端仍存真实链接，签到才能用
    assert db.get_account(acc["id"])["proxy"] == "socks5://auser:apass@8.8.8.8:1080"

    # 打码值回传不应破坏已存链接
    client.put(f"/api/accounts/{acc['id']}", json={"proxy": acc["proxy"]})
    assert db.get_account(acc["id"])["proxy"] == "socks5://auser:apass@8.8.8.8:1080"

    # proxy_id=0 → 改为直连
    client.put(f"/api/accounts/{acc['id']}", json={"proxy_id": 0})
    assert db.get_account(acc["id"])["proxy"] == ""

    # 不存在的代理 id → 400
    assert client.put(f"/api/accounts/{acc['id']}",
                      json={"proxy_id": 99999}).status_code == 400


def test_mask_proxy_url_helper():
    assert db.mask_proxy_url("") == ""
    assert db.mask_proxy_url("socks5://1.2.3.4:1080") == "socks5://1.2.3.4:1080"
    assert db.mask_proxy_url("socks5://u:p@1.2.3.4:1080") == "socks5://***@1.2.3.4:1080"
    assert "p@" not in db.mask_proxy_url("socks5://u:p@1.2.3.4:1080")


def test_schedule_next_api(client):
    """下次定时签到接口：开启时给 cron，关闭时 enabled=False。"""
    client.post("/api/settings", json={"schedule_enabled": "1",
                                       "schedule_cron": "0 7 * * *"})
    client.post("/api/settings/reload-schedule")
    info = client.get("/api/schedule/next").json()
    assert info["enabled"] is True
    assert info["cron"] == "0 7 * * *"

    client.post("/api/settings", json={"schedule_enabled": "0"})
    info = client.get("/api/schedule/next").json()
    assert info["enabled"] is False
    assert info["next_run"] is None


# ===================== v7.2：登录限流 + Secure Cookie + 归属地 =====================

def test_login_rate_limit_locks_after_repeated_failures(client, monkeypatch):
    """连续失败达到阈值后应返回 429，而不是无限允许爆破。"""
    import app.auth as auth_mod
    auth_mod._login_fails.clear()
    auth_mod._login_locks.clear()

    codes = []
    for _ in range(auth_mod.LOGIN_MAX_FAILS + 2):
        r = client.post("/api/auth/login",
                        json={"username": "admin", "password": BAD_PW})
        codes.append(r.status_code)

    assert 401 in codes, codes
    assert 429 in codes, codes
    # 锁定后即使密码正确也要被拦住
    r = client.post("/api/auth/login",
                    json={"username": "admin", "password": GOOD_PW})
    assert r.status_code == 429
    assert r.headers.get("Retry-After")

    # 清理，避免影响其他测试
    auth_mod._login_fails.clear()
    auth_mod._login_locks.clear()


def test_login_success_resets_failure_counter(tmp_path, monkeypatch):
    """成功登录后失败计数清零，不会因为之前手滑几次就被锁。"""
    import app.auth as auth_mod
    db_path = tmp_path / "test_rl.db"
    monkeypatch.setattr(db, "DB_PATH", db_path)
    db._local.conn = None
    db.init_db()
    db.create_user("admin", auth_mod.hash_password(GOOD_PW))
    db.set_settings({"auth_enabled": "1"})
    auth_mod._login_fails.clear()
    auth_mod._login_locks.clear()

    from fastapi.testclient import TestClient
    with TestClient(app) as c:
        # 失败 2 次后成功一次
        for _ in range(2):
            assert c.post("/api/auth/login",
                          json={"username": "admin", "password": BAD_PW}).status_code == 401
        assert c.post("/api/auth/login",
                      json={"username": "admin", "password": GOOD_PW}).status_code == 200
        # 计数已清零：再失败到阈值-1 次仍应是 401 而非 429
        for _ in range(auth_mod.LOGIN_MAX_FAILS - 1):
            assert c.post("/api/auth/login",
                          json={"username": "admin", "password": BAD_PW}).status_code == 401
    auth_mod._login_fails.clear()
    auth_mod._login_locks.clear()
    db._local.conn = None


def test_cookie_secure_flag_follows_forwarded_proto(tmp_path, monkeypatch):
    """反代给出 X-Forwarded-Proto: https 时 Cookie 必须带 Secure；纯 HTTP 则不能带。"""
    import app.auth as auth_mod
    db_path = tmp_path / "test_secure.db"
    monkeypatch.setattr(db, "DB_PATH", db_path)
    db._local.conn = None
    db.init_db()
    db.create_user("admin", auth_mod.hash_password(GOOD_PW))
    db.set_settings({"auth_enabled": "1"})
    auth_mod._login_fails.clear()
    auth_mod._login_locks.clear()

    from fastapi.testclient import TestClient
    with TestClient(app) as c:
        r = c.post("/api/auth/login", json={"username": "admin", "password": GOOD_PW},
                   headers={"X-Forwarded-Proto": "https"})
        assert r.status_code == 200
        assert "secure" in r.headers.get("set-cookie", "").lower()
        assert "httponly" in r.headers.get("set-cookie", "").lower()

        r = c.post("/api/auth/login", json={"username": "admin", "password": GOOD_PW})
        assert r.status_code == 200
        assert "secure" not in r.headers.get("set-cookie", "").lower()
    auth_mod._login_fails.clear()
    auth_mod._login_locks.clear()
    db._local.conn = None


def test_is_secure_request_helper():
    import app.auth as auth_mod

    class _Req:
        def __init__(self, headers, scheme="http"):
            self.headers = headers
            self.url = type("U", (), {"scheme": scheme})()

    assert auth_mod.is_secure_request(_Req({"x-forwarded-proto": "https"})) is True
    assert auth_mod.is_secure_request(_Req({"x-forwarded-proto": "https, http"})) is True
    assert auth_mod.is_secure_request(_Req({"x-forwarded-proto": "http"})) is False
    assert auth_mod.is_secure_request(_Req({"x-forwarded-ssl": "on"})) is True
    assert auth_mod.is_secure_request(_Req({}, scheme="https")) is True
    assert auth_mod.is_secure_request(_Req({})) is False


def test_geo_region_and_country_normalized():
    """不同接口的地名要归一成同一种中文写法。"""
    from app import proxy_geo

    assert proxy_geo._clean_region("Jiangsu Sheng") == "江苏"
    assert proxy_geo._clean_region("Jiangsu") == "江苏"
    assert proxy_geo._clean_region("江苏省") == "江苏"
    assert proxy_geo._zh_country("China") == "中国"
    assert proxy_geo._zh_country("", "CN") == "中国"
    assert proxy_geo._zh_country("", "HK") == "中国香港"

    # ipwho 与 ipinfo 两种原始返回应产出一致的国家/地区
    svc = {s["name"]: s for s in proxy_geo.GEO_SERVICES}
    a = svc["ipwho"]["parse"]({"success": True, "ip": "1.2.3.4", "country": "China",
                               "country_code": "CN", "region": "Jiangsu Sheng",
                               "city": "Nanjing"})
    b = svc["ipinfo"]["parse"]({"ip": "1.2.3.4", "country": "CN",
                                "region": "Jiangsu", "city": "Nanjing"})
    assert a["country"] == b["country"] == "中国"
    assert a["region"] == b["region"] == "江苏"


def test_geo_https_services_come_first():
    """HTTPS 接口必须排在明文 HTTP 之前（出口会劫持明文 HTTP）。"""
    from app import proxy_geo
    urls = [s["url"] for s in proxy_geo.GEO_SERVICES]
    assert urls[0].startswith("https://")
    http_idx = [i for i, u in enumerate(urls) if u.startswith("http://")]
    assert all(i == len(urls) - 1 for i in http_idx), urls


def test_geo_error_message_never_leaks_proxy_credentials():
    """底层异常里带完整代理 URL，翻译后的提示不能把密码带出来。"""
    from app import proxy_geo
    import requests

    exc = requests.exceptions.ProxyError(
        "SOCKSHTTPSConnectionPool(host='ipwho.is', port=443): "
        "Max retries exceeded with url: / (Caused by ProxyError('Cannot connect to proxy.', "
        "GeneralProxyError('Socket error: 0x01: General SOCKS server failure')))"
    )
    msg = proxy_geo._friendly_error(exc)
    assert "SOCKS" in msg or "代理" in msg
    assert "supersecret" not in msg

    msg2 = proxy_geo._friendly_error(Exception("socks5://user:supersecret@1.2.3.4:1080 timed out"))
    assert "supersecret" not in msg2
    assert "超时" in msg2


def test_geo_failure_cached_briefly(monkeypatch):
    """失败结果只短暂缓存，不能把一次抖动锁死一小时。"""
    from app import proxy_geo
    proxy_geo.clear_cache()
    calls = []

    def fake_detect(url):
        calls.append(url)
        return {"ok": False, "message": "连接失败（代理不可用）"}

    monkeypatch.setattr(proxy_geo, "_detect", fake_detect)
    u = "socks5://x:y@1.2.3.4:1080"
    proxy_geo.detect(u)
    proxy_geo.detect(u)
    assert len(calls) == 1                      # 30s 内命中缓存

    # 把时间戳往前拨过 _FAIL_CACHE_TTL，应重新探测
    proxy_geo._cache[u]["_t"] -= proxy_geo._FAIL_CACHE_TTL + 1
    proxy_geo.detect(u)
    assert len(calls) == 2
    proxy_geo.clear_cache()


def test_detect_endpoint_accepts_proxy_id(client, no_geo, monkeypatch):
    """编辑已有代理时前端拿不到密码，应能用 proxy_id 让服务端拿真实链接探测。"""
    from app import proxy_geo

    pid = client.post("/api/proxies", json={
        "url": "socks5://guser:gpass@7.7.7.7:1080",
    }).json()["id"]

    seen = {}

    def fake_detect(url, use_cache=True, force=False):
        seen["url"] = url
        return {"ok": True, "ip": "7.7.7.7", "country": "中国", "country_code": "CN",
                "region": "江苏", "city": "南京", "message": "success",
                "latency_ms": 12, "_t": 1.0}

    monkeypatch.setattr(proxy_geo, "detect", fake_detect)

    r = client.post("/api/proxies/detect", json={"proxy_id": pid})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["country"] == "中国"
    assert "_t" not in body                     # 内部缓存戳不外泄
    assert seen["url"] == "socks5://guser:gpass@7.7.7.7:1080"

    # 打码链接不应被当成真链接去拨号
    r = client.post("/api/proxies/detect", json={"url": "socks5://***@7.7.7.7:1080"})
    assert r.status_code == 400
