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
BASE_PC = "https://weibo.com"
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


def _normalize_mblog(mblog_raw):
    """将 mblog 对象（PC 或移动端格式）标准化为统一输出格式。"""
    if not mblog_raw or not isinstance(mblog_raw, dict):
        return None
    if not mblog_raw.get("id"):
        return None
    import re as _re, html as _html
    text = mblog_raw.get("text", "")
    text = _re.sub(r'<[^>]+>', '', text).strip()
    # 解码 HTML 实体（&amp; -> &, &lt; -> < 等）
    text = _html.unescape(text)
    # 去掉微博文本尾部的「来自 XXX」和「转发了」等重复内容
    # 这些内容已经单独存在于 source / retweeted_status 字段中
    if text:
        # 先去掉尾部的时间戳行（如 "Thu Sep 17 21:13:15 +0800 2026"）
        text = _re.sub(r'\n[A-Z][a-z]{2}\s[A-Z][a-z]{2}\s\d{1,2}\s\d{2}:\d{2}:\d{2}\s[+-]\d{4}\s\d{4}\s*$', '', text)
        # 去掉尾部的「来自 XXX」行（source 字段已包含此信息）
        text = _re.sub(r'(?:^|\n)来自\s+\S+\s*$', '', text)
        # 去掉尾部的「转发了」等无意义结尾
        text = _re.sub(r'(?:^|\n)(转发了|轉發了|Repost)\s*$', '', text)
        text = text.strip()
    user = mblog_raw.get("user") or {}
    # pics: pic_ids (list of str) or pic_infos (dict)
    pics = []
    pic_infos = mblog_raw.get("pic_infos") or {}
    if pic_infos:
        for pic_info in pic_infos.values():
            if isinstance(pic_info, dict):
                # original can be a string URL or a dict with url key
                original = pic_info.get("original")
                if isinstance(original, str) and original:
                    pics.append(original)
                elif isinstance(original, dict) and original.get("url"):
                    pics.append(original["url"])
                else:
                    large = pic_info.get("large")
                    if isinstance(large, str) and large:
                        pics.append(large)
                    elif isinstance(large, dict) and large.get("url"):
                        pics.append(large["url"])
    elif mblog_raw.get("pic_ids"):
        # pic_ids are photo IDs, convert to URLs
        for pic_id in mblog_raw["pic_ids"]:
            pics.append(f"https://wx2.sinaimg.cn/orj1080/{pic_id}.jpg")
    return {
        "mid": mblog_raw.get("idstr") or str(mblog_raw.get("id", "")),
        "text": text,
        "created_at": mblog_raw.get("created_at", ""),
        "source": mblog_raw.get("source", ""),
        "reposts_count": mblog_raw.get("reposts_count", 0),
        "comments_count": mblog_raw.get("comments_count", 0),
        "attitudes_count": mblog_raw.get("attitudes_count", 0),
        "pics": pics,
        "user": {
            "id": user.get("idstr") or str(user.get("id", "")),
            "screen_name": user.get("screen_name", ""),
            "profile_image_url": user.get("profile_image_url", "").replace("http://", "https://"),
        },
    }


def _extract_posts_from_payload(payload):
    """从 API 响应中提取帖子列表，兼容 PC chaohua/page 和移动端 container/getIndex。"""
    if not isinstance(payload, dict):
        return []
    results = []

    # 格式 1: PC chaohua/page - items[].data 直接是 mblog
    items = payload.get("items") or []
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("category") == "feed":
            mblog = item.get("data")
            normalized = _normalize_mblog(mblog)
            if normalized:
                results.append(normalized)
    if results:
        return results

    # 格式 2: 移动端 container/getIndex - data.cards[].card_group[].mblog
    data = payload.get("data") or {}
    cards = data.get("cards") or []
    for card in cards:
        card_group = card.get("card_group") or []
        for item in card_group:
            mblog = item.get("mblog")
            normalized = _normalize_mblog(mblog)
            if normalized:
                results.append(normalized)

    # 格式 3: data.cards 直接是 mblog 列表（无 card_group 包装）
    if not results:
        for card in cards:
            mblog = card.get("mblog") or card
            normalized = _normalize_mblog(mblog)
            if normalized:
                results.append(normalized)

    # 格式 4: data.list[] / data.items[] 直接是 mblog 列表
    if not results:
        direct_list = data.get("list") or data.get("items") or []
        for entry in direct_list:
            mblog = entry.get("mblog") or entry
            normalized = _normalize_mblog(mblog)
            if normalized:
                results.append(normalized)

    return results


