"""
微博超话签到客户端
复用原青龙脚本的核心逻辑，改造成可被管理面板调用的库。
"""
from __future__ import annotations

import json
import logging
import random
import re
import time
from datetime import datetime
from urllib.parse import parse_qs, urlparse

import requests

log = logging.getLogger("weibo.client")

# SOCKS 支持检测（缺失时给出明确提示）
try:
    import socks as _socks  # noqa: F401  (PySocks)
    SOCKS_AVAILABLE = True
except ImportError:
    SOCKS_AVAILABLE = False

BASE = "https://m.weibo.cn"
CONFIG_URL = BASE + "/api/config"
TOPICS_URL = BASE + "/api/container/getIndex"
FOLLOWED_CONTAINER = "100803_-_followsuper"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148"
    ),
    "Referer": BASE + "/",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "X-Requested-With": "XMLHttpRequest",
    "MWeibo-Pwa": "1",
}

_RETRYABLE = (
    requests.exceptions.ProxyError,
    requests.exceptions.ConnectionError,
    requests.exceptions.ConnectTimeout,
    requests.exceptions.ReadTimeout,
    requests.exceptions.SSLError,
)

# ========================= 超话页面（v1.3.0） =========================

TOPIC_POSTS_URL = BASE + "/api/container/getIndex"


def fetch_topic_posts(session, cookies, containerid: str, channel="auto",
                      proxy=None, force=False, allow_fallback=True, count: int = 20):
    """拉取指定超话的最新帖子列表（默认前 count 条）。

    返回 dict: {"posts": [...], "error": ""} 或 {"posts": [], "error": "具体错误原因"}。
    """
    posts = []
    error_msg = ""

    # 清理 containerid，去掉可能的后缀
    clean_cid = containerid.split("_-_")[0] if "_-_" in containerid else containerid

    # 按优先级尝试不同的容器ID格式（最新内容优先）
    cids_to_try = [
        f"{clean_cid}_-_new",        # new posts (chronological)
        clean_cid,                    # original format
        f"100808{clean_cid}",        # 100808 prefix format
    ]
    
    seen = set()
    unique_cids = []
    for c in cids_to_try:
        if c not in seen:
            seen.add(c)
            unique_cids.append(c)

    for cid in unique_cids:
        page = 1
        since_id = ""
        while len(posts) < count:
            params = {"containerid": cid, "page": page, "count": 25, "vtype": 12}
            if since_id:
                params["since_id"] = since_id
            try:
                payload = request_json(
                    session, "GET", TOPIC_POSTS_URL, params=params, cookies=cookies,
                    channel=channel, proxy=proxy, force=force,
                    allow_fallback=allow_fallback,
                )
            except NetworkError as exc:
                return {"posts": posts, "error": f"网络错误：{exc}"}
            except RuntimeError as exc:
                return {"posts": posts, "error": f"请求失败：{exc}"}
            except Exception as exc:
                return {"posts": posts, "error": f"未知错误：{exc}"}

            if payload.get("ok") == -100:
                break  # Cookie 过期，换下一个
            if payload.get("ok") != 1:
                break  # 换下一个 cid

            cards = (payload.get("data") or {}).get("cards", [])
            if not cards:
                break

            found = False
            for card in cards:
                card_group = card.get("card_group") or []
                for item in card_group:
                    if item.get("card_type") != "9":
                        continue
                    mblog = item.get("mblog") or {}
                    if not mblog:
                        continue
                    found = True
                    user = mblog.get("user") or {}
                    text = mblog.get("text", "")
                    import re as _re
                    text = _re.sub(r'<[^>]+>', '', text).strip()
                    posts.append({
                        "mid": mblog.get("idstr") or str(mblog.get("id", "")),
                        "text": text,
                        "created_at": mblog.get("created_at", ""),
                        "source": mblog.get("source", ""),
                        "reposts_count": mblog.get("reposts_count", 0),
                        "comments_count": mblog.get("comments_count", 0),
                        "attitudes_count": mblog.get("attitudes_count", 0),
                        "pics": [p.get("url", "") for p in (mblog.get("pics") or [])],
                        "user": {
                            "id": user.get("idstr") or str(user.get("id", "")),
                            "screen_name": user.get("screen_name", ""),
                            "profile_image_url": user.get("profile_image_url", "").replace("http://", "https://"),
                        },
                    })
                    if len(posts) >= count:
                        break
                if len(posts) >= count:
                    break
            if not found:
                break
            # Get next page cursor
            cardlist_info = (payload.get("data") or {}).get("cardlistInfo") or {}
            next_since = cardlist_info.get("since_id", "")
            if next_since:
                since_id = next_since
            else:
                page += 1
            time.sleep(0.3)

        if posts:
            break  # 有数据了

    # Deduplicate by mid
    seen_mids = set()
    unique_posts = []
    for p in posts:
        mid = p.get("mid", "")
        if mid and mid in seen_mids:
            continue
        seen_mids.add(mid)
        unique_posts.append(p)
    return {"posts": unique_posts[:count], "error": error_msg}


