"""归属地识别与扫码登录模块的单元测试（mock 网络/浏览器，避免真实依赖）。"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app.proxy_geo as geo  # noqa: E402
import app.weibo_login as wl  # noqa: E402


# ---------------- proxy_geo 归属地容错 ----------------

def test_detect_rejects_non_socks():
    r = geo._detect("http://1.2.3.4:80")
    assert r["ok"] is False
    assert "socks" in r["message"]


def test_safe_json_handles_non_json():
    class _R:
        status_code = 200
        headers = {"content-type": "text/html"}
        text = "<html>oops"
        def json(self):
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
    out = geo._safe_json(_R())
    assert out["ok"] is False
    assert "非 JSON" in out["message"]
    # 关键：不应抛出 'Expecting value' 原文
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


def test_query_service_parses_ipapi_success(monkeypatch):
    class _R:
        status_code = 200
        headers = {"content-type": "application/json"}
        text = '{"status":"success","query":"1.2.3.4","country":"香港","countryCode":"HK","regionName":"Kowloon","city":"香港"}'
        def json(self):
            import json
            return json.loads(self.text)
    monkeypatch.setattr(geo.requests, "get", lambda *a, **k: _R())
    svc = geo.GEO_SERVICES[0]
    out = geo._query_service(svc, {})
    assert out["ok"] is True
    assert out["country_code"] == "HK"


def test_query_service_fallback_to_ipwho(monkeypatch):
    """ip-api 失败时应继续尝试 ipwho.is。"""
    calls = {"n": 0}

    class _R:
        status_code = 200
        headers = {"content-type": "application/json"}
        text = '{"success":true,"ip":"9.9.9.9","country":"United States","country_code":"US","region":"California","city":"LA"}'
        def json(self):
            import json
            return json.loads(self.text)

    def fake_get(url, *a, **k):
        if "ip-api" in url:
            calls["n"] += 1
            raise geo.requests.exceptions.ConnectionError("boom on ip-api")
        return _R()

    monkeypatch.setattr(geo.requests, "get", fake_get)
    out = geo._detect("socks5://u:p@1.2.3.4:1080")
    assert out["ok"] is True
    assert out["country_code"] == "US"
    assert calls["n"] == 1


def test_detect_uses_cache(monkeypatch):
    monkeypatch.setattr(geo, "_detect", lambda u: {"ok": True, "ip": "cached", "country": "X", "country_code": "XX", "region": "", "city": ""})
    url = "socks5://u:p@1.2.3.4:1080"
    first = geo.detect(url, use_cache=True)
    assert first["ip"] == "cached"
    # 第二次命中缓存，_detect 不应再次调用
    second = geo.detect(url, use_cache=True)
    assert second["ip"] == "cached"


# ---------------- weibo_login 扫码 finalize ----------------

import asyncio


def run_async(coro):
    """在独立事件循环里跑 async coroutine（测试用）。"""
    return asyncio.run(coro)


@pytest.fixture()
def qr_session():
    wl.QR_SESSIONS.clear()
    return wl.QR_SESSIONS


def test_finalize_requires_scan(qr_session):
    qrid = "q1"
    qr_session[qrid] = {"status": "pending", "created_at": __import__("time").time(), "cookies": {}, "uid": "", "username": ""}
    r = run_async(wl.finalize_login(qrid))
    assert r["ok"] is False
    assert "扫码" in r["message"]


def test_finalize_completes_when_sub_present(qr_session):
    qrid = "q2"
    qr_session[qrid] = {
        "status": "success", "created_at": __import__("time").time(),
        "cookies": {"SUB": "abc", "SUBP": "x"}, "uid": "123", "username": "昵称",
    }
    r = run_async(wl.finalize_login(qrid))
    assert r["ok"] is True
    assert r["cookies"]["SUB"] == "abc"
    assert r["uid"] == "123"


def test_finalize_tries_crossdomain_when_missing_sub(qr_session, monkeypatch):
    qrid = "q3"
    qr_session[qrid] = {
        "status": "scanned", "created_at": __import__("time").time(),
        "cookies": {}, "uid": "", "username": "",
    }

    class _PageFull:
        def __init__(self):
            self._cookies = [
                {"name": "SUB", "value": "from-crossdomain"},
                {"name": "SUBP", "value": "y"},
            ]
        async def goto(self, *a, **k):
            pass
        async def wait_for_timeout(self, *a, **k):
            pass
        async def cookies(self):
            return self._cookies

    page = _PageFull()

    async def fake_ensure():
        return page

    async def fake_collect(p):
        all_c = await p.cookies()
        return {c["name"]: c["value"] for c in all_c}

    monkeypatch.setattr(wl, "_ensure_browser", fake_ensure)
    monkeypatch.setattr(wl, "_collect_cookies", fake_collect)

    r = run_async(wl.finalize_login(qrid))
    assert r["ok"] is True
    assert r["cookies"].get("SUB") == "from-crossdomain"
    # 状态被修正为 success
    assert qr_session[qrid]["status"] == "success"


def test_check_qrcode_success_finalizes(monkeypatch):
    """check_qrcode 在扫码成功后应能补全 cookie 并返回 success。"""
    import time as _t
    qrid = "q5"
    wl.QR_SESSIONS.clear()
    wl.QR_SESSIONS[qrid] = {
        "status": "scanned", "created_at": _t.time(),
        "cookies": {}, "uid": "", "username": "", "rid": "RID",
    }

    class _PageEval:
        _cookies = [{"name": "SUB", "value": "sub-after-check"},
                    {"name": "SUBP", "value": "sp"}]
        async def evaluate(self, *a, **k):
            return {"code": 20000000, "msg": "ok", "data": {"uid": "888"}}
        async def goto(self, *a, **k):
            pass
        async def wait_for_timeout(self, *a, **k):
            pass
        async def cookies(self):
            return self._cookies

    page = _PageEval()

    async def fake_ensure():
        return page

    async def fake_collect(p):
        all_c = await p.cookies()
        return {c["name"]: c["value"] for c in all_c}

    monkeypatch.setattr(wl, "_ensure_browser", fake_ensure)
    monkeypatch.setattr(wl, "_collect_cookies", fake_collect)

    st = run_async(wl.check_qrcode(qrid))
    assert st["status"] == "success"
    assert st["uid"] == "888"


def test_expired_qr_finalize_rejected(qr_session):
    import time
    qrid = "q4"
    qr_session[qrid] = {
        "status": "pending", "created_at": time.time() - 9999,
        "cookies": {}, "uid": "", "username": "",
    }
    r = run_async(wl.finalize_login(qrid))
    assert r["ok"] is False
    assert "过期" in r["message"]
