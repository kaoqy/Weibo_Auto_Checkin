"""归属地识别与扫码登录模块的单元测试（mock 网络/浏览器，避免真实依赖）。"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app.proxy_geo as geo  # noqa: E402


# ---------------- proxy_geo 归属地容错 ----------------

def test_detect_rejects_non_socks():
    r = geo._detect("http://1.2.3.4:80")
    assert r["ok"] is False
    assert "socks" in r["message"]


def test_safe_json_handles_non_json():
    """HTML 响应（常见于出口劫持明文 HTTP）要给人话提示。"""
    class _R:
        status_code = 200
        headers = {"content-type": "text/html"}
        text = "<html>oops"
        def json(self):
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
    out = geo._safe_json(_R())
    assert out["ok"] is False
    assert "网页" in out["message"] or "非 JSON" in out["message"]
    # 关键：不应抛出 'Expecting value' 原文
    assert "Expecting value" not in out["message"]


def test_safe_json_non_json_plain_text():
    """非 HTML 的非 JSON 响应仍按“非 JSON”报。"""
    class _R:
        status_code = 200
        headers = {"content-type": "text/plain"}
        text = "Valid API key is required."
        def json(self):
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
    out = geo._safe_json(_R())
    assert out["ok"] is False
    assert "非 JSON" in out["message"]
    assert "Expecting value" not in out["message"]


def test_safe_json_handles_empty():
    class _R:
        status_code = 500
        headers = {"content-type": "application/json"}
        text = ""
        def json(self):
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
    out = geo._safe_json(_R())
    assert out["ok"] is False


def test_safe_json_parses_valid():
    class _R:
        status_code = 200
        headers = {"content-type": "application/json"}
        text = '{"status":"success","country":"HK"}'
        def json(self):
            import json as _j
            return _j.loads(self.text)
    out = geo._safe_json(_R())
    assert out == {"status": "success", "country": "HK"}


def test_query_service_parses_ipwho_success(monkeypatch):
    """首选服务（ipwho，HTTPS）能正确解析。"""
    class _R:
        status_code = 200
        headers = {"content-type": "application/json"}
        text = '{"success":true,"ip":"1.2.3.4","country":"Hong Kong","country_code":"HK","region":"Kowloon","city":"Hong Kong"}'
        def json(self):
            import json
            return json.loads(self.text)
    monkeypatch.setattr(geo.requests, "get", lambda *a, **k: _R())
    svc = geo.GEO_SERVICES[0]
    out = geo._query_service(svc, {})
    assert out["ok"] is True
    assert out["country_code"] == "HK"
    assert out["country"] == "中国香港"
    assert out["region"] == "九龙"


def test_query_service_parses_ipapi_http_success(monkeypatch):
    """兜底的明文 ip-api 解析仍然可用。"""
    class _R:
        status_code = 200
        headers = {"content-type": "application/json"}
        text = '{"status":"success","query":"1.2.3.4","country":"香港","countryCode":"HK","regionName":"Kowloon","city":"香港"}'
        def json(self):
            import json
            return json.loads(self.text)
    monkeypatch.setattr(geo.requests, "get", lambda *a, **k: _R())
    svc = [s for s in geo.GEO_SERVICES if s["name"] == "ip-api-http"][0]
    out = geo._query_service(svc, {})
    assert out["ok"] is True
    assert out["country_code"] == "HK"


def test_query_service_fallback_to_next_service(monkeypatch):
    """首选服务（ipwho）失败时应继续尝试下一个（ipinfo）。"""
    calls = {"n": 0}

    class _R:
        status_code = 200
        headers = {"content-type": "application/json"}
        text = '{"ip":"9.9.9.9","country":"US","region":"California","city":"LA"}'
        def json(self):
            import json
            return json.loads(self.text)

    def fake_get(url, *a, **k):
        if "ipwho" in url:
            calls["n"] += 1
            raise geo.requests.exceptions.ConnectionError("boom on ipwho")
        return _R()

    monkeypatch.setattr(geo.requests, "get", fake_get)
    geo.clear_cache()
    out = geo._detect("socks5://u:p@1.2.3.4:1080")
    assert out["ok"] is True
    assert out["country_code"] == "US"
    assert out["region"] == "加州"
    assert calls["n"] == 1
    geo.clear_cache()


def test_proxy_error_stops_trying_other_services(monkeypatch):
    """代理本身拒连时不必再轮其他接口，直接给结论。"""
    calls = {"n": 0}

    def fake_get(url, *a, **k):
        calls["n"] += 1
        raise geo.requests.exceptions.ProxyError(
            "Cannot connect to proxy. GeneralProxyError('Socket error: "
            "0x01: General SOCKS server failure')")

    monkeypatch.setattr(geo.requests, "get", fake_get)
    geo.clear_cache()
    out = geo._detect("socks5://u:p@1.2.3.4:1080")
    assert out["ok"] is False
    assert calls["n"] == 1          # 只试一次就停
    assert "密码" in out["message"] or "SOCKS" in out["message"]
    geo.clear_cache()


def test_detect_uses_cache(monkeypatch):
    monkeypatch.setattr(geo, "_detect", lambda u: {"ok": True, "ip": "cached", "country": "X", "country_code": "XX", "region": "", "city": ""})
    url = "socks5://u:p@1.2.3.4:1080"
    first = geo.detect(url, use_cache=True)
    assert first["ip"] == "cached"
    # 第二次命中缓存，_detect 不应再次调用
    second = geo.detect(url, use_cache=True)
    assert second["ip"] == "cached"