class NetworkError(RuntimeError):
    """整遍网络层失败。"""


# ========================= Cookie 工具 =========================

def normalize_cookie(raw) -> dict:
    """将 dict / 字符串 Cookie 统一成 dict。"""
    if isinstance(raw, dict):
        return {str(k): str(v) for k, v in raw.items() if v is not None}
    if isinstance(raw, str):
        raw = raw.strip()
        if raw.startswith("{"):
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    return normalize_cookie(parsed)
            except json.JSONDecodeError:
                pass
        result = {}
        for part in raw.split(";"):
            if "=" in part:
                key, value = part.split("=", 1)
                result[key.strip()] = value.strip()
        return result
    return {}


def cookie_to_string(cookie_dict: dict) -> str:
    """dict Cookie 转回字符串，便于回写。"""
    return "; ".join(f"{k}={v}" for k, v in cookie_dict.items() if v)


# ========================= 代理 =========================

def parse_proxies(raw: str) -> list[str]:
    """解析代理配置，支持换行 / 逗号 / 分号。仅保留 socks5:// 开头。"""
    if not raw:
        return []
    items = re.split(r"[\s,;]+", raw)
    urls = [i.rstrip("/") for i in items if i]
    return [u for u in urls if u.startswith(("socks5://", "socks5h://"))]


def proxy_proxies_dict(url: str) -> dict:
    return {"http": url, "https": url}


def proxy_display_name(url: str) -> str:
    parsed = urlparse(url)
    host = parsed.hostname or "未知"
    return f"{host}:{parsed.port}" if parsed.port else host


# ========================= 请求 =========================

def request_via_proxy(session, method, url, channel="auto", proxy=None,
                      force=False, allow_fallback=True, **kwargs):
    """
    channel: auto / socks / direct
    force: 严格代理，失败不回退直连。
    allow_fallback: 允许请求级失败回退直连。
    """
    if channel == "direct":
        kwargs.pop("proxies", None)
        return session.request(method, url, **kwargs)

    if proxy and channel in ("socks", "auto"):
        if not SOCKS_AVAILABLE:
            # 配置了 SOCKS 代理但未装 PySocks
            msg = ("配置了 SOCKS5 代理，但缺少 PySocks 支持。请安装：pip install PySocks "
                   "（或移除设置里的 SOCKS 代理节点改用直连）")
            log.error(msg)
            if channel == "socks" or force or not allow_fallback:
                raise NetworkError(msg)
            log.warning("PySocks 缺失，回退直连")
            kwargs.pop("proxies", None)
            return session.request(method, url, **kwargs)

        pk = dict(kwargs)
        merged = proxy_proxies_dict(proxy)
        merged.update(pk.get("proxies") or {})
        pk["proxies"] = merged
        try:
            return session.request(method, url, **pk)
        except _RETRYABLE as exc:
            if channel == "socks":
                raise NetworkError(f"socks 代理请求失败：{exc}") from exc
            if force or not allow_fallback:
                raise NetworkError(f"socks 代理请求失败：{exc}") from exc
            log.warning("socks 代理失败（%s），回退直连", exc)
        except Exception:
            raise

    kwargs.pop("proxies", None)
    return session.request(method, url, **kwargs)


def request_json(session, method, url, channel="auto", proxy=None,
                 force=False, allow_fallback=True, **kwargs):
    response = request_via_proxy(
        session, method, url, channel=channel, proxy=proxy,
        force=force, allow_fallback=allow_fallback,
        timeout=kwargs.pop("timeout", 15), **kwargs,
    )
    response.raise_for_status()
    try:
        return response.json()
    except ValueError as exc:
        raise RuntimeError(
            f"微博接口返回非 JSON：HTTP {response.status_code}，URL={url}"
        ) from exc


# ========================= 微博接口 =========================

