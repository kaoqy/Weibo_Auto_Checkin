"""
SOCKS5 代理归属地识别。
通过每个 socks 代理发起 IP 定位请求，自动识别归属地（国家/地区）。

v7.2 修订（实测校准，非推测）：
- **HTTPS 接口优先**。实测部分 SOCKS 出口会劫持明文 HTTP：请求
  `http://ip-api.com/json/` 会拿到一个 HTML 门户页而不是 JSON，于是识别结果
  忽成功忽失败。HTTPS 不会被中间层改写，所以放前面，明文 HTTP 只作兜底。
- **统一中文地名**。以前多服务混用会出现同一节点两种写法（ip-api 给「江苏」、
  ipwho 给「Jiangsu Sheng」、ipinfo 只给「CN」+「Jiangsu」）。现在统一映射成
  中文并去掉 Sheng/Province 之类后缀，展示保持一致。
- 成功结果缓存 1 小时；失败只缓存 30 秒，避免一次网络抖动把错误锁死一小时。
- 错误信息翻译成人话，且不回显代理链接（防止泄漏凭据）。

候选接口经实测筛选：ipwho.is 与 ipinfo.io 免鉴权且稳定；
ip-api.io 需要 API key（401）、ipapi.co 被 Cloudflare 拦（403），故不采用。
"""
from __future__ import annotations

import json
import logging
import threading
import time

import requests

log = logging.getLogger("weibo.proxygeo")


# ========================= 地名规整 =========================

# 英文 → 中文常见地区映射（只覆盖高频，未命中则原样展示）
_REGION_ZH = {
    "Jiangsu": "江苏", "Zhejiang": "浙江", "Guangdong": "广东", "Beijing": "北京",
    "Shanghai": "上海", "Tianjin": "天津", "Chongqing": "重庆", "Hebei": "河北",
    "Shanxi": "山西", "Liaoning": "辽宁", "Jilin": "吉林", "Heilongjiang": "黑龙江",
    "Anhui": "安徽", "Fujian": "福建", "Jiangxi": "江西", "Shandong": "山东",
    "Henan": "河南", "Hubei": "湖北", "Hunan": "湖南", "Guangxi": "广西",
    "Hainan": "海南", "Sichuan": "四川", "Guizhou": "贵州", "Yunnan": "云南",
    "Shaanxi": "陕西", "Gansu": "甘肃", "Qinghai": "青海", "Ningxia": "宁夏",
    "Xinjiang": "新疆", "Tibet": "西藏", "Xizang": "西藏",
    "Inner Mongolia": "内蒙古", "Nei Mongol": "内蒙古",
    "Hong Kong": "香港", "Kowloon": "九龙", "Macau": "澳门", "Macao": "澳门",
    "Taiwan": "台湾", "New Taipei": "新北", "Taipei": "台北",
    "Tokyo": "东京", "Osaka": "大阪", "Seoul": "首尔", "Singapore": "新加坡",
    "California": "加州", "Virginia": "弗吉尼亚", "New York": "纽约",
    "Washington": "华盛顿", "Texas": "德州", "Oregon": "俄勒冈",
    "England": "英格兰", "Frankfurt": "法兰克福", "Hesse": "黑森",
}

_COUNTRY_ZH = {
    "China": "中国", "Hong Kong": "中国香港", "Taiwan": "中国台湾",
    "Macao": "中国澳门", "Macau": "中国澳门",
    "Japan": "日本", "South Korea": "韩国", "Korea": "韩国",
    "Singapore": "新加坡", "United States": "美国", "USA": "美国",
    "United Kingdom": "英国", "Germany": "德国", "France": "法国",
    "Netherlands": "荷兰", "Canada": "加拿大", "Australia": "澳大利亚",
    "Russia": "俄罗斯", "India": "印度", "Vietnam": "越南",
    "Thailand": "泰国", "Malaysia": "马来西亚", "Indonesia": "印度尼西亚",
    "Philippines": "菲律宾", "Turkey": "土耳其", "Brazil": "巴西",
}

# 国家代码 → 中文名（ipinfo 之类只返回 code）
_COUNTRY_BY_CODE = {
    "CN": "中国", "HK": "中国香港", "TW": "中国台湾", "MO": "中国澳门",
    "JP": "日本", "KR": "韩国", "SG": "新加坡", "US": "美国",
    "GB": "英国", "UK": "英国", "DE": "德国", "FR": "法国",
    "NL": "荷兰", "CA": "加拿大", "AU": "澳大利亚", "RU": "俄罗斯",
    "IN": "印度", "VN": "越南", "TH": "泰国", "MY": "马来西亚",
    "ID": "印度尼西亚", "PH": "菲律宾", "TR": "土耳其", "BR": "巴西",
}


