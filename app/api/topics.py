"""超话管理 API（v1.3.0）。

提供：
- 单账号关注超话列表（带缓存，刷新按钮）
- 全部去重超话列表（独立侧栏页）
- 超话详情页内容拉取（含图片代理）
- AI 总结（OpenAI 兼容 API）
"""
from __future__ import annotations

import hashlib
import imghdr
import logging
from pathlib import Path
from urllib.parse import quote as escape

import requests
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

from .. import auth, database
from ..weibo_client import (
    fetch_topic_posts,
    get_followed_topics,
    normalize_cookie,
)

router = APIRouter(prefix="/api/topics", tags=["topics"])

log = logging.getLogger("weibo.topics")

# 图片缓存目录
IMG_CACHE_DIR = database.DB_PATH.parent / "img_cache"
IMG_CACHE_DIR.mkdir(parents=True, exist_ok=True)


def _download_image(url: str) -> Path | None:
    """下载图片到本地缓存，返回本地路径。"""
    if not url or not url.startswith(("http://", "https://")):
        return None
    # 用 URL hash 做文件名
    url_hash = hashlib.md5(url.encode()).hexdigest()
    ext = ".jpg"  # 默认
    # 尝试从 URL 取 ext
    lower = url.lower()
    for e in (".png", ".webp", ".gif", ".bmp"):
        if e in lower:
            ext = e
            break
    local_path = IMG_CACHE_DIR / f"{url_hash}{ext}"
    if local_path.exists() and local_path.stat().st_size > 0:
        return local_path
    try:
        resp = requests.get(url, timeout=15, headers={
            "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15",
            "Referer": "https://m.weibo.cn/",
        })
        resp.raise_for_status()
        content = resp.content
        if len(content) < 100:
            return None
        # 检测真实格式
        detected = imghdr.what(None, content)
        if detected == "png":
            ext = ".png"
        elif detected == "webp":
            ext = ".webp"
        elif detected == "gif":
            ext = ".gif"
        elif detected == "jpeg":
            ext = ".jpg"
        local_path = IMG_CACHE_DIR / f"{url_hash}{ext}"
        local_path.write_bytes(content)
        return local_path
    except Exception as exc:
        log.warning("图片下载失败 %s: %s", url[:80], exc)
        return None


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
        new_name = t.get("name", "").strip()
        existing = database.get_all_topic(cid)
        # 只有获取到非空名称时才更新，避免覆盖已有名称
        if new_name:
            database.upsert_all_topic(
                topic_id=cid,
                name=new_name,
                topic_url=f"https://weibo.com/page/{cid}",
            )
        elif existing:
            # 只更新 URL，不覆盖名称
            database.upsert_all_topic(
                topic_id=cid,
                name=existing.get("name", ""),
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
        result = fetch_topic_posts(
            session, cookie, containerid=topic_id,
            channel=channel, proxy=proxy,
            force=opts.proxy_force, allow_fallback=opts.proxy_fallback,
            count=min(count, 50),
        )
    except Exception as exc:
        raise HTTPException(502, f"拉取超话帖子失败：{exc}") from exc

    posts = result.get("posts", [])
    error = result.get("error", "")

    # 替换图片 URL 为本地代理
    for p in posts:
        if p.get("pics"):
            p["pics"] = [
                f"/api/topics/img?url={escape(url)}" if url.startswith(("http://", "https://")) else url
                for url in p["pics"]
            ]
        user = p.get("user", {})
        avatar = user.get("profile_image_url", "")
        if avatar.startswith(("http://", "https://")):
            user["profile_image_url"] = f"/api/topics/img?url={escape(avatar)}"

    database.upsert_all_topic(topic_id=topic_id, fetched_at=database._now())

    return {
        "ok": True,
        "topic_id": topic_id,
        "account_used": acc["id"],
        "account_name": acc.get("name", ""),
        "posts": posts,
        "count": len(posts),
        "error": error,
    }


# ========================= 图片代理 =========================

@router.get("/img")
def proxy_image(url: str):
    """代理下载图片（绕过防盗链，缓存到本地）。"""
    if not url:
        raise HTTPException(400, "url 为空")
    try:
        from urllib.parse import unquote
        url = unquote(url)
    except Exception:
        pass
    local_path = _download_image(url)
    if not local_path or not local_path.exists():
        raise HTTPException(404, "图片获取失败")
    # 根据扩展名返回正确的 Content-Type
    ext = local_path.suffix.lower()
    content_type_map = {
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".png": "image/png", ".webp": "image/webp",
        ".gif": "image/gif", ".bmp": "image/bmp",
    }
    media_type = content_type_map.get(ext, "application/octet-stream")
    return FileResponse(local_path, media_type=media_type)


# ========================= AI 总结 =========================

class AISummaryIn(BaseModel):
    text: str
    topic_name: str = "超话"


@router.post("/ai_summary")
def ai_summary(data: AISummaryIn, user: dict = Depends(auth.require_admin)):
    """调用 OpenAI 兼容 API 对超话内容进行总结。

    配置项（settings）：ai_base_url / ai_api_key / ai_model / ai_topic_prompt
    """
    base_url = (database.get_setting("ai_base_url", "") or "").strip().rstrip("/")
    api_key = (database.get_setting("ai_api_key", "") or "").strip()
    model = (database.get_setting("ai_model", "") or "gpt-4o-mini").strip()
    prompt_template = (database.get_setting("ai_topic_prompt", "") or
                       "你是一个超话内容总结助手。请对以下超话帖子内容进行简洁总结（200字以内），包括：1. 主要讨论话题 2. 热门帖子要点 3. 整体氛围。只输出总结文字，不要任何前缀或格式标记。")

    if not base_url or not api_key:
        return {
            "ok": False,
            "summary": "",
            "error": "未配置 AI 总结功能。请在设置中填写 API Base URL 和 API Key。",
        }

    # 构建提示词
    system_prompt = prompt_template.replace("{topic_name}", data.topic_name)
    # 截断文本避免超出上下文
    truncated_text = data.text[:8000] if len(data.text) > 8000 else data.text
    user_content = f"以下是「{data.topic_name}」超话的最新帖子内容：\n\n{truncated_text}"

    try:
        resp = requests.post(
            f"{base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content},
                ],
                "max_tokens": 500,
                "temperature": 0.7,
            },
            timeout=60,
        )
        resp.raise_for_status()
        result = resp.json()
        summary = result["choices"][0]["message"]["content"].strip()
        return {"ok": True, "summary": summary, "model": model}
    except requests.exceptions.Timeout:
        return {"ok": False, "summary": "", "error": "请求超时，请稍后重试"}
    except Exception as exc:
        log.error("AI 总结失败: %s", exc)
        return {"ok": False, "summary": "", "error": f"调用失败：{exc}"}
