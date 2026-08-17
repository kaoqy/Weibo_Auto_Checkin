"""
SOCKS5 代理归属地识别。
通过每个 socks 代理发起 IP 定位请求，自动识别归属地（国家/地区）。
- 首选 http://ip-api.com（JSON，免费，无 HTTPS 限制）
- 失败时兜底 https://ipwho.is（HTTPS，返回 JSON）
结果缓存，避免频繁请求。
"""
from __future__ import annotations

import json
import logging
import threading
import time

import requests

log = logging.getLogger("weibo.proxygeo")

GEO_SERVICES = [
    {
        "name": "ip-api",
        "url": "http://ip-api.com/json/",
        "params": {"fields": "status,query,country,countryCode,regionName,city", "lang": "zh-CN"},
        "parse": lambda d: {
            "ok": d.get("status") == "success" or bool(d.get("country")),
            "ip": d.get("query", ""),
            "country": d.get("country", ""),
            "country_code": d.get("countryCode", ""),
            "region": d.get("regionName", ""),
            "city": d.get("city", ""),
            "message": "success" if d.get("status") == "success" or d.get("country") else d.get("message", "查询失败"),
        },
    },
    {
        "name": "ipwho",
        "url": "https://ipwho.is/",
        "params": {},
        "parse": lambda d: {
            "ok": bool(d.get("success")),
            "ip": d.get("ip", ""),
            "country": d.get("country", ""),
            "country_code": d.get("country_code", ""),
            "region": (d.get("region") or "").split(" ")[0],
            "city": d.get("city", ""),
            "message": "success" if d.get("success") else d.get("message", "查询失败"),
        },
    },
]

# socks链接 -> 归属地信息缓存
_cache: dict[str, dict] = {}
_cache_lock = threading.Lock()
_CACHE_TTL = 3600  # 1 小时


def _via_proxy(socks_url: str) -> dict | None:
    """解析 socks url 为 requests proxies dict。"""
    if not socks_url:
        return None
    return {"http": socks_url, "https": socks_url} if socks_url.startswith(("socks5://", "socks5h://")) else None


def _safe_json(r: requests.Response) -> dict | None:
    """安全解析 JSON，避免 'Expecting value' 等 JSONDecodeError 泄漏给用户。"""
    try:
        data = r.json()
        if isinstance(data, dict):
            return data
    except (json.JSONDecodeError, ValueError):
        # 响应头非 JSON / 空 / HTML：尽量截一段文本方便排查
        text = (r.text or "").strip()
        snippet = text[:80].replace("\n", " ") if text else "(空响应)"
        log.warning("非 JSON 响应 status=%s ctype=%s body=%r", r.status_code, r.headers.get("content-type"), text[:200])
        return {"ok": False, "message": f"接口返回非 JSON（HTTP {r.status_code}，内容：{snippet}）"}
    except Exception as exc:  # noqa: BLE001
        log.warning("JSON 解析异常: %s", exc)
        return {"ok": False, "message": f"响应解析失败：{exc}"}
    return None


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
        return {"ok": False, "message": f"代理连接失败（{str(exc)[:80]}）"}
    except requests.exceptions.ConnectTimeout:
        return {"ok": False, "message": "连接超时"}
    except requests.exceptions.ConnectionError as exc:
        return {"ok": False, "message": f"连接失败（{str(exc)[:80]}）"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": f"请求异常（{str(exc)[:80]}）"}

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
        errors.append(f"{svc['name']}: {r.get('message', '失败')}")
    # 全部失败：把首个可读错误作为主信息，附加其余供排查
    return {"ok": False, "message": errors[0] if errors else "所有归属地接口均失败"}


def detect(socks_url: str, use_cache: bool = True, force: bool = False) -> dict:
    """检测 socks 归属地（带缓存）。force=True 跳过缓存强制刷新。"""
    key = socks_url.strip()
    if not key:
        return {"ok": False, "message": "代理为空", "display": "无"}
    now = time.time()
    with _cache_lock:
        hit = _cache.get(key)
        if use_cache and not force and hit and now - hit.get("_t", 0) < _CACHE_TTL:
            return hit
    result = _detect(key)
    result["_t"] = now
    with _cache_lock:
        _cache[key] = result
    return result


def display(socks_url: str) -> str:
    """返回简短显示名：如 🇭🇰 HK·Hong Kong 或 “检测失败”。"""
    info = detect(socks_url)
    if not info.get("ok"):
        return "⚠️ " + str(info.get("message", "检测失败"))
    flag = _flag(info.get("country_code", ""))
    parts = [p for p in [info.get("country"), info.get("region")] if p]
    region = "·".join(parts)
    return f"{flag} {region}" if region else "未知"


def short_label(socks_url: str) -> str:
    """返回简短标签如 HK 或 proxy1。"""
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