def _clean_region(value: str) -> str:
    """规整地区名：去掉 Sheng/Province/省/市 等后缀，并映射常见中文名。"""
    s = (value or "").strip()
    if not s:
        return ""
    for suffix in (" Sheng", " Province", " Shi", " City", "省", "市"):
        if s.endswith(suffix):
            s = s[: -len(suffix)].strip()
    return _REGION_ZH.get(s, s)


def _zh_country(name: str, code: str = "") -> str:
    """统一国家名为中文；只有 code 时按 code 映射。"""
    s = (name or "").strip()
    if s:
        return _COUNTRY_ZH.get(s, s)
    cc = (code or "").strip().upper()
    return _COUNTRY_BY_CODE.get(cc, cc)


# ========================= 归属地服务 =========================

GEO_SERVICES = [
    {
        "name": "ipwho",
        "url": "https://ipwho.is/",
        "params": {},
        "parse": lambda d: {
            "ok": bool(d.get("success")),
            "ip": d.get("ip", ""),
            "country": _zh_country(d.get("country", ""), d.get("country_code", "")),
            "country_code": d.get("country_code", ""),
            "region": _clean_region(d.get("region", "")),
            "city": d.get("city", ""),
            "message": "success" if d.get("success") else d.get("message", "查询失败"),
        },
    },
    {
        "name": "ipinfo",
        "url": "https://ipinfo.io/json",
        "params": {},
        "parse": lambda d: {
            "ok": bool(d.get("ip")),
            "ip": d.get("ip", ""),
            "country": _zh_country("", d.get("country", "")),
            "country_code": d.get("country", ""),
            "region": _clean_region(d.get("region", "")),
            "city": d.get("city", ""),
            "message": "success" if d.get("ip") else "查询失败",
        },
    },
    {
        # 明文 HTTP 兜底：部分出口会劫持改写成 HTML，所以放最后
        "name": "ip-api-http",
        "url": "http://ip-api.com/json/",
        "params": {"fields": "status,query,country,countryCode,regionName,city",
                   "lang": "zh-CN"},
        "parse": lambda d: {
            "ok": d.get("status") == "success" or bool(d.get("country")),
            "ip": d.get("query", ""),
            "country": _zh_country(d.get("country", ""), d.get("countryCode", "")),
            "country_code": d.get("countryCode", ""),
            "region": _clean_region(d.get("regionName", "")),
            "city": d.get("city", ""),
            "message": ("success" if d.get("status") == "success" or d.get("country")
                        else d.get("message", "查询失败")),
        },
    },
]

# socks链接 -> 归属地信息缓存
_cache: dict[str, dict] = {}
_cache_lock = threading.Lock()
_CACHE_TTL = 3600        # 成功结果缓存 1 小时
_FAIL_CACHE_TTL = 30     # 失败只缓存 30s，避免一次网络抖动锁死一小时


def _via_proxy(socks_url: str) -> dict | None:
    """解析 socks url 为 requests proxies dict。"""
    if not socks_url:
        return None
    return {"http": socks_url, "https": socks_url} if socks_url.startswith(("socks5://", "socks5h://")) else None


def _safe_json(r: requests.Response) -> dict | None:
    """安全解析 JSON，避免 JSONDecodeError 直接抛给用户。"""
    try:
        data = r.json()
        if isinstance(data, dict):
            return data
    except (json.JSONDecodeError, ValueError):
        text = (r.text or "").strip()
        # 出口劫持明文 HTTP 时会返回 HTML 门户页，给一句人能看懂的话
        if text[:60].lower().lstrip().startswith(("<!doctype", "<html")):
            log.warning("归属地接口响应被改写为 HTML（疑似出口劫持明文 HTTP）status=%s",
                        r.status_code)
            return {"ok": False, "message": "接口响应被改写为网页（出口可能劫持了明文 HTTP）"}
        snippet = text[:80].replace("\n", " ") if text else "(空响应)"
        log.warning("非 JSON 响应 status=%s ctype=%s body=%r",
                    r.status_code, r.headers.get("content-type"), text[:200])
        return {"ok": False, "message": f"接口返回非 JSON（HTTP {r.status_code}，内容：{snippet}）"}
    except Exception as exc:  # noqa: BLE001
        log.warning("JSON 解析异常: %s", exc)
        return {"ok": False, "message": f"响应解析失败：{exc}"}
    return None


