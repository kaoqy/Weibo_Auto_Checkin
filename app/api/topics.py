"""超话管理 API（v1.3.0）。"""

from __future__ import annotations

import hashlib
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

def _extract_ai_content(result: dict) -> str:
    """安全提取 API 响应中的正文；部分网关在内容审核或仅回 usage 时会返回空 choices。"""
    choices = result.get("choices") or []
    if not choices:
        return ""
    msg = (choices[0] or {}).get("message") or {}
    return (msg.get("content") or "").strip()


def call_ai_summary(text: str, topic_name: str = "超话", stream: bool = False) -> dict:
    """直接调用 AI 总结（不依赖 FastAPI 路由）。"""
    base_url = (database.get_setting("ai_base_url", "") or "").strip().rstrip("/")
    api_key = (database.get_setting("ai_api_key", "") or "").strip()
    model = (database.get_setting("ai_model", "") or "gpt-4o-mini").strip()

    if not base_url or not api_key:
        return {"ok": False, "summary": "", "error": "未配置 AI 总结功能"}

    truncated_text = text[:8000] if len(text) > 8000 else text
    system_content = AI_TOPIC_PROMPT.replace("{topic_name}", topic_name)
    user_content = f"以下是「{topic_name}」超话的最新帖子内容：\n\n{truncated_text}"
    messages = [
        {"role": "system", "content": system_content},
        {"role": "user", "content": user_content},
    ]

    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": 1200,
        "temperature": 0.7,
        "stream": stream,
    }

    try:
        if stream:
            # Non-streaming fallback for scheduler
            payload["stream"] = False
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
        summary = _extract_ai_content(result)
        if not summary:
            log.warning("AI 返回空内容 (model=%s)", result.get("model", model))
            return {"ok": False, "summary": "", "error": "AI 服务未返回有效内容，请稍后重试"}
        return {"ok": True, "summary": summary, "model": result.get("model", model)}
    except requests.exceptions.Timeout:
        return {"ok": False, "summary": "", "error": "请求超时"}
    except Exception as exc:
        return {"ok": False, "summary": "", "error": f"调用失败：{exc}"}


router = APIRouter(prefix="/api/topics", tags=["topics"])

log = logging.getLogger("weibo.topics")

IMG_CACHE_DIR = database.DB_PATH.parent / "img_cache"
IMG_CACHE_DIR.mkdir(parents=True, exist_ok=True)


def _detect_image_ext(content: bytes) -> str | None:
    """从 magic bytes 检测图片扩展名（替代已弃用的 imghdr）。"""
    if content[:8] == bytes([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A]):
        return ".png"
    if content[:3] == bytes([0xFF, 0xD8, 0xFF]):
        return ".jpg"
    if content[:6] in (b'GIF87a', b'GIF89a'):
        return ".gif"
    if content[:4] == b'RIFF' and content[8:12] == b'WEBP':
        return ".webp"
    if content[:2] == b'BM':
        return ".bmp"
    return None


# 硬编码的 AI 提示词
AI_TOPIC_PROMPT = """你是微博「{topic_name}」超话的内容分析助手。请基于用户提供的最新帖子，输出一份简洁、信息密度高的中文摘要。

按以下结构输出（Markdown）：

## 📋 内容概览
用 1-2 句话点明本期超话具体在讨论什么，直接说清话题本身，不要泛泛而谈。

## 🔥 热门话题
列出 2-3 个最受关注的话题，每条一行：话题名称 —— 受关注的原因，尽量引用帖子中的具体内容或数据（转发/评论/点赞量）。

## 💬 互动与氛围
用 2-3 句话概括粉丝互动特点与社区整体情绪走向。

要求：
- 只依据所给帖子内容，严禁编造；若帖子稀少、重复或内容空洞，如实说明即可，不要硬凑
- 人名、作品名、事件名等专有名词保留原文
- 客观中立、简洁流畅，全文控制在 300 字以内
- 直接从「## 📋 内容概览」开始输出，不要开场白和结尾客套"""


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
        ext = _detect_image_ext(content) or ext
        local_path = IMG_CACHE_DIR / f"{url_hash}{ext}"
        local_path.write_bytes(content)
        return local_path
    except Exception as exc:
        log.warning("图片下载失败 %s: %s", url[:80], exc)
        return None


# ========================= 单账号关注超话缓存 =========================

class TopicPushIn(BaseModel):
    push_enabled: int = 0


