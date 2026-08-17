"""代理节点管理 API。"""
from __future__ import annotations

import time

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .. import auth, database, proxy_geo

router = APIRouter(prefix="/api/proxies", tags=["proxies"])


class ProxyIn(BaseModel):
    label: str = ""
    ip: str = ""
    port: int = 0
    username: str = ""
    password: str = ""
    url: str = ""        # 可选，完整 socks5:// 链接
    enabled: bool = True
    remark: str = ""


class ProxyUpdate(BaseModel):
    label: str | None = None
    ip: str | None = None
    port: int | None = None
    username: str | None = None
    password: str | None = None
    url: str | None = None
    enabled: bool | None = None
    remark: str | None = None


def _parse_link(url: str) -> dict:
    """解析 socks5:// 链接为字段。返回 {ip,port,username,password}。"""
    from urllib.parse import urlparse
    if not url:
        return {"ip": "", "port": 0, "username": "", "password": ""}
    try:
        p = urlparse(url)
        host = p.hostname or ""
        port = p.port or 0
        user, pwd = "", ""
        if p.username:
            user = p.username
        if p.password:
            pwd = p.password
        return {"ip": host, "port": port, "username": user, "password": pwd}
    except Exception:
        return {"ip": "", "port": 0, "username": "", "password": ""}


def _public(p: dict) -> dict:
    """对外输出：绝不返回含认证信息的完整代理链接（v7.1）。

    历史版本把 `url` 原样返回，浏览器 DevTools / 任意 API 调用方都能拿到
    socks5://user:pass@host:port 的明文密码。现在只返回打码链接，
    真实链接仅在服务端（调度器直接读数据库）使用。
    """
    p = dict(p)
    raw = p.get("url", "") or ""
    if not raw and p.get("ip"):
        raw = database.build_proxy_url(p)
    p["password"] = "***" if p.get("password") else ""
    p["has_auth"] = bool(p.get("username") or p.get("password"))
    p["url"] = database.mask_proxy_url(raw)
    return p


@router.get("")
def list_proxies(user: dict = Depends(auth.require_admin)):
    return [_public(p) for p in database.get_proxies(include_disabled=True)]


@router.post("")
def create_proxy(data: ProxyIn, user: dict = Depends(auth.require_admin)):
    payload = data.model_dump()
    # 若有链接则从链接解析字段，并识别归属地
    if payload.get("url"):
        parsed = _parse_link(payload["url"])
        for k, v in parsed.items():
            if not payload.get(k):
                payload[k] = v
    if payload.get("ip"):
        url = database.build_proxy_url(payload) if not payload.get("url") else payload["url"]
        payload["url"] = url
        geo = proxy_geo.detect(url)
        if geo.get("ok"):
            payload["geo_country"] = geo.get("country", "")
            payload["geo_region"] = geo.get("region", "")
            payload["geo_country_code"] = geo.get("country_code", "")
            payload["geo_ip"] = geo.get("ip", "")
    else:
        raise HTTPException(status_code=400, detail="缺少代理 IP/链接")
    pid = database.add_proxy(payload)
    return _public(database.get_proxy(pid))


@router.put("/{proxy_id}")
def update_one(proxy_id: int, data: ProxyUpdate, user: dict = Depends(auth.require_admin)):
    existing = database.get_proxy(proxy_id)
    if not existing:
        raise HTTPException(404, "代理不存在")
    payload = {k: v for k, v in data.model_dump().items() if v is not None}
    # 前端不会回填密码，打码占位符一律忽略，避免把 "***" 当真密码存进库。
    if payload.get("password") in ("***", "•••"):
        payload.pop("password", None)
    # 若更新了 url，重新解析；但链接里没带认证时不要把已有账密洗成空（v7.1 修复）。
    if "url" in payload and payload["url"]:
        parsed = _parse_link(payload["url"])
        for k, v in parsed.items():
            if k in ("username", "password") and not v:
                continue   # 链接未带认证 → 保留原有凭据
            payload.setdefault(k, v)
    if payload.get("ip"):
        merged = dict(existing)
        merged.update(payload)
        url = database.build_proxy_url(merged) or payload.get("url", existing.get("url", ""))
        payload["url"] = url
        geo = proxy_geo.detect(url)
        if geo.get("ok"):
            payload["geo_country"] = geo.get("country", "")
            payload["geo_region"] = geo.get("region", "")
            payload["geo_country_code"] = geo.get("country_code", "")
            payload["geo_ip"] = geo.get("ip", "")
    database.update_proxy(proxy_id, payload)
    return _public(database.get_proxy(proxy_id))


@router.delete("/{proxy_id}")
def delete_one(proxy_id: int, user: dict = Depends(auth.require_admin)):
    if not database.delete_proxy(proxy_id):
        raise HTTPException(404, "代理不存在")
    return {"ok": True}


@router.post("/{proxy_id}/test")
def test_proxy(proxy_id: int, user: dict = Depends(auth.require_admin)):
    p = database.get_proxy(proxy_id)
    if not p:
        raise HTTPException(404, "代理不存在")
    url = p.get("url") or database.build_proxy_url(p)
    # 通过代理测试访问归属地服务，记录端到端延迟并持久化，供面板刷新后继续展示。
    started = time.perf_counter()
    info = proxy_geo.detect(url, use_cache=False) if url else {"ok": False, "message": "无代理链接"}
    elapsed_ms = int(info.get("latency_ms") or round((time.perf_counter() - started) * 1000))
    ok = info.get("ok", False)
    message = (
        f"✅ {info.get('country','')} {info.get('region','')} · {info.get('ip','')} · {elapsed_ms} ms"
        if ok else f"❌ {info.get('message','测试失败')} · {elapsed_ms} ms"
    )
    database.update_proxy(proxy_id, {
        "last_test": "ok" if ok else "fail",
        "last_latency_ms": elapsed_ms,
        "last_test_at": database._now(),
        "last_test_message": message,
    })
    return {"ok": ok, "message": message, "latency_ms": elapsed_ms, "geo": info}


@router.post("/detect")
def detect_url(data: dict, user: dict = Depends(auth.require_admin)):
    """根据链接或字段识别归属地（不保存）。data: {url} 或 {ip,port,username,password}"""
    url = data.get("url", "")
    if not url and data.get("ip"):
        url = database.build_proxy_url({**data, "username": data.get("user") or data.get("username",""),
                                        "password": data.get("pwd") or data.get("password","")})
    if not url:
        raise HTTPException(400, "缺少链接或 IP")
    info = proxy_geo.detect(url)
    return info