def _format_weibo_time(raw_time: str) -> str:
    """将微博时间格式化为可读字符串。
    处理：
    - 相对时间（"10分钟前"、"今天 12:30"）→ 转为 YYYY-MM-DD HH:MM
    - 已经是标准格式的 → 原样返回
    - 无法解析 → 返回原始值
    """
    if not raw_time:
        return ""
    from datetime import datetime as _dt, timedelta as _td
    now = _dt.now()

    try:
        # 标准格式
        if len(raw_time) >= 10 and raw_time[4] == '-':
            return raw_time[:16]
        # "今天 HH:MM"
        if raw_time.startswith("今天"):
            t = raw_time.replace("今天", "").strip()
            return f"{now.strftime('%Y-%m-%d')} {t}"
        # "昨天 HH:MM"
        if raw_time.startswith("昨天"):
            t = raw_time.replace("昨天", "").strip()
            yest = now - _td(days=1)
            return f"{yest.strftime('%Y-%m-%d')} {t}"
        # "N分钟前"
        if "分钟前" in raw_time:
            n = int(raw_time.replace("分钟前", "").strip())
            out = now - _td(minutes=n)
            return out.strftime("%Y-%m-%d %H:%M")
        # "N小时前"
        if "小时前" in raw_time:
            n = int(raw_time.replace("小时前", "").strip())
            out = now - _td(hours=n)
            return out.strftime("%Y-%m-%d %H:%M")
        # "N天前"
        if "天前" in raw_time:
            n = int(raw_time.replace("天前", "").strip())
            out = now - _td(days=n)
            return out.strftime("%Y-%m-%d %H:%M")
        # "MM-DD HH:MM"（无年份，补当前年）
        if len(raw_time) >= 11 and raw_time[2] == '-':
            return f"{now.year}-{raw_time}"
        # "YYYY-MM-DD" 纯日期
        if len(raw_time) == 10 and raw_time[4] == '-':
            return f"{raw_time} 00:00"
    except Exception:
        pass
    return raw_time


