"""Deterministic topic post preparation and engagement statistics."""
from __future__ import annotations

import hashlib
import re
from collections import Counter
from datetime import datetime, timedelta

from .weibo_client import _clean_mblog_text


def normalize_post(post: dict) -> dict:
    """Create a stable, compact object suitable for storage and model context."""
    text = _clean_mblog_text(post.get("text_raw") or post.get("text") or "")
    retweeted = post.get("retweeted") or {}
    return {
        "mid": str(post.get("mid") or ""),
        "bid": post.get("bid") or "",
        "text": text,
        "created_at": post.get("created_at") or "",
        "user": {"id": str((post.get("user") or {}).get("id") or ""),
                 "screen_name": (post.get("user") or {}).get("screen_name") or "未知用户"},
        "reposts_count": _count(post.get("reposts_count")),
        "comments_count": _count(post.get("comments_count")),
        "attitudes_count": _count(post.get("attitudes_count")),
        "url": post.get("url") or (f"https://weibo.com/detail/{post.get('mid')}" if post.get("mid") else ""),
        "is_repost": bool(retweeted),
        "retweeted": retweeted,
    }


def _count(value) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def engagement(post: dict) -> int:
    return (int(post.get("reposts_count") or 0) * 3
            + int(post.get("comments_count") or 0) * 2
            + int(post.get("attitudes_count") or 0))


def content_key(post: dict) -> str:
    normalized = re.sub(r"https?://\S+|#[^#]+#|\s+", "", post.get("text") or "").lower()
    return hashlib.sha1(normalized.encode()).hexdigest() if normalized else str(post.get("mid") or "")


def filter_posts(posts: list[dict], hours: int = 0, keyword: str = "",
                 min_engagement: int = 0, hide_reposts: bool = False) -> list[dict]:
    now = datetime.now()
    keyword = keyword.strip().casefold()
    result = []
    for post in posts:
        if hide_reposts and post.get("is_repost"):
            continue
        if engagement(post) < min_engagement:
            continue
        if keyword and keyword not in (post.get("text") or "").casefold():
            continue
        if hours:
            try:
                created = datetime.strptime(post.get("created_at", ""), "%Y-%m-%d %H:%M:%S")
                if created < now - timedelta(hours=hours):
                    continue
            except ValueError:
                pass
        result.append(post)
    return result


def deduplicate_posts(posts: list[dict]) -> list[dict]:
    seen_ids, seen_text, unique = set(), set(), []
    for post in posts:
        mid = post.get("mid") or ""
        key = content_key(post)
        if (mid and mid in seen_ids) or (key and key in seen_text):
            continue
        if mid:
            seen_ids.add(mid)
        if key:
            seen_text.add(key)
        unique.append(post)
    return unique


def prepare_posts(posts: list[dict], limit: int = 30) -> list[dict]:
    prepared = deduplicate_posts([normalize_post(p) for p in posts])
    prepared.sort(key=lambda p: (engagement(p), p.get("created_at") or ""), reverse=True)
    for post in prepared:
        post["engagement"] = engagement(post)
    return prepared[:max(1, min(limit, 100))]


def summarize_statistics(posts: list[dict]) -> dict:
    hashtags = Counter(tag for p in posts for tag in re.findall(r"#([^#\n]{1,60})#", p.get("text") or ""))
    return {
        "posts": len(posts),
        "authors": len({p.get("user", {}).get("id") or p.get("user", {}).get("screen_name") for p in posts}),
        "reposts": sum(p.get("is_repost", False) for p in posts),
        "comments": sum(p.get("comments_count", 0) for p in posts),
        "likes": sum(p.get("attitudes_count", 0) for p in posts),
        "shares": sum(p.get("reposts_count", 0) for p in posts),
        "hashtags": [{"name": name, "count": count} for name, count in hashtags.most_common(8)],
        "top_mids": [p.get("mid") for p in sorted(posts, key=engagement, reverse=True)[:5]],
    }


def build_analysis_context(posts: list[dict], topic_name: str, limit: int = 30) -> tuple[str, dict]:
    prepared = prepare_posts(posts, limit=limit)
    stats = summarize_statistics(prepared)
    lines = [f"超话：{topic_name}", f"样本：{stats['posts']} 条帖子，{stats['authors']} 位作者"]
    for i, post in enumerate(prepared, 1):
        text = re.sub(r"\s+", " ", post.get("text") or "").strip()[:1200]
        if not text:
            continue
        lines.append(f"[{i}] ID={post['mid']} 时间={post['created_at']} 作者={post['user']['screen_name']} "
                     f"转发={post['reposts_count']} 评论={post['comments_count']} 点赞={post['attitudes_count']}\n{text}")
    return "\n\n".join(lines), {"posts": prepared, "stats": stats}