def verify_cookie(session, cookies, channel="auto", proxy=None,
                  force=False, allow_fallback=True):
    try:
        data = request_json(
            session, "GET", CONFIG_URL, cookies=cookies,
            channel=channel, proxy=proxy, force=force,
            allow_fallback=allow_fallback,
        ).get("data", {})
        return bool(data.get("login")), data.get("st")
    except NetworkError:
        raise
    except requests.exceptions.RequestException as exc:
        raise NetworkError(f"验证 Cookie 网络失败：{exc}") from exc
    except Exception as exc:
        log.warning("验证 Cookie 失败：%s", exc)
        return False, None


def get_followed_topics(session, cookies, channel="auto", proxy=None,
                        force=False, allow_fallback=True):
    """获取关注超话列表，返回 [{name,id,scheme,done}]。"""
    topics = []
    since_id = ""
    while True:
        params = {"containerid": FOLLOWED_CONTAINER}
        if since_id:
            params["since_id"] = since_id
        payload = request_json(
            session, "GET", TOPICS_URL, params=params, cookies=cookies,
            channel=channel, proxy=proxy, force=force,
            allow_fallback=allow_fallback,
        )
        if payload.get("ok") != 1:
            break
        data = payload.get("data") or {}
        for card in data.get("cards", []):
            items = card.get("card_group") or [card]
            for item in items:
                if item.get("card_type") != "8" or not item.get("buttons"):
                    continue
                name = item.get("title_sub", "").strip()
                scheme = item.get("scheme", "")
                query = parse_qs(urlparse(scheme).query)
                topic_id = query.get("containerid", [""])[0]
                button = item["buttons"][0]
                button_name = button.get("name", "")
                button_scheme = button.get("scheme")
                done = (
                    button_name in ("已签", "已簽", "已签到", "已簽到")
                    or not button_scheme
                )
                if name:
                    topics.append({
                        "name": name,
                        "id": topic_id,
                        "scheme": None if done else button_scheme,
                        "done": done,
                    })
        since_id = (data.get("cardlistInfo") or {}).get("since_id", "")
        if not since_id:
            break
        time.sleep(0.5)
    return topics


def checkin_topic(session, cookies, scheme, st, channel="auto", proxy=None,
                  force=False, allow_fallback=True):
    if not scheme:
        raise RuntimeError("缺少签到链接 scheme")
    url = scheme if scheme.startswith("http") else BASE + scheme
    separator = "&" if "?" in url else "?"
    return request_json(
        session, "GET", f"{url}{separator}st={st}", cookies=cookies,
        channel=channel, proxy=proxy, force=force, allow_fallback=allow_fallback,
    )


def merge_refreshed_cookies(session, cookie_dict: dict) -> tuple[dict, list]:
    """合并响应中的 Set-Cookie，返回 (新dict, 变化的key列表)。

    只有「原 cookie_dict 中已存在、且值实际发生变化」的 key 才计入 changed。
    响应里新出现的无关 cookie 只并入 merged，但不触发回写，避免每次签到
    都因无关 cookie 被判为「有变动」而频繁写数据库。
    """
    merged = dict(cookie_dict)
    changed = []
    for cookie in session.cookies:
        if not cookie.value:
            continue
        if cookie.name in cookie_dict and cookie_dict[cookie.name] != cookie.value:
            changed.append(cookie.name)
        merged[cookie.name] = cookie.value
    return merged, changed


# ========================= 单账号签到 =========================

class CheckinOptions:
    """签到参数配置。"""

    def __init__(self, checkin_delay_min=3, checkin_delay_max=8,
                 proxies=None, proxy_force=False, proxy_fallback=True):
        self.checkin_delay_min = checkin_delay_min
        self.checkin_delay_max = checkin_delay_max
        self.proxies = proxies or []
        self.proxy_force = proxy_force
        self.proxy_fallback = proxy_fallback

    @classmethod
    def from_settings(cls, db_get):
        return cls(
            checkin_delay_min=int(db_get("checkin_delay_min", "3") or 3),
            checkin_delay_max=int(db_get("checkin_delay_max", "8") or 8),
            proxies=parse_proxies(db_get("proxies", "")),
            proxy_force=db_get("proxy_force", "0") == "1",
            proxy_fallback=db_get("proxy_fallback", "1") != "0",
        )


