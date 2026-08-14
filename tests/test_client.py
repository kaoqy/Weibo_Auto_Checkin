"""测试微博客户端核心逻辑（Cookie 解析、防封、代理解析）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.weibo_client import (  # noqa: E402
    CheckinOptions,
    cookie_to_string,
    merge_refreshed_cookies,
    normalize_cookie,
    parse_proxies,
    proxy_display_name,
)
from app.anti_ban import AntiBanPolicy, node_rotation  # noqa: E402


# ---------- Cookie 解析 ----------

def test_normalize_dict():
    assert normalize_cookie({"SUB": "a", "SCF": "b"}) == {"SUB": "a", "SCF": "b"}


def test_normalize_string():
    raw = "SUB=a; SUBP=b; SCF=c"
    assert normalize_cookie(raw) == {"SUB": "a", "SUBP": "b", "SCF": "c"}


def test_normalize_json_string():
    raw = '{"SUB": "a", "SCF": "b"}'
    assert normalize_cookie(raw) == {"SUB": "a", "SCF": "b"}


def test_normalize_empty():
    assert normalize_cookie("") == {}
    assert normalize_cookie(None) == {}


def test_cookie_to_string_roundtrip():
    d = {"SUB": "a", "SCF": "b"}
    s = cookie_to_string(d)
    assert normalize_cookie(s) == d


# ---------- 代理解析 ----------

def test_parse_proxies_single():
    assert parse_proxies("socks5://user:pass@1.1.1.1:8388") == \
        ["socks5://user:pass@1.1.1.1:8388"]


def test_parse_proxies_multi():
    raw = "socks5://a@1.1.1.1:1, socks5://b@2.2.2.2:2\nsocks5://c@3.3.3.3:3"
    out = parse_proxies(raw)
    assert len(out) == 3


def test_parse_proxies_ignore_http():
    assert parse_proxies("http://x:1") == []
    assert parse_proxies("") == []


def test_proxy_display_hides_password():
    name = proxy_display_name("socks5://user:secret@1.1.1.1:8388")
    assert "secret" not in name
    assert name == "1.1.1.1:8388"


# ---------- 防封 ----------

def test_anti_ban_window():
    # 构造一个永远 in_window 的策略（window_hour=99）
    p = AntiBanPolicy(enabled=True, wait_min=1, wait_max=3, window_hour=99)
    assert p.should_wait() is True
    p2 = AntiBanPolicy(enabled=False, wait_min=1, wait_max=3, window_hour=99)
    assert p2.should_wait() is False


def test_node_rotation():
    assert node_rotation(3, 1) == 0
    assert node_rotation(3, 2) == 1
    assert node_rotation(3, 4) == 0
    assert node_rotation(0, 5) == 0


# ---------- Cookie 合并 ----------

def test_merge_refreshed_cookies():
    class FakeCookie:
        def __init__(self, name, value):
            self.name = name
            self.value = value

    class FakeSession:
        cookies = [FakeCookie("SUB", "newval"), FakeCookie("SCF", "same")]

    old = {"SUB": "old", "SCF": "same"}
    merged, changed = merge_refreshed_cookies(FakeSession(), old)
    assert merged["SUB"] == "newval"
    assert merged["SCF"] == "same"
    assert changed == ["SUB"]


# ---------- CheckinOptions ----------

def test_checkin_options_from_settings():
    class FakeDB:
        def __call__(self, key, default=""):
            m = {
                "checkin_delay_min": "2", "checkin_delay_max": "5",
                "proxies": "socks5://a@1:1", "proxy_force": "0",
                "proxy_fallback": "1",
            }
            return m.get(key, default)

    opts = CheckinOptions.from_settings(FakeDB())
    assert opts.checkin_delay_min == 2
    assert opts.checkin_delay_max == 5
    assert len(opts.proxies) == 1
    assert opts.proxy_force is False
