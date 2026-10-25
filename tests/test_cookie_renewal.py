from datetime import datetime

import pytest
import requests

from app import cookie_session, database as db, weibo_client


@pytest.fixture()
def account(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "renew.db")
    db._local.conn = None
    db.init_db()
    account_id = db.add_account({"name": "续签账号", "cookie_raw": "SUB=old; SCF=old"})
    monkeypatch.setattr(cookie_session.notifier, "send_text_message", lambda text: True)
    return db.get_account(account_id)


def test_new_session_cookies_are_persisted(account, monkeypatch):
    def fake_verify(session, cookies, **kwargs):
        session.cookies.set("ALF", "new-alf")
        session.cookies.set("SUB", "new-sub")
        return True, "st"
    monkeypatch.setattr(cookie_session, "verify_cookie", fake_verify)
    result = cookie_session.refresh_account_cookie(account["id"])
    assert result["ok"] is True
    assert set(result["changed"]) == {"ALF", "SUB"}
    updated = db.get_account(account["id"])
    assert "SUB=new-sub" in updated["cookie_raw"]
    assert "ALF=new-alf" in updated["cookie_raw"]
    assert updated["cookie_status"] == "healthy"
    assert updated["cookie_refreshed_at"]


def test_transient_cookies_do_not_claim_renewal(account, monkeypatch):
    def fake_verify(session, cookies, **kwargs):
        session.cookies.set("WBTOKEN", "transient")
        return True, "st"
    monkeypatch.setattr(cookie_session, "verify_cookie", fake_verify)
    result = cookie_session.refresh_account_cookie(account["id"])
    assert result["changed"] == []
    assert "有效" in result["message"]


def test_network_failure_does_not_replace_cookie(account, monkeypatch):
    monkeypatch.setattr(cookie_session, "verify_cookie",
                        lambda *args, **kwargs: (_ for _ in ()).throw(weibo_client.NetworkError("offline")))
    result = cookie_session.refresh_account_cookie(account["id"])
    assert result["valid"] is None
    updated = db.get_account(account["id"])
    assert updated["cookie_raw"] == account["cookie_raw"]
    assert updated["cookie_status"] == "network"
    assert updated["cookie_failure_count"] == 1


def test_expired_login_is_not_retried_automatically(account, monkeypatch):
    monkeypatch.setattr(cookie_session, "verify_cookie", lambda *args, **kwargs: (False, None))
    result = cookie_session.refresh_account_cookie(account["id"])
    assert result["status"] == "expired"
    assert cookie_session.should_refresh(db.get_account(account["id"])) is False


def test_refresh_due_and_backoff(account):
    assert cookie_session.should_refresh(account) is True
    db.update_account(account["id"], {"cookie_checked_at": db._now(), "cookie_status": "healthy"})
    assert cookie_session.should_refresh(db.get_account(account["id"])) is False
    db.update_account(account["id"], {"cookie_status": "network", "cookie_failure_count": 2})
    assert cookie_session.should_refresh(db.get_account(account["id"])) is False


def test_cookie_expiry_accepts_unix_and_relative_values():
    now = int(datetime.now().timestamp()) + 86400
    assert cookie_session.cookie_expiry({"ALF": str(now)})
    assert cookie_session.cookie_expiry({"ALF": "3600"})
    assert cookie_session.cookie_expiry({"ALF": "invalid"}) == ""


def test_request_json_matches_v121_cookie_behavior():
    session = requests.Session()
    class Response:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return {"ok": 1}
    calls = []
    def request(method, url, **kwargs):
        calls.append(kwargs.get("cookies"))
        if len(calls) == 1:
            session.cookies.set("SUB", "new")
        return Response()
    session.request = request
    weibo_client.request_json(session, "GET", "https://m.weibo.cn/api/config", cookies={"SUB": "old"})
    weibo_client.request_json(session, "GET", "https://m.weibo.cn/api/config", cookies={"SUB": "old"})
    assert calls == [{"SUB": "old"}, {"SUB": "old"}]
    assert session.cookies["SUB"] == "new"


def test_panel_session_is_sliding(account):
    from datetime import timedelta
    user_id = db.create_user("admin", "hash")
    db.create_session("token", user_id)
    soon = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
    db._get_conn().execute("UPDATE sessions SET expires_at=? WHERE token='token'", (soon,))
    db._get_conn().commit()
    assert db.get_session_user("token", sliding=True)["username"] == "admin"
    expiry = db._get_conn().execute("SELECT expires_at FROM sessions WHERE token='token'").fetchone()[0]
    assert datetime.strptime(expiry, "%Y-%m-%d %H:%M:%S") > datetime.now() + timedelta(days=29)


def test_stale_cookie_result_cannot_overwrite_new_qr_login(account):
    stale = dict(account)
    db.update_account(account["id"], {"cookie": "SUB=new-login", "cookie_raw": "SUB=new-login",
                                     "cookie_checked_at": db._now()})
    result = cookie_session.persist_cookie_result(stale, {"SUB": "old"}, True, "healthy")
    assert result["cookie_raw"] == "SUB=new-login"
    assert db.get_account(account["id"])["cookie_raw"] == "SUB=new-login"
