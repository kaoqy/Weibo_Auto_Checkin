"""超话管理 API（v1.3.0）。"""

from __future__ import annotations

import hashlib
import imghdr
import logging
from pathlib import Path
from urllib.parse import quote as escape

import requests
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .. import auth, database
from ..weibo_client import (
    fetch_topic_posts,
    get_followed_topics,
    normalize_cookie,
)

router = APIRouter(prefix="/api/topics", tags=["topics"])

log = logging.getLogger("weibo.topics")

IMG_CACHE_DIR = database.DB_PATH.parent / "img_cache"
IMG_CACHE_DIR.mkdir(parents=True, exist_ok=True)

# 硬编码的 AI 提示词
AI_TOPIC_PROMPT = """你是一个专业的微博超话内容分析助手。请根据以下超话帖子内容进行深度总结分析。

请按照以下结构输出总结内容（使用 Markdown 格式）：

## 📋 内容概览
（50字以内）：概括本期超话的核心话题与讨论焦点

## 🔥 热门话题
（100字以内）：提取2-3个最受关注的具体话题或事件，附带相关数据（如转发量、评论数等）

## 💬 互动分析
（50字以内）：分析粉丝互动特点，包括转发、评论、点赞的趋势

## 🎭 整体氛围
（50字以内）：总结超话社区的整体情感倾向和活跃程度

注意事项：
- 保持客观中立，不要添加个人观点
- 使用简洁流畅的中文表达
- 直接输出总结内容，不要任何前缀或格式标记
- 如果帖子内容较少或质量不高，请如实说明"""


def _download_image(url: str) -> Path | None:
    if not url or not url.startswith(("http://", "https://")):
        return None
    url_hash = hashlib.md5(url.encode()).hexdigest()
    ext = ".jpg"
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
        detected = imghdr.what(None, content)
        ext_map = {"png": ".png", "webp": ".webp", "gif": ".gif", "jpeg": ".jpg"}
        ext = ext_map.get(detected, ext)
        local_path = IMG_CACHE_DIR / f"{url_hash}{ext}"
        local_path.write_bytes(content)
        return local_path
    except Exception as exc:
        log.warning("图片下载失败 %s: %s", url[:80], exc)
        return None


# ========================= 单账号关注超话缓存 =========================

@router.get("/cache/{account_id}")
def get_cached_topics(account_id: int, user=Depends(auth.require_admin)):
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


@router.post("/refresh")
def refresh_topics(data: RefreshIn, user=Depends(auth.require_admin)):
    """刷新指定账号的关注超话"""
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

    from ..weibo_client import CheckinOptions
    opts = CheckinOptions.from_settings(database.get_setting)

    try:
        topics = get_followed_topics(
            session, cookie, channel=channel, proxy=proxy,
            force=opts.proxy_force, allow_fallback=opts.proxy_fallback,
        )
    except Exception as exc:
        raise HTTPException(502, f"获取超话列表失败：{exc}") from exc

    for t in topics:
        cid = t.get("id", "")
        if cid:
            t["url"] = f"https://weibo.com/page/{cid}"

    database.set_topic_cache(data.account_id, topics)

    # 合并进全量去重表，但不覆盖已有名称
    for t in topics:
        cid = t.get("id", "")
        if not cid:
            continue
        new_name = t.get("name", "").strip()
        existing = database.get_all_topic(cid)
        if new_name:
            database.upsert_all_topic(
                topic_id=cid,
                name=new_name,
                topic_url=f"https://weibo.com/page/{cid}",
            )
        elif existing:
            database.upsert_all_topic(
                topic_id=cid,
                name=existing.get("name", ""),
                topic_url=f"https://weibo.com/page/{cid}",
            )

    return {"ok": True, "count": len(topics), "topics": topics}


@router.post("/refresh_all")
def refresh_all_topics(user=Depends(auth.require_admin)):
    """刷新所有账号的关注超话"""
    accounts = database.get_accounts()
    if not accounts:
        return {"ok": True, "results": [], "message": "没有账号"}

    results = []
    for acc in accounts:
        cookie = normalize_cookie(acc.get("cookie") or acc.get("cookie_raw") or "")
        if not cookie:
            results.append({
                "account_id": acc["id"],
                "account_name": acc.get("name", ""),
                "ok": False,
                "error": "Cookie 为空",
                "count": 0,
            })
            continue

        try:
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

            topics = get_followed_topics(
                session, cookie, channel=channel, proxy=proxy,
                force=opts.proxy_force, allow_fallback=opts.proxy_fallback,
            )

            for t in topics:
                cid = t.get("id", "")
                if cid:
                    t["url"] = f"https://weibo.com/page/{cid}"

            database.set_topic_cache(acc["id"], topics)

            for t in topics:
                cid = t.get("id", "")
                if not cid:
                    continue
                new_name = t.get("name", "").strip()
                existing = database.get_all_topic(cid)
                if new_name:
                    database.upsert_all_topic(
                        topic_id=cid,
                        name=new_name,
                        topic_url=f"https://weibo.com/page/{cid}",
                    )
                elif existing:
                    database.upsert_all_topic(
                        topic_id=cid,
                        name=existing.get("name", ""),
                        topic_url=f"https://weibo.com/page/{cid}",
                    )

            results.append({
                "account_id": acc["id"],
                "account_name": acc.get("name", ""),
                "ok": True,
                "count": len(topics),
            })
        except Exception as exc:
            results.append({
                "account_id": acc["id"],
                "account_name": acc.get("name", ""),
                "ok": False,
                "error": str(exc),
                "count": 0,
            })

    total = sum(r.get("count", 0) for r in results)
    errors = [r for r in results if not r.get("ok")]

    return {
        "ok": True,
        "results": results,
        "total": total,
        "errors": len(errors),
        "message": f"共获取 {total} 个超话" + (f"，{len(errors)} 个账号失败" if errors else ""),
    }