def fetch_topic_posts(session, cookies, containerid: str, channel="auto",
                      proxy=None, force=False, allow_fallback=True, count: int = 20):
    """拉取指定超话的最新帖子列表（默认前 count 条）。

    微博超话帖子接口是公开的，不需要登录态。
    优先尝试 PC 端 chaohua/page，失败则回退到移动端 container/getIndex。
    """
    posts = []
    errors = []
    tried = 0

    # 清理 containerid，去掉可能的后缀
    clean_cid = containerid.split("_-_")[0] if "_-_" in containerid else containerid
    # 去掉可能已存在的 100808 前缀
    if clean_cid.startswith("100808"):
        clean_cid = clean_cid[6:]

    # 端点列表（按优先级排序，确保获取最新帖子而非精华帖）
    endpoints = [
        # PC 端（chaohua/page）- 按时间排序（最可靠）
        {
            "url": f"{BASE_PC}/ajax_proxy/chaohua/page",
            "params": {"flowId": f"100808{clean_cid}_-_sort_time", "vtype": 1},
            "headers": {"Referer": f"{BASE_PC}/p/100808{clean_cid}"},
        },
        # PC 端（chaohua/page）- 默认排序
        {
            "url": f"{BASE_PC}/ajax_proxy/chaohua/page",
            "params": {"flowId": f"100808{clean_cid}", "vtype": 1},
            "headers": {"Referer": f"{BASE_PC}/p/100808{clean_cid}"},
        },
        # 移动端（container/getIndex）- 带 100808 前缀
        {
            "url": f"{BASE}/api/container/getIndex",
            "params": {"containerid": f"100808{clean_cid}", "page": 1, "count": 25, "vtype": 1},
            "headers": {"Referer": f"{BASE}/p/100808{clean_cid}"},
        },
        # 移动端（container/getIndex）- 无前缀
        {
            "url": f"{BASE}/api/container/getIndex",
            "params": {"containerid": clean_cid, "page": 1, "count": 25, "vtype": 1},
            "headers": {"Referer": f"{BASE}/p/{clean_cid}"},
        },
        # 移动端 - _all 全部（回退方案）
        {
            "url": f"{BASE}/api/container/getIndex",
            "params": {"containerid": f"100808{clean_cid}_all", "page": 1, "count": 25, "vtype": 1},
            "headers": {"Referer": f"{BASE}/p/100808{clean_cid}"},
        },
    ]

    for ep in endpoints:
        page = 1
        since_id = ""
        tried += 1
        while len(posts) < count:
            params = dict(ep["params"])
            if page > 1:
                params["page"] = page
            if since_id:
                params["since_id"] = since_id
            try:
                # 合并自定义 headers
                old_headers = dict(session.headers)
                session.headers.update(ep.get("headers", {}))
                payload = request_json(
                    session, "GET", ep["url"], params=params, cookies=cookies,
                    channel=channel, proxy=proxy, force=force,
                    allow_fallback=allow_fallback,
                )
                session.headers = old_headers
            except NetworkError as exc:
                errors.append(f"[{tried}] {ep['url']} p{page}: 网络错误 {exc}")
                break
            except RuntimeError as exc:
                errors.append(f"[{tried}] {ep['url']} p{page}: 请求失败 {exc}")
                break
            except Exception as exc:
                errors.append(f"[{tried}] {ep['url']} p{page}: 未知错误 {exc}")
                break

            # 检查响应状态（PC 端没有 ok 字段，直接有 items）
            if "items" in payload:
                # PC 端格式
                pass
            else:
                # 移动端格式，检查 ok
                ok_val = payload.get("ok")
                if ok_val == -100:
                    errors.append(f"[{tried}] {ep['url']} p{page}: Cookie 过期")
                    break
                if ok_val != 1 and ok_val != "1":
                    log.warning(f"fetch_topic_posts: ok={ok_val!r}, url={ep['url']}, page={page}")
                    break

            extracted = _extract_posts_from_payload(payload)
            if not extracted:
                errors.append(f"[{tried}] {ep['url']} p{page}: 无帖子数据")
                break

            posts.extend(extracted)
            if len(posts) >= count:
                break

            # 翻页
            data = payload.get("data") or {}
            cardlist_info = data.get("cardlistInfo") or {}
            next_since = cardlist_info.get("since_id", "")
            if next_since:
                since_id = next_since
            else:
                page += 1
                if page > 3:
                    break
            time.sleep(0.3)

        if posts:
            break

    # Deduplicate by mid
    seen_mids = set()
    unique_posts = []
    for p in posts:
        mid = p.get("mid", "")
        if mid and mid in seen_mids:
            continue
        seen_mids.add(mid)
        unique_posts.append(p)

    # 格式化时间
    for p in unique_posts[:count]:
        p["created_at"] = _format_weibo_time(p.get("created_at", ""))

    error_msg = "; ".join(errors) if errors else ""
    return {"posts": unique_posts[:count], "error": error_msg, "tried": tried}


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
    """获取关注超话列表，返回 [{name,id,scheme,done,avatar,description,member_count}]。"""
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
                # 提取头像、描述、成员数
                avatar = item.get("pic", "") or item.get("portrait", "") or ""
                if avatar and avatar.startswith("//"):
                    avatar = "https:" + avatar
                desc = item.get("desc", "") or item.get("desc1", "") or ""
                member = item.get("member_count", 0) or 0
                if name:
                    topics.append({
                        "name": name,
                        "id": topic_id,
                        "scheme": None if done else button_scheme,
                        "done": done,
                        "avatar": avatar,
                        "description": desc,
                        "member_count": member,
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
    """合并响应中的 Set-Cookie，返回 (新dict, 变化的key列表)。"""
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
    """对单个账号执行一遍签到。"""
    cookie_dict = normalize_cookie(cookie_dict)
    if not cookie_dict:
        return _bundle("failed", "Cookie 为空，请重新登录", 0, 0, 0, [],
                       cookie_dict, [], failure_type="cookie_invalid")

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