@router.patch("/push/{topic_id}")
def set_topic_push(topic_id: str, data: TopicPushIn, user=Depends(auth.require_admin)):
    """设置超话是否启用每日推送"""
    database.upsert_all_topic(topic_id=topic_id, push_enabled=data.push_enabled)
    return {"ok": True, "topic_id": topic_id, "push_enabled": data.push_enabled}


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
        avatar = t.get("avatar", "")
        desc = t.get("description", "")
        member = t.get("member_count", 0)
        if new_name:
            database.upsert_all_topic(
                topic_id=cid,
                name=new_name,
                topic_url=f"https://weibo.com/page/{cid}",
                avatar_url=avatar,
                description=desc,
                member_count=member,
            )
        elif existing:
            database.upsert_all_topic(
                topic_id=cid,
                name=existing.get("name", ""),
                topic_url=f"https://weibo.com/page/{cid}",
                avatar_url=avatar,
                description=desc,
                member_count=member,
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
                avatar = t.get("avatar", "")
                desc = t.get("description", "")
                member = t.get("member_count", 0)
                if new_name:
                    database.upsert_all_topic(
                        topic_id=cid,
                        name=new_name,
                        topic_url=f"https://weibo.com/page/{cid}",
                        avatar_url=avatar,
                        description=desc,
                        member_count=member,
                    )
                elif existing:
                    database.upsert_all_topic(
                        topic_id=cid,
                        name=existing.get("name", ""),
                        topic_url=f"https://weibo.com/page/{cid}",
                        avatar_url=avatar,
                        description=desc,
                        member_count=member,
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
    result = database.get_all_topics(limit=limit, offset=offset)
    # 通过本站图片代理加载超话头像，避免微博图床的防盗链影响列表显示。
    for topic in result.get("items", []):
        avatar = topic.get("avatar_url", "")
        if avatar.startswith(("http://", "https://")):
            topic["avatar_url"] = f"/api/topics/img?url={escape(avatar)}"
    return result


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
    stream: bool = False


@router.post("/ai_summary")
def ai_summary(data: AISummaryIn, user=Depends(auth.require_admin)):
    """调用 OpenAI 兼容 API 对超话内容进行结构化总结。"""
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

    try:
        if data.stream:
            # SSE streaming response
            # 先建立上游连接，让鉴权/模型/网关错误仍由本接口转成 JSON 错误，
            # 避免流已经开始后只剩一个空白面板。
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

            def generate():
                import json as _json
                buffer = b""
                try:
                    for chunk in resp_stream.iter_content(chunk_size=2048):
                        if not chunk:
                            continue
                        buffer += chunk
                        while b"\n\n" in buffer:
                            message, buffer = buffer.split(b"\n\n", 1)
                            for msg_line in message.split(b"\n"):
                                msg_line = msg_line.strip()
                                if not msg_line:
                                    continue
                                try:
                                    line_str = msg_line.decode("utf-8")
                                except UnicodeDecodeError:
                                    continue
                                if line_str.startswith("data: "):
                                    data_str = line_str[6:].strip()
                                    if not data_str or data_str == "[DONE]":
                                        break
                                    try:
                                        chunk_data = _json.loads(data_str)
                                        choices = chunk_data.get("choices") or []
                                        choice = choices[0] if choices else {}
                                        delta = choice.get("delta") or {}
                                        text_piece = delta.get("content", "") or ""
                                        if text_piece:
                                            out = {"text": text_piece}
                                            yield f"data: {_json.dumps(out, ensure_ascii=False)}\n\n"
                                        if choice.get("finish_reason"):
                                            out_done = {"finish": True}
                                            usage = chunk_data.get("usage")
                                            if usage:
                                                out_done["usage"] = usage
                                            mdl = chunk_data.get("model")
                                            if mdl:
                                                out_done["model"] = mdl
                                            yield f"data: {_json.dumps(out_done, ensure_ascii=False)}\n\n"
                                            break
                                    except _json.JSONDecodeError:
                                        buffer = msg_line + b"\n" + buffer
                                        break
                                    except Exception as exc:
                                        out_err = {"error": str(exc)}
                                        yield f"data: {_json.dumps(out_err, ensure_ascii=False)}\n\n"
                                        break
                except Exception as exc:
                    out_err = {"error": f"流式读取失败：{exc}"}
                    yield f"data: {_json.dumps(out_err, ensure_ascii=False)}\n\n"
                finally:
                    close = getattr(resp_stream, "close", None)
                    if close:
                        close()

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
            summary = _extract_ai_content(result)
            if not summary:
                log.warning("AI 返回空内容 (model=%s)", result.get("model", model))
                return {"ok": False, "summary": "", "error": "AI 服务未返回有效内容（可能被内容审核拦截），请稍后重试"}
            out = {
                "ok": True,
                "summary": summary,
                "model": result.get("model", model),
            }
            return out
    except requests.exceptions.Timeout:
        return {"ok": False, "summary": "", "error": "请求超时，请稍后重试"}
    except Exception as exc:
        log.error("AI 总结失败: %s", exc)
        return {"ok": False, "summary": "", "error": f"调用失败：{exc}"}


# ========================= TG 推送超话摘要 =========================

class TopicSummaryPushIn(BaseModel):
    text: str
    topic_name: str = "超话"


@router.post("/push_tg")
def push_topic_summary(data: TopicSummaryPushIn, user=Depends(auth.require_admin)):
    """推送超话 AI 摘要到 Telegram"""
    base_url = (database.get_setting("ai_base_url", "") or "").strip().rstrip("/")
    api_key = (database.get_setting("ai_api_key", "") or "").strip()
    model = (database.get_setting("ai_model", "") or "gpt-4o-mini").strip()

    if not base_url or not api_key:
        return {"ok": False, "error": "未配置 AI 总结功能"}

    truncated_text = data.text[:8000] if len(data.text) > 8000 else data.text

    # 构建 prompt
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
    }

    try:
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
        summary = _extract_ai_content(result)
        if not summary:
            log.warning("推送超话摘要：AI 返回空内容")
            return {"ok": False, "error": "AI 服务未返回有效内容，推送已取消"}

        # 推送到 TG
        from ..notifier import send_telegram
        tg_text = f"📊 {data.topic_name} 超话摘要\n\n{summary}"
        send_telegram(tg_text, title="超话摘要")

        return {"ok": True, "summary": summary}
    except Exception as exc:
        log.error("推送超话摘要失败: %s", exc)
        return {"ok": False, "error": f"推送失败：{exc}"}