# ========================= 全部去重超话 =========================

@router.get("/all")
def list_all_topics(limit: int = 50, offset: int = 0,
                    user=Depends(auth.require_admin)):
    return database.get_all_topics(limit=limit, offset=offset)


@router.delete("/all")
def clear_all_topics(user=Depends(auth.require_admin)):
    n = database.clear_all_topics()
    return {"ok": True, "removed": n}


# ========================= 超话帖子缓存 =========================

@router.get("/posts_cache/{topic_id}")
def get_cached_posts(topic_id: str, user=Depends(auth.require_admin)):
    """获取超话帖子缓存"""
    cache = database.get_topic_posts_cache(topic_id)
    if cache is None:
        return {"cached": False, "posts": [], "fetched_at": None}
    return {
        "cached": True,
        "posts": cache["posts"],
        "fetched_at": cache["fetched_at"],
    }


# ========================= 超话内容拉取 =========================

@router.get("/posts/{topic_id}")
def get_topic_posts(topic_id: str, count: int = 20,
                    force: bool = False, user=Depends(auth.require_admin)):
    """拉取指定超话的最新帖子。
    默认使用缓存（cache-first），force=true 时强制刷新。
    微博超话帖子接口是公开的，不需要登录态。
    """
    # Cache-first: return cached data unless force=true
    if not force:
        cached = database.get_topic_posts_cache(topic_id)
        if cached and cached.get("posts"):
            return {
                "ok": True,
                "topic_id": topic_id,
                "account_used": 0,
                "account_name": "",
                "posts": cached["posts"],
                "count": len(cached["posts"]),
                "error": "",
                "cached": True,
                "fetched_at": cached.get("fetched_at", ""),
            }

    # 创建无 Cookie 的公开请求会话
    session = requests.Session()
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

    # 尝试用账号的代理（如果有），但不需要 Cookie
    accounts = database.get_accounts()
    proxy = None
    channel = "direct"
    for acc in accounts:
        proxy_url = (acc.get("proxy") or "").strip()
        if proxy_url.startswith(("socks5://", "socks5h://")):
            proxy = proxy_url
            channel = "socks"
            break

    from ..weibo_client import CheckinOptions
    opts = CheckinOptions.from_settings(database.get_setting)

    try:
        result = fetch_topic_posts(
            session, cookies={}, containerid=topic_id,
            channel=channel, proxy=proxy,
            force=opts.proxy_force, allow_fallback=opts.proxy_fallback,
            count=min(count, 50),
        )
    except Exception as exc:
        return {
            "ok": True,
            "topic_id": topic_id,
            "account_used": 0,
            "account_name": "",
            "posts": [],
            "count": 0,
            "error": f"拉取失败：{exc}",
        }

    if result.get("posts"):
        posts = result["posts"]
        for p in posts:
            if p.get("pics"):
                p["pics"] = [
                    f"/api/topics/img?url={escape(url)}" if url.startswith(("http://", "https://")) else url
                    for url in p["pics"]
                ]
            user_obj = p.get("user", {})
            avatar = user_obj.get("profile_image_url", "")
            if avatar.startswith(("http://", "https://")):
                user_obj["profile_image_url"] = f"/api/topics/img?url={escape(avatar)}"

        database.set_topic_posts_cache(topic_id, posts)
        existing_topic = database.get_all_topic(topic_id)
        if existing_topic and existing_topic.get("name"):
            database.upsert_all_topic(topic_id=topic_id, name=existing_topic["name"], fetched_at=database._now())
        else:
            database.upsert_all_topic(topic_id=topic_id, fetched_at=database._now())

        return {
            "ok": True,
            "topic_id": topic_id,
            "account_used": 0,
            "account_name": "公开访问",
            "posts": posts,
            "count": len(posts),
            "error": "",
            "cached": False,
        }

    return {
        "ok": True,
        "topic_id": topic_id,
        "account_used": 0,
        "account_name": "",
        "posts": [],
        "count": 0,
        "error": result.get("error", "未获取到帖子"),
    }


class RefreshPostsIn(BaseModel):
    account_id: int = 0
    count: int = 20


