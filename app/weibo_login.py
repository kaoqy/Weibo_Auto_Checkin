"""
微博 m.weibo.cn 扫码登录模块（Playwright 版）。

纯 requests 调用 passport 的 qrcode/check 会因缺失微博的人机验证 SDK
(wbBotDetector) 生成的 rid 而报 -479 system error 风控。
本模块用 Playwright 无头浏览器加载真实登录页，从而：
    - 获得有效的 rid（wbBotDetector.get()）
    - 获得二维码图片 URL
    - 在真实浏览器上下文里轮询扫描状态，避免风控
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from urllib.parse import unquote, urlparse, parse_qs

log = logging.getLogger("weibo.qrlogin")

# 内存中的扫码会话：qrid -> {...}
QR_SESSIONS: dict[str, dict] = {}
QR_TTL = 300  # 5 分钟

_login_url = (
    "https://passport.weibo.com/sso/signin?entry=wapsso"
    "&source=wapsso&url=https%3A%2F%2Fm.weibo.cn"
)

_browser = None
_ctx = None
_page = None
_browser_lock = threading.Lock()
_pw_obj = None           # 保存 sync_playwright 实例以便 close
_last_use = 0.0          # 最后使用时间戳
BROWSER_IDLE_TIMEOUT = 300  # 5 分钟空闲自动关闭释放内存


def _sweep_idle_browser():
    """若浏览器空闲超时则关闭并释放内存（资源占用优化）。"""
    global _browser, _ctx, _page, _pw_obj
    with _browser_lock:
        if _page is None:
            return
        if _page.is_closed():
            _browser = _ctx = _page = _pw_obj = None
            return
        if time.time() - _last_use < BROWSER_IDLE_TIMEOUT:
            return
        try:
            _browser.close()
        except Exception:
            pass
        try:
            if _pw_obj:
                _pw_obj.stop()
        except Exception:
            pass
        _browser = _ctx = _page = _pw_obj = None
        log.info("浏览器空闲超时，已关闭释放内存")


def close_browser():
    """主动关闭浏览器（可被 shutdown 钩子调用）。"""
    global _browser, _ctx, _page, _pw_obj
    with _browser_lock:
        try:
            if _page is not None and not _page.is_closed():
                _browser.close()
        except Exception:
            pass
        try:
            if _pw_obj:
                _pw_obj.stop()
        except Exception:
            pass
        _browser = _ctx = _page = _pw_obj = None


class QrLoginError(RuntimeError):
    pass


def _ensure_browser():
    """懒启动 Playwright 浏览器（同步 API，线程锁保护）。空闲会自动关闭。"""
    global _browser, _ctx, _page, _pw_obj, _last_use
    with _browser_lock:
        _last_use = time.time()
        if _page is not None and not _page.is_closed():
            return _page
        from playwright.sync_api import sync_playwright

        pw = sync_playwright().start()
        _pw_obj = pw
        try:
            browser = pw.chromium.launch(
                headless=True, args=["--no-sandbox", "--disable-gpu"],
            )
        except Exception:
            # 兜底：用环境变量指定可执行路径
            import os
            exe = os.environ.get("CHROMIUM_PATH", "")
            if not exe:
                raise
            browser = pw.chromium.launch(
                headless=True, executable_path=exe,
                args=["--no-sandbox", "--disable-gpu"],
            )
        ctx = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
        )
        page = ctx.new_page()
        _browser, _ctx, _page = browser, ctx, page
        return page


def _load_login_page(page, timeout_ms=30000):
    """导航到登录页，等待二维码与 wbBotDetector 就绪。"""
    try:
        page.goto(_login_url, timeout=timeout_ms, wait_until="domcontentloaded")
    except Exception as exc:
        log.warning("导航登录页: %s", exc)
    # 等待 wbBotDetector 与二维码渲染
    try:
        page.wait_for_function(
            "window.wbBotDetector && window.wbBotDetector.get",
            timeout=timeout_ms,
        )
    except Exception:
        pass
    page.wait_for_timeout(1500)


def _get_rid(page) -> str:
    """从 wbBotDetector 获取真实 rid（过风控的关键）。"""
    try:
        return page.evaluate(
            """() => new Promise(res => {
                if (window.wbBotDetector && window.wbBotDetector.get) {
                    window.wbBotDetector.get({useCache:false})
                        .then(y => res((y && y.rid) ? y.rid : 'getriderror'))
                        .catch(e => res('getriderror:' + e));
                } else res('nodetector');
            })"""
        )
    except Exception as exc:
        log.warning("获取 rid 失败: %s", exc)
        return ""


def _extract_qrcode(page) -> dict:
    """从页面提取二维码信息：图片 URL 与 qrid。"""
    # 找二维码图片
    img_src = page.evaluate(
        """() => {
            const imgs = Array.from(document.images).map(i => i.src);
            const qr = imgs.find(s => s.includes('qr.weibo.cn/inf/gen') || s.includes('/qr/') || s.includes('qrcode'));
            return qr || '';
        }"""
    )
    # 从图片 URL 解析 data 里的 qr 参数（含 qrid）
    qrid = ""
    if img_src:
        parsed = urlparse(img_src)
        qs = parse_qs(parsed.query)
        data_url = qs.get("data", [""])[0]
        # data 里含 https://passport.weibo.cn/signin/qrcode/scan?qr=xxx..
        inner = parse_qs(unquote(data_url))
        qr_param = inner.get("qr", [""])[0]
        qrid = qr_param
        if not qrid and "qr=" in data_url:
            qrid = data_url.split("qr=", 1)[-1].split("&", 1)[0]
    return {"image": img_src, "qrid": qrid}


def generate_qrcode() -> dict:
    """加载登录页并返回 {qrid, image}。会话缓存在内存。"""
    page = _ensure_browser()
    with _browser_lock:
        _load_login_page(page)
        rid = _get_rid(page)
        info = _extract_qrcode(page)

    if not info["image"] or not info["qrid"]:
        # 重试一次（页面可能未加载完）
        page.reload()
        page.wait_for_timeout(2500)
        with _browser_lock:
            rid = _get_rid(page)
            info = _extract_qrcode(page)

    if not info["image"]:
        raise QrLoginError("未能从页面获取二维码，请稍后重试")

    qrid = info["qrid"]
    QR_SESSIONS[qrid] = {
        "image": info["image"],
        "rid": rid,
        "created_at": time.time(),
        "status": "pending",
        "cookies": {},
        "uid": "",
        "username": "",
    }
    # 清理过期
    now = time.time()
    for k in list(QR_SESSIONS.keys()):
        if now - QR_SESSIONS[k]["created_at"] > QR_TTL:
            QR_SESSIONS.pop(k, None)

    return {"qrid": qrid, "image": info["image"]}


def check_qrcode(qrid: str) -> dict:
    """在真实浏览器上下文里轮询扫码状态，返回可序列化结果。"""
    sess = QR_SESSIONS.get(qrid)
    if not sess:
        return {"status": "expired", "message": "二维码不存在或已过期"}
    if time.time() - sess["created_at"] > QR_TTL:
        sess["status"] = "expired"
        return {"status": "expired", "message": "二维码已过期"}

    page = _ensure_browser()
    rid = sess.get("rid") or _get_rid(page)
    with _browser_lock:
        result = page.evaluate(
            """([qrid, rid]) => fetch(
                '/sso/v2/qrcode/check?entry=wapsso&source=wapsso'
                + '&url=' + encodeURIComponent('https://m.weibo.cn')
                + '&qrid=' + encodeURIComponent(qrid)
                + '&rid=' + encodeURIComponent(rid)
                + '&ver=20250520',
                {credentials: 'include', headers: {'Accept': 'application/json'}}
            ).then(r => r.json()).then(d => ({code: d.retcode, msg: d.msg, data: d.data}))
             .catch(e => ({code: -1, msg: String(e)}))""",
            [qrid, rid],
        )

    retcode = result.get("code")
    msg = result.get("msg", "")
    # 状态映射
    if retcode == 20000000:
        # 确认成功
        sess["status"] = "success"
        cookies = _collect_cookies(page)
        uid = (result.get("data") or {}).get("uid", "")
        sess["uid"] = uid
        sess["cookies"] = cookies
        return {
            "status": "success", "message": msg,
            "cookies": cookies,
            "has_cookie": bool(cookies.get("SUB")),
            "uid": uid,
        }
    elif retcode == 50114001:
        sess["status"] = "pending"
        return {"status": "pending", "message": msg or "等待扫码"}
    elif retcode == 50114002:
        sess["status"] = "scanned"
        return {"status": "scanned", "message": msg or "已扫码，等待确认"}
    elif retcode in (50114003, 50114004):
        sess["status"] = "expired"
        return {"status": "expired", "message": msg or "二维码失效"}
    else:
        sess["status"] = "unknown"
        return {"status": "unknown", "message": msg or f"未知状态({retcode})"}


def _collect_cookies(page) -> dict:
    """收集当前上下文里 passport/weibo 域的 cookie。"""
    cookies = {}
    try:
        all_c = page.context.cookies()
        for c in all_c:
            if c["value"]:
                cookies[c["name"]] = c["value"]
    except Exception as exc:
        log.warning("收集 cookie: %s", exc)
    return cookies


def finalize_login(qrid: str) -> dict:
    """扫码确认后获取完整登录 Cookie。"""
    sess = QR_SESSIONS.get(qrid)
    if not sess or sess.get("status") != "success":
        return {"ok": False, "message": "尚未确认登录"}

    cookies = sess.get("cookies") or {}
    # 若缺 SUB，尝试触发跨域登录后再收集
    if not cookies.get("SUB"):
        page = _ensure_browser()
        try:
            with _browser_lock:
                page.goto(
                    "https://passport.weibo.com/sso/crossdomain",
                    timeout=15000, wait_until="domcontentloaded",
                )
                page.wait_for_timeout(2000)
            cookies = _collect_cookies(page)
            sess["cookies"] = cookies
        except Exception as exc:
            log.warning("crossdomain 获取 cookie: %s", exc)

    if cookies.get("SUB"):
        return {
            "ok": True,
            "cookies": cookies,
            "uid": sess.get("uid", ""),
            "username": sess.get("username", ""),
        }
    # 从 cookie 尝试推断 uid/昵称
    return {
        "ok": bool(cookies),
        "cookies": cookies,
        "uid": sess.get("uid", ""),
        "username": "",
        "message": "" if cookies else "未获取到完整 Cookie",
    }