def run_account_checkin(cookie_dict: dict, opts: CheckinOptions,
                        proxy_url: str | None = None,
                        proxy_index: int = 0) -> dict:
    """
    对单个账号执行一遍签到。
    - proxy_url: 账号指定使用的 socks 链接（优先）
    - proxy_index: 无指定时按 opts.proxies 轮询的序号（兼容旧逻辑）
    返回 dict：{status, channel, total, success, fail, message, results, cookie, cookie_changed}
    """
    cookie_dict = normalize_cookie(cookie_dict)
    if not cookie_dict:
        return _bundle("failed", "Cookie 为空，请重新登录", 0, 0, 0, [],
                       cookie_dict, [], failure_type="cookie_invalid")

    # 代理选择：账号指定 proxy_url 优先；否则按 opts.proxies 轮询
    proxy = None
    channel = "direct"
    if proxy_url and proxy_url.strip().startswith(("socks5://", "socks5h://")):
        proxy = proxy_url.strip()
        channel = "socks"
    elif opts.proxies:
        proxy = opts.proxies[proxy_index % len(opts.proxies)]
        channel = "socks"

    session = requests.Session()
    session.headers.update(HEADERS)

    try:
        logged_in, st = verify_cookie(
            session, cookie_dict, channel=channel, proxy=proxy,
            force=opts.proxy_force, allow_fallback=opts.proxy_fallback,
        )
    except NetworkError as exc:
        return _bundle("failed", f"验证 Cookie 网络失败：{exc}", 0, 0, 0, [],
                       cookie_dict, [], channel, failure_type="network")

    if not logged_in:
        merged, changed = merge_refreshed_cookies(session, cookie_dict)
        return _bundle("failed", "Cookie 无效或已过期，请重新登录", 0, 0, 0,
                       [], merged, changed, channel,
                       failure_type="cookie_invalid")

    try:
        topics = get_followed_topics(
            session, cookie_dict, channel=channel, proxy=proxy,
            force=opts.proxy_force, allow_fallback=opts.proxy_fallback,
        )
    except NetworkError as exc:
        return _bundle("failed", f"获取超话列表网络失败：{exc}", 0, 0, 0, [],
                       cookie_dict, [], channel)

    if not topics:
        merged, changed = merge_refreshed_cookies(session, cookie_dict)
        return _bundle("success", "没有关注超话", 0, 0, 0, [],
                       merged, changed, channel)

    results = []
    total = len(topics)
    for idx, topic in enumerate(topics, start=1):
        if topic["done"]:
            results.append({"name": topic["name"], "success": True,
                            "message": "今日已签到"})
            continue
        try:
            response = checkin_topic(
                session, cookie_dict, topic["scheme"], st,
                channel=channel, proxy=proxy,
                force=opts.proxy_force, allow_fallback=opts.proxy_fallback,
            )
            resp_text = str(response)
            if str(response.get("errno")) == "100015" or "验签" in resp_text or "驗簽" in resp_text:
                # 刷新 st 重试
                logged_in, st = verify_cookie(
                    session, cookie_dict, channel=channel, proxy=proxy,
                    force=opts.proxy_force, allow_fallback=opts.proxy_fallback,
                )
                if not logged_in:
                    raise RuntimeError("Cookie 在签到过程中失效")
                response = checkin_topic(
                    session, cookie_dict, topic["scheme"], st,
                    channel=channel, proxy=proxy,
                    force=opts.proxy_force, allow_fallback=opts.proxy_fallback,
                )
            message = response.get("msg", "")
            success = (
                response.get("ok") == 1
                or "成功" in message
                or "已签到" in message
                or "已簽到" in message
            )
            results.append({"name": topic["name"], "success": success,
                            "message": message or str(response)[:100]})
        except NetworkError:
            # 签到请求网络失败，整遍判为网络失败
            merged, changed = merge_refreshed_cookies(session, cookie_dict)
            return _bundle("failed", "签到过程中网络失败", total,
                           sum(1 for r in results if r["success"]),
                           len(results) - sum(1 for r in results if r["success"]),
                           results, merged, changed, channel)
        except Exception as exc:
            results.append({"name": topic["name"], "success": False,
                            "message": str(exc)})
        if idx < total:
            time.sleep(random.uniform(opts.checkin_delay_min, opts.checkin_delay_max))

    merged, changed = merge_refreshed_cookies(session, cookie_dict)
    success_count = sum(1 for r in results if r["success"])
    fail_count = total - success_count
    status = "success" if fail_count == 0 else "partial"
    return _bundle(status, "签到完成", total, success_count, fail_count,
                   results, merged, changed, channel)


def _bundle(status, message, total, success, fail, results, cookie,
            cookie_changed, channel="direct", failure_type=None):
    return {
        "status": status,
        "message": message,
        "total": total,
        "success": success,
        "fail": fail,
        "results": results,
        "cookie": cookie,
        "cookie_changed": cookie_changed,
        "channel": channel,
        "failure_type": failure_type,
    }