@router.post("/refresh_posts/{topic_id}")
def refresh_posts(topic_id: str, data: RefreshPostsIn,
                  user=Depends(auth.require_admin)):
    """手动刷新超话帖子"""
    database.delete_topic_posts_cache(topic_id)
    return get_topic_posts(topic_id, count=data.count, force=True)


# ========================= 图片代理 =========================

@router.get("/img")
def proxy_image(url: str):
    """代理下载图片"""
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
    question: str = ""
    reasoning: bool = False
    stream: bool = False


@router.post("/ai_summary")
def ai_summary(data: AISummaryIn, user=Depends(auth.require_admin)):
    """调用 OpenAI 兼容 API 对超话内容进行总结或回答问题。

    支持模式：
    - 总结模式（默认）：无 question 时，用 AI_TOPIC_PROMPT 结构化总结超话帖子。
    - Q&A 模式：有 question 时，把帖子内容作为上下文，结合 question 生成回答。
    """
    base_url = (database.get_setting("ai_base_url", "") or "").strip().rstrip("/")
    api_key = (database.get_setting("ai_api_key", "") or "").strip()
    model = (database.get_setting("ai_model", "") or "gpt-4o-mini").strip()

    if not base_url or not api_key:
        return {
            "ok": False,
            "summary": "",
            "error": "未配置 AI 总结功能。请在设置中填写 API Base URL 和 API Key。",
        }

    truncated_text = data.text[:8000] if len(data.text) > 8000 else data.text

    # 构建 messages
    is_qa = bool(data.question and data.question.strip())
    if is_qa:
        # Q&A 模式：system 带上下文 + 分析指令，user 是问题
        system_content = (
            f"你是「{data.topic_name}」超话的内容分析助手。"
            f"以下是该超话最新的帖子内容，请基于这些内容回答用户的问题。"
            f"如果帖子中没有相关信息，请如实说明。\n\n"
            f"--- 帖子内容 ---\n{truncated_text}"
        )
        messages = [
            {"role": "system", "content": system_content},
            {"role": "user", "content": data.question.strip()},
        ]
    else:
        # 总结模式：结构化总结
        system_content = AI_TOPIC_PROMPT.replace("{topic_name}", data.topic_name)
        user_content = f"以下是「{data.topic_name}」超话的最新帖子内容：\n\n{truncated_text}"
        messages = [
            {"role": "system", "content": system_content},
            {"role": "user", "content": user_content},
        ]

    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": 1200,
        "temperature": 0.7,
        "stream": data.stream,
    }
    if data.reasoning:
        payload["reasoning_effort"] = "medium"

    try:
        if data.stream:
            # SSE streaming response
            def generate():
                import json as _json
                resp_stream = requests.post(
                    f"{base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                        "Accept": "text/event-stream",
                    },
                    json=payload,
                    timeout=120,
                    stream=True,
                )
                resp_stream.raise_for_status()
                for line in resp_stream.iter_lines(decode_unicode=True):
                    if line is None:
                        continue
                    s = line.strip()
                    if s.startswith("data: "):
                        chunk = s[6:]
                        if chunk == "[DONE]":
                            break
                        try:
                            chunk_data = _json.loads(chunk)
                            choice = chunk_data["choices"][0]
                            delta = choice.get("delta", {})
                            text_piece = delta.get("content", "")
                            reasoning_piece = delta.get("reasoning_content", "")
                            if text_piece or reasoning_piece:
                                out = {"text": text_piece, "reasoning": reasoning_piece}
                                yield f"data: {_json.dumps(out, ensure_ascii=False)}\n\n"
                            if choice.get("finish_reason"):
                                out_done = {"finish": True}
                                try:
                                    usage = chunk_data.get("usage")
                                    if usage:
                                        out_done["usage"] = usage
                                    mdl = chunk_data.get("model")
                                    if mdl:
                                        out_done["model"] = mdl
                                except Exception:
                                    pass
                                yield f"data: {_json.dumps(out_done, ensure_ascii=False)}\n\n"
                                break
                        except Exception as exc:
                            out_err = {"error": str(exc)}
                            yield f"data: {_json.dumps(out_err, ensure_ascii=False)}\n\n"
                            break

            from fastapi.responses import StreamingResponse
            return StreamingResponse(generate(), media_type="text/event-stream")
        else:
            # Non-streaming
            resp = requests.post(
                f"{base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=120,
            )
            resp.raise_for_status()
            result = resp.json()
            msg = result["choices"][0]["message"]
            summary = msg.get("content", "").strip()
            reasoning_content = msg.get("reasoning_content", "") or ""
            out = {
                "ok": True,
                "summary": summary,
                "model": result.get("model", model),
                "qa_mode": is_qa,
            }
            if reasoning_content:
                out["reasoning"] = reasoning_content
            return out
    except requests.exceptions.Timeout:
        return {"ok": False, "summary": "", "error": "请求超时，请稍后重试"}
    except Exception as exc:
        log.error("AI 总结失败: %s", exc)
        return {"ok": False, "summary": "", "error": f"调用失败：{exc}"}