def _friendly_error(exc: Exception) -> str:
    """把底层异常翻译成用户能看懂的一句话。

    注意：不能把异常原文直接抛给前端 —— requests 的异常消息里会带完整
    代理 URL（含用户名密码），那样等于从错误提示里泄漏凭据。
    """
    low = str(exc).lower()
    if "0x01: general socks server failure" in low:
        return "代理拒绝连接（SOCKS 服务端返回失败，常见于用户名/密码错误）"
    if "0x02" in low or "not allowed" in low:
        return "代理拒绝授权（连接不被允许）"
    if "authentication" in low or "0x03" in low:
        return "代理认证失败（用户名或密码不正确）"
    if "name or service not known" in low or "nodename nor servname" in low:
        return "代理地址无法解析（域名或 IP 有误）"
    if "timed out" in low or "timeout" in low:
        return "连接超时（代理不可达或网络太慢）"
    if "connection refused" in low:
        return "连接被拒绝（端口未开放或代理已下线）"
    if "ssl" in low or "eof occurred" in low:
        return "TLS 握手失败（出口可能拦截了 HTTPS）"
    return "连接失败（代理不可用）"


def _query_service(svc: dict, proxies: dict) -> dict:
    """对单个归属地服务发起请求并解析结果，并记录端到端延迟。"""
    started = time.perf_counter()
    try:
        r = requests.get(
            svc["url"],
            params=svc["params"] or None,
            proxies=proxies, timeout=12,
        )
    except requests.exceptions.ProxyError as exc:
        # 代理层就失败：换接口也没用
        return {"ok": False, "message": _friendly_error(exc), "fatal": True}
    except requests.exceptions.ConnectTimeout:
        return {"ok": False, "message": "连接超时（代理不可达或网络太慢）"}
    except requests.exceptions.ConnectionError as exc:
        return {"ok": False, "message": _friendly_error(exc)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": _friendly_error(exc)}

    if r.status_code != 200:
        return {"ok": False, "message": f"接口返回 HTTP {r.status_code}"}
    data = _safe_json(r)
    if not data:
        return {"ok": False, "message": "接口无有效数据"}
    parsed = svc["parse"](data)
    parsed.setdefault("service", svc["name"])
    parsed["latency_ms"] = round((time.perf_counter() - started) * 1000)
    return parsed


def _detect(socks_url: str) -> dict:
    """通过 socks 代理查询归属地（依次尝试多个服务）。失败返回 ok=False。"""
    proxies = _via_proxy(socks_url)
    if not proxies:
        return {"ok": False, "message": "不是 socks5 链接"}

    errors = []
    for svc in GEO_SERVICES:
        r = _query_service(svc, proxies)
        if r.get("ok"):
            return r
        msg = r.get("message", "失败")
        errors.append(msg)
        # 代理本身连不上/认证失败，换接口也没意义，直接给结论
        if r.get("fatal"):
            return {"ok": False, "message": msg}
    uniq = list(dict.fromkeys(errors))
    return {"ok": False, "message": uniq[0] if uniq else "所有归属地接口均失败"}


def detect(socks_url: str, use_cache: bool = True, force: bool = False) -> dict:
    """检测 socks 归属地（带缓存）。force=True 跳过缓存强制刷新。"""
    key = (socks_url or "").strip()
    if not key:
        return {"ok": False, "message": "代理为空", "display": "无"}
    now = time.time()
    with _cache_lock:
        hit = _cache.get(key)
        if use_cache and not force and hit:
            ttl = _CACHE_TTL if hit.get("ok") else _FAIL_CACHE_TTL
            if now - hit.get("_t", 0) < ttl:
                return hit
    result = _detect(key)
    result["_t"] = now
    with _cache_lock:
        _cache[key] = result
    return result


def clear_cache() -> None:
    """清空归属地缓存（换了代理密码或出口后手动刷新用）。"""
    with _cache_lock:
        _cache.clear()


def display(socks_url: str) -> str:
    """返回简短显示名：如 🇭🇰 中国香港 或 “检测失败”。"""
    info = detect(socks_url)
    if not info.get("ok"):
        return "⚠️ " + str(info.get("message", "检测失败"))
    flag = _flag(info.get("country_code", ""))
    parts = [p for p in [info.get("country"), info.get("region")] if p]
    region = "·".join(parts)
    return f"{flag} {region}" if region else "未知"


def short_label(socks_url: str) -> str:
    """返回简短标签如 HK 或 ?。"""
    info = detect(socks_url)
    if info.get("ok"):
        cc = info.get("country_code", "")
        return cc if cc else "?"
    return "?"


def _flag(cc: str) -> str:
    """国家代码 → 国旗 emoji。"""
    if not cc or len(cc) != 2:
        return "🌐"
    return chr(0x1F1E6 + ord(cc[0].upper()) - ord("A")) + \
        chr(0x1F1E6 + ord(cc[1].upper()) - ord("A"))


def detect_all(urls: list[str]) -> list[dict]:
    """检测多个 socks 的归属地。"""
    return [detect(u) for u in urls]
