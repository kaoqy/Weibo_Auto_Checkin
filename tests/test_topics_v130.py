"""v1.3.0 超话功能测试。"""
import sys
from pathlib import Path

import pytest
import requests  # noqa: F401  (用于 mock)

sys.path.insert(0, str(Path(__file__).resolve().parent))

import app.database as db  # noqa: E402
from app.main import app  # noqa: E402

# 测试口令
GOOD_PW = "secret" + "123"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_path = tmp_path / "test_topics.db"
    monkeypatch.setattr(db, "DB_PATH", db_path)
    db._local.conn = None
    db.init_db()
    from app import auth as auth_mod
    db.create_user("admin", auth_mod.hash_password(GOOD_PW))
    db.set_settings({"auth_enabled": "1"})
    auth_mod._login_fails.clear()
    auth_mod._login_locks.clear()

    from fastapi.testclient import TestClient
    with TestClient(app) as c:
        r = c.post("/api/auth/login", json={"username": "admin", "password": GOOD_PW})
        assert r.status_code == 200
        token = r.cookies.get(auth_mod.COOKIE_NAME)
        c.headers.update({"Cookie": f"{auth_mod.COOKIE_NAME}={token}"})
        yield c

    db._local.conn = None


def test_topic_cache_crud(tmp_path, monkeypatch):
    """超话缓存表的基本 CRUD。"""
    db_path = tmp_path / "test_tc.db"
    monkeypatch.setattr(db, "DB_PATH", db_path)
    db._local.conn = None
    db.init_db()

    # 创建账号（外键约束）
    acc_id = db.add_account({"name": "T", "cookie_raw": "SUB=x"})

    # 无缓存时应返回 None
    assert db.get_topic_cache(acc_id) is None

    # 写入缓存
    topics = [
        {"name": "测试超话1", "id": "100808abc", "scheme": None, "done": False},
        {"name": "测试超话2", "id": "100808def", "scheme": None, "done": True},
    ]
    db.set_topic_cache(acc_id, topics)

    cache = db.get_topic_cache(acc_id)
    assert cache is not None
    assert cache["account_id"] == acc_id
    assert len(cache["topics"]) == 2
    assert cache["topics"][0]["name"] == "测试超话1"

    # 再次写入（更新）
    db.set_topic_cache(acc_id, topics[:1])
    cache = db.get_topic_cache(acc_id)
    assert len(cache["topics"]) == 1

    # 清除缓存
    db.delete_topic_cache(acc_id)
    assert db.get_topic_cache(acc_id) is None

    db._local.conn = None


def test_all_topics_upsert_and_query(tmp_path, monkeypatch):
    """全量超话去重表。"""
    db_path = tmp_path / "test_at.db"
    monkeypatch.setattr(db, "DB_PATH", db_path)
    db._local.conn = None
    db.init_db()

    # 插入
    db.upsert_all_topic(topic_id="100808aaa", name="周深", description="歌手")
    db.upsert_all_topic(topic_id="100808bbb", name="陈奕迅", description="歌神")

    result = db.get_all_topics()
    assert result["total"] == 2

    # 重复插入应去重
    db.upsert_all_topic(topic_id="100808aaa", name="周深超话", description="歌手周深")
    result = db.get_all_topics()
    assert result["total"] == 2

    # 获取单条
    t = db.get_all_topic("100808aaa")
    assert t is not None
    assert t["name"] == "周深超话"

    # 清空
    n = db.clear_all_topics()
    assert n == 2
    assert db.get_all_topics()["total"] == 0

    db._local.conn = None


def test_topics_api_cache_and_refresh(client):
    """超话缓存和刷新接口。"""
    # 创建一个账号
    acc = client.post("/api/accounts", json={
        "name": "测试号", "cookie_raw": "SUB=valid; SCF=x",
    }).json()

    # 初始无缓存
    r = client.get(f"/api/topics/cache/{acc['id']}")
    assert r.status_code == 200
    assert r.json()["cached"] is False

    # 刷新（会被 mock 拦截）
    from unittest.mock import patch
    fake_topics = [
        {"name": "超话1", "id": "100808a", "scheme": "/checkin?a=1", "done": False},
        {"name": "超话2", "id": "100808b", "scheme": None, "done": True},
    ]
    with patch("app.api.topics.get_followed_topics", return_value=fake_topics):
        r = client.post("/api/topics/refresh", json={"account_id": acc["id"]})
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 2
    # 超话 URL 应自动构建
    assert body["topics"][0]["url"] == "https://weibo.com/page/100808a"

    # 缓存应已写入
    r = client.get(f"/api/topics/cache/{acc['id']}")
    assert r.json()["cached"] is True
    assert len(r.json()["topics"]) == 2

    # 全量超话应包含这两个
    r = client.get("/api/topics/all")
    assert r.json()["total"] == 2


def test_topics_api_posts_endpoint(client):
    """超话帖子拉取接口。"""
    # 创建一个有 Cookie 的账号
    acc = client.post("/api/accounts", json={
        "name": "测试号", "cookie_raw": "SUB=valid; SCF=x",
    }).json()

    from unittest.mock import patch
    fake_posts = [
        {
            "mid": "123",
            "text": "测试帖子内容",
            "created_at": "2026-09-12 12:00:00",
            "source": "微博",
            "reposts_count": 10,
            "comments_count": 20,
            "attitudes_count": 30,
            "pics": ["https://example.com/pic1.jpg"],
            "user": {
                "id": "456",
                "screen_name": "测试用户",
                "profile_image_url": "https://example.com/avatar.jpg",
            },
        }
    ]
    with patch("app.weibo_client.fetch_topic_posts", return_value={"posts": fake_posts, "error": ""}):
        r = client.get(f"/api/topics/posts/100808abc?count=20")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["count"] == 1
    assert body["posts"][0]["text"] == "测试帖子内容"
    assert body["account_used"] == acc["id"]
    # 图片 URL 应被替换为代理地址
    assert "/api/topics/img?url=" in body["posts"][0]["pics"][0]
    assert "/api/topics/img?url=" in body["posts"][0]["user"]["profile_image_url"]


def test_ai_summary_without_config(client):
    """未配置 AI 时应返回错误提示。"""
    r = client.post("/api/topics/ai_summary", json={"text": "测试", "topic_name": "测试"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert "未配置" in body["error"]


def test_ai_summary_with_mock(client, monkeypatch):
    """配置 AI 后应调用 API。"""
    db.set_settings({
        "ai_base_url": "https://api.openai.com/v1",
        "ai_api_key": "sk-test",
        "ai_model": "gpt-4o-mini",
        "ai_topic_prompt": "总结以下内容：",
    })

    class FakeResp:
        status_code = 200
        def json(self):
            return {"choices": [{"message": {"content": "这是 AI 总结内容"}}]}
        def raise_for_status(self):
            pass

    monkeypatch.setattr(requests, "post", lambda *a, **kw: FakeResp())
    r = client.post("/api/topics/ai_summary", json={"text": "帖子内容", "topic_name": "测试超话"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["summary"] == "这是 AI 总结内容"
    assert body["model"] == "gpt-4o-mini"
    db._local.conn = None
