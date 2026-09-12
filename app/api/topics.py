"""超话管理 API（v1.3.0）。

提供：
- 单账号关注超话列表（带缓存，刷新按钮）
- 全部去重超话列表（独立侧栏页）
- 超话详情页内容拉取
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .. import auth, database
from ..weibo_client import (
    fetch_topic_posts,
    get_followed_topics,
    normalize_cookie,
)

router = APIRouter(prefix="/api/topics", tags=["topics"])

log = logging.getLogger("weibo.topics")


# ========================= 单账号关注超话缓存 =========================

@router.get("/cache/{account_id}")
def get_cached_topics(account_id: int, user: dict = Depends(auth.require_admin)):
    """读取单账号的关注超话缓存（不触发网络请求）。"""
    acc = database.get_account(account_id)
    if not acc:
        raise HTTPException(404, "账号不存在")
    cache = database.get_topic_cache(account_id)
    if cache is None:
        return {"cached": False, "topics": [], "cached_at": None}
    return {
        "cached": True,
        "topics": cache["topics"],
        "cached_at": cache["cached_at"],
    }


class RefreshIn(BaseModel):
    account_id: int
    force: bool = False


@router.post("/refresh")
def refresh_topics(data: RefreshIn, user: dict = Depends(auth.require_admin)):
    """手动刷新指定账号的关注超话（并写入缓存）。"""
    acc = database.get_account(data.account_id)
    if not acc:
        raise HTTPException(404, "账号不存在")
    cookie = normalize_cookie(acc.get("cookie") or acc.get("cookie_raw") or "")
    if not cookie:
        raise HTTPException(400, "账号 Cookie 为空")

    import requests as _req
    session = _req.Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148"
        ),
        "Referer": "https://m.weibo.cn/",
        "Accept": "application/json, text/plain, */*",
        "X-Requested-With": "XMLHttpRequest",
        "MWeibo-Pwa": "1",
    })

    proxy = (acc.get("proxy") or "").strip() or None
    channel = "socks" if proxy else "direct"
    opts_db = database.get_setting

    from ..weibo_client import CheckinOptions
    opts = CheckinOptions.from_settings(opts_db)

    try:
        topics = get_followed_topics(
            session, cookie, channel=channel, proxy=proxy,
            force=opts.proxy_force, allow_fallback=opts.proxy_fallback,
        )
    except Exception as exc:
        raise HTTPException(502, f"获取超话列表失败：{exc}") from exc

    # 构建超话 URL（page 格式，前端直接可跳转）
    for t in topics:
        cid = t.get("id", "")
        if cid:
            t["url"] = f"https://weibo.com/page/{cid}"

    database.set_topic_cache(data.account_id, topics)

    # 合并进全量去重表
    for t in topics:
        cid = t.get("id", "")
        if not cid:
            continue
        database.upsert_all_topic(
            topic_id=cid,
            name=t.get("name", ""),
            topic_url=f"https://weibo.com/page/{cid}",
        )

    return {"ok": True, "count": len(topics), "topics": topics}


# ========================= 全部去重超话 =========================

@router.get("/all")
def list_all_topics(limit: int = 50, offset: int = 0,
                    user: dict = Depends(auth.require_admin)):
    """全量去重超话列表（分页）。"""
    return database.get_all_topics(limit=limit, offset=offset)


@router.delete("/all")
def clear_all_topics(user: dict = Depends(auth.require_admin)):
    """清空全量超话列表。"""
    n = database.clear_all_topics()
    return {"ok": True, "removed": n}


# ========================= 超话内容拉取 =========================

@router.get("/posts/{topic_id}")
def get_topic_posts(topic_id: str, account_id: int = 0, count: int = 20,
                    user: dict = Depends(auth.require_admin)):
    """拉取指定超话的最新帖子（需要至少一个有效账号的 Cookie）。

    account_id 指定使用哪个账号拉取；如果为 0，取第一个有 Cookie 的启用账号。
    """
    acc = None
    if account_id:
        acc = database.get_account(account_id)
        if not acc:
            raise HTTPException(404, "指定账号不存在")
    else:
        for a in database.get_accounts():
            cookie = a.get("cookie") or a.get("cookie_raw") or ""
            if cookie.strip():
                acc = a
                break
        if not acc:
            raise HTTPException(400, "没有可用账号，请先添加 Cookie")

    cookie = normalize_cookie(acc.get("cookie") or acc.get("cookie_raw") or "")
    if not cookie:
        raise HTTPException(400, "账号 Cookie 为空")

    import requests as _req
    session = _req.Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148"
        ),
        "Referer": "https://m.weibo.cn/",
        "Accept": "application/json, text/plain, */*",
        "X-Requested-With": "XMLHttpRequest",
        "MWeibo-Pwa": "1",
    })

    proxy = (acc.get("proxy") or "").strip() or None
    channel = "socks" if proxy else "direct"

    from ..weibo_client import CheckinOptions
    opts = CheckinOptions.from_settings(database.get_setting)

    try:
        posts = fetch_topic_posts(
            session, cookie, containerid=topic_id,
            channel=channel, proxy=proxy,
            force=opts.proxy_force, allow_fallback=opts.proxy_fallback,
            count=min(count, 50),
        )
    except Exception as exc:
        raise HTTPException(502, f"拉取超话帖子失败：{exc}") from exc

    # 顺便更新 all_topics 里的 fetched_at
    database.upsert_all_topic(topic_id=topic_id, fetched_at=database._now())

    return {
        "ok": True,
        "topic_id": topic_id,
        "account_used": acc["id"],
        "account_name": acc.get("name", ""),
        "posts": posts,
        "count": len(posts),
    }
