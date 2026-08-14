"""
微博 m.weibo.cn 扫码登录模块。
通过 passport.weibo.com 的 SSO 二维码接口实现：
    1. 获取二维码（qrid + 图片 URL）
    2. 轮询扫码状态
    3. 确认成功后返回登录 Cookie
"""
from __future__ import annotations

import io
import json
import logging
import time
import uuid
from datetime import datetime
from urllib.parse import quote

import requests

log = logging.getLogger("weibo.qrlogin")

# 微博 passport 接口
PASSPORT = "https://passport.weibo.com"
QR_IMAGE_URL = PASSPORT + "/sso/v2/qrcode/image"
QR_CHECK_URL = PASSPORT + "/sso/v2/qrcode/check"
LOGIN_URL = "https://m.weibo.cn"

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# 返回码含义
RET = {
    20000000: ("success", "扫码确认成功"),
    50114001: ("pending", "未使用（等待扫码）"),
    50114002: ("scanned", "已扫码，等待确认"),
    50114003: ("expired", "二维码已过期"),
    50114004: ("cancelled", "用户取消授权"),
}

# 内存中的二维码会话（生产可换 Redis，此处够用）
# qrid -> {qr_image, created_at, status, cookies, uid, username}
QR_SESSIONS: dict[str, dict] = {}
QR_TTL = 300  # 5 分钟过期


def _new_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
        "Referer": PASSPORT + "/sso/signin?entry=wapsso",
    })
    return s


def _urlencode(s: str) -> str:
    return quote(s, safe="")


def generate_qrcode() -> dict:
    """请求微博生成二维码，返回 qrid + 图片 URL。"""
    s = _new_session()
    resp = s.get(QR_IMAGE_URL, params={"entry": "wapsso", "size": "180"},
                 timeout=15)
    resp.raise_for_status()
    data = resp.json()
    if data.get("retcode") != 20000000:
        raise RuntimeError(f"获取二维码失败：{data.get('msg')}")
    d = data["data"]
    qrid = d["qrid"]
    image_url = d["image"]
    QR_SESSIONS[qrid] = {
        "qr_image": image_url,
        "created_at": time.time(),
        "status": "pending",
        "cookies": {},
        "uid": "",
        "username": "",
        "session": s,   # 保留会话以接收 Set-Cookie
    }
    # 清理过期会话
    now = time.time()
    for k in list(QR_SESSIONS.keys()):
        if now - QR_SESSIONS[k]["created_at"] > QR_TTL:
            QR_SESSIONS.pop(k, None)
    return {"qrid": qrid, "image": image_url}


def check_qrcode(qrid: str) -> dict:
    """轮询二维码状态。确认成功后解析登录 Cookie 存入会话。"""
    sess = QR_SESSIONS.get(qrid)
    if not sess:
        return {"status": "expired", "message": "二维码不存在或已过期"}
    if time.time() - sess["created_at"] > QR_TTL:
        sess["status"] = "expired"
        return {"status": "expired", "message": "二维码已过期"}

    s = sess["session"]
    params = {
        "entry": "wapsso",
        "source": "wapsso",
        "url": f"https:%2F%2Fm.weibo.cn",
        "qrid": qrid,
        "rid": "",
        "ver": "20250520",
    }
    try:
        resp = s.get(QR_CHECK_URL, params=params, timeout=15)
        data = resp.json()
    except Exception as exc:
        return {"status": "error", "message": f"轮询失败：{exc}"}

    retcode = data.get("retcode")
    ret, msg = RET.get(retcode, ("unknown", data.get("msg", "")))

    if ret == "success":
        # 扫码确认成功：从会话 cookie 中提取登录态
        cookies = dict(s.cookies)
        # 从跨域 Set-Cookie 拿登录凭证（SUB 等）。若无则尝试 follow crossid
        # 保存会话 cookie
        sess["cookies"] = cookies
        sess["status"] = "success"
        sess["crossid"] = (data.get("data") or {}).get("crossid", "")
        # 尝试解析 uid / 昵称
        sess["uid"] = (data.get("data") or {}).get("uid", "")
        sess["username"] = (data.get("data") or {}).get("screen_name", "")
        return {
            "status": "success",
            "message": msg,
            "cookies": cookies,
            "has_cookie": bool(cookies.get("SUB")),
            "crossid": sess["crossid"],
        }

    sess["status"] = ret
    return {"status": ret, "message": msg}


def finalize_login(qrid: str) -> dict:
    """确认成功后，获取完整登录凭证。
    若 check 已带回 cookie 直接用；否则通过 crossid 完成登录换取 cookie。
    """
    sess = QR_SESSIONS.get(qrid)
    if not sess or sess.get("status") != "success":
        return {"ok": False, "message": "尚未确认登录"}

    # 情况1：已有 SUB cookie
    if sess["cookies"].get("SUB"):
        return {
            "ok": True,
            "cookies": sess["cookies"],
            "uid": sess.get("uid", ""),
            "username": sess.get("username", ""),
        }

    # 情况2：需要 follow crossid/location 完成登录
    crossid = sess.get("crossid", "")
    if crossid:
        s = sess["session"]
        try:
            # 访问跨域登录链接拿 Set-Cookie
            resp = s.get(
                f"https://passport.weibo.com/sso/crossdomain?crossid={crossid}",
                timeout=15, allow_redirects=True,
            )
            cookies = dict(s.cookies)
            sess["cookies"] = cookies
            if cookies.get("SUB"):
                return {
                    "ok": True,
                    "cookies": cookies,
                    "uid": sess.get("uid", ""),
                    "username": sess.get("username", ""),
                }
        except Exception as exc:
            log.warning("crossdomain 获取 cookie 失败: %s", exc)

    return {"ok": False, "message": "未能获取完整 Cookie，请重试"}
