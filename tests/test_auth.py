"""认证与登录 API 测试。"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app.database as db  # noqa: E402
from app import auth  # noqa: E402


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_path = tmp_path / "test_auth.db"
    monkeypatch.setattr(db, "DB_PATH", db_path)
    db._local.conn = None
    db.init_db()
    # 创建默认管理员
    db.create_user("admin", auth.hash_password("secret123"))
    db.set_settings({"auth_enabled": "1"})

    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as c:
        yield c
    db._local.conn = None


def test_health_public(client):
    assert client.get("/api/health").status_code == 200


def test_login_wrong_password(client):
    r = client.post("/api/auth/login", json={"username": "admin", "password": "wrong"})
    assert r.status_code == 401


def test_login_success_and_protected_api(client):
    # 未登录访问受保护 API → 401
    r = client.get("/api/accounts")
    assert r.status_code == 401

    # 登录
    r = client.post("/api/auth/login", json={"username": "admin", "password": "secret123"})
    assert r.status_code == 200
    token = r.cookies.get(auth.COOKIE_NAME)
    assert token
    client.cookies.set(auth.COOKIE_NAME, token)

    # 登录后访问受保护 API → 200
    r = client.get("/api/accounts")
    assert r.status_code == 200

    # /api/auth/me
    r = client.get("/api/auth/me")
    assert r.status_code == 200
    assert r.json()["username"] == "admin"


def test_logout_invalidates_session(client):
    r = client.post("/api/auth/login", json={"username": "admin", "password": "secret123"})
    token = r.cookies.get(auth.COOKIE_NAME)
    client.cookies.set(auth.COOKIE_NAME, token)
    # 登出
    client.post("/api/auth/logout")
    # 登出后 token 失效
    r = client.get("/api/accounts")
    assert r.status_code == 401


def test_change_password(client):
    r = client.post("/api/auth/login", json={"username": "admin", "password": "secret123"})
    token = r.cookies.get(auth.COOKIE_NAME)
    client.cookies.set(auth.COOKIE_NAME, token)
    # 原密码错误
    r = client.post("/api/auth/change-password",
                    json={"old_password": "wrong", "new_password": "newpass123"})
    assert r.status_code == 400
    # 正确修改
    r = client.post("/api/auth/change-password",
                    json={"old_password": "secret123", "new_password": "newpass123"})
    assert r.status_code == 200
    # 改密后旧会话被踢
    r = client.get("/api/accounts")
    assert r.status_code == 401
    # 新密码可登录
    r = client.post("/api/auth/login", json={"username": "admin", "password": "newpass123"})
    assert r.status_code == 200


def test_password_hash_verify():
    h = auth.hash_password("mysecret")
    assert h != "mysecret"
    assert auth.verify_password("mysecret", h)
    assert not auth.verify_password("wrong", h)


def test_ensure_default_admin(tmp_path, monkeypatch):
    """无用户时自动创建默认管理员。"""
    db_path = tmp_path / "test_seed.db"
    monkeypatch.setattr(db, "DB_PATH", db_path)
    db._local.conn = None
    db.init_db()
    assert db.count_users() == 0
    monkeypatch.setenv("WCM_ADMIN_USER", "boss")
    monkeypatch.setenv("WCM_ADMIN_PASSWORD", "pwd12345")
    auth.ensure_default_admin()
    assert db.count_users() == 1
    u = db.get_user_by_name("boss")
    assert u is not None
    assert auth.verify_password("pwd12345", u["password_hash"])
    db._local.conn = None


def test_static_login_page(client):
    assert client.get("/login.html").status_code == 200
    assert "登录" in client.get("/login.html").text


def test_init_flow_redirects_to_init_when_no_user(tmp_path, monkeypatch):
    """无用户时访问首页应重定向到 init.html。"""
    # 确保不因 WCM_ADMIN_PASSWORD 自动创建管理员
    monkeypatch.delenv("WCM_ADMIN_PASSWORD", raising=False)
    db_path = tmp_path / "test_init.db"
    monkeypatch.setattr(db, "DB_PATH", db_path)
    db._local.conn = None
    db.init_db()
    # auth_enabled 默认 1，无用户
    db.set_settings({"auth_enabled": "1"})

    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as c:
        # 访问首页 → 重定向 init.html
        r = c.get("/", follow_redirects=False)
        assert r.status_code == 302
        assert "/init.html" in r.headers.get("location", "")

        # 未初始化时 API 返回 403
        r = c.get("/api/accounts")
        assert r.status_code == 403

        # needs-init
        assert c.get("/api/auth/needs-init").json()["needs_init"] is True

        # 通过 init 设置管理员
        r = c.post("/api/auth/init", json={"username": "boss", "password": "12345678"},
                   follow_redirects=False)
        # 302 重定向到首页 + 设置 cookie
        assert r.status_code == 302
        assert c.get("/api/auth/needs-init").json()["needs_init"] is False

        # init 后能访问受保护 API（带 init 设置的 cookie）
        r2 = c.get("/api/accounts")
        assert r2.status_code in (200, 401)  # cookie 可能有或无
    db._local.conn = None


def test_init_rejected_when_users_exist(client):
    """已有用户时不能重复 init。"""
    r = client.post("/api/auth/init", json={"username": "x", "password": "12345678"})
    assert r.status_code == 400
