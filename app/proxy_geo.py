"""
SOCKS5 代理归属地识别。
通过每个 socks 代理发起 IP 定位请求（ip-api.com），自动识别归属地（国家/地区）。
结果缓存，避免频繁请求。
"""
from __future__ import annotations

import json
import logging
import threading
import time

import requests

log = logging.getLogger("weibo.proxygeo")

GEO_API = "http://ip-api.com/json/"
GEO_FIELDS = "query,country,countryCode,regionName,city"

# socks链接 -> 归属地信息缓存
_cache: dict[str, dict] = {}
_cache_lock = threading.Lock()
_CACHE_TTL = 3600  # 1 小时

# 时区偏移表用于简化显示（可选）


def _via_proxy(socks_url: str) -> str | None:
    """解析 socks url 为 requests proxies dict。"""
    if not socks_url:
        return None
    return {"http": socks_url, "https": socks_url} if socks_url.startswith(("socks5://", "socks5h://")) else None


def _detect(socks_url: str) -> dict:
    """通过 socks 代理查询归属地。失败返回 None。"""
    proxies = _via_proxy(socks_url)
    if not proxies:
        return {"ok": False, "message": "不是 socks5 链接"}
    try:
        r = requests.get(
            GEO_API,
            params={"fields": GEO_FIELDS, "lang": "zh-CN"},
            proxies=proxies, timeout=12,
        )
        data = r.json()
        if data.get("status") != "success":
            return {"ok": False, "message": data.get("message", "查询失败")}
        return {
            "ok": True,
            "ip": data.get("query", ""),
            "country": data.get("country", ""),
            "country_code": data.get("countryCode", ""),
            "region": data.get("regionName", ""),
            "city": data.get("city", ""),
        }
    except Exception as exc:
        return {"ok": False, "message": str(exc)[:100]}


def detect(socks_url: str, use_cache: bool = True) -> dict:
    """检测 socks 归属地（带缓存）。"""
    key = socks_url.strip()
    if not key:
        return {"ok": False, "message": "代理为空", "display": "无"}
    now = time.time()
    with _cache_lock:
        hit = _cache.get(key)
        if use_cache and hit and now - hit.get("_t", 0) < _CACHE_TTL:
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
