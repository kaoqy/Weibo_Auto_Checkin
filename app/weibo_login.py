"""
微博 m.weibo.cn 扫码登录模块（Playwright async 版）。

纯 requests 调用 passport 的 qrcode/check 会因缺失微博的人机验证 SDK
(wbBotDetector) 生成的 rid 而报 -479 system error 风控。
本模块用 Playwright 无头浏览器加载真实登录页，从而：
    - 获得有效的 rid（wbBotDetector.get()）
    - 获得二维码图片 URL
    - 在真实浏览器上下文里轮询扫描状态，避免风控

⚠️ 线程安全说明：
必须使用 async 版本（async_playwright）。Playwright 的 sync API 不是线程安全的，
若在 FastAPI 同步端点（线程池线程）里启动，浏览器实例会绑定该线程的 greenlet/
event loop；请求结束线程退出后再被其他线程复用会报
“cannot switch to a different thread (which happens to have exited)”。
故本模块所有函数均为 async，且浏览器实例常驻 FastAPI 主事件循环（所有 async
端点共享同一 loop），保证跨请求复用不崩。
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from urllib.parse import unquote, urlparse, parse_qs

log = logging.getLogger("weibo.qrlogin")

# 内存中的扫码会话：qrid -> {...}
QR_SESSIONS: dict[str, dict] = {}
QR_TTL = 300  # 5 分钟（从生成起）

_login_url = (
    "https://passport.weibo.com/sso/signin?entry=wapsso"
    "&source=wapsso&url=https%3A%2F%2Fm.weibo.cn%2F"
)

# 浏览器实例（模块级，常驻主事件循环）
_playwright = None
_browser = None
_ctx = None
_page = None
_browser_ready = asyncio.Event()   # 首次启动完成标记（可忽略，靠锁兜底）
_launch_lock = None                # 惰性创建的 asyncio.Lock
_last_use = 0.0
BROWSER_IDLE_TIMEOUT = 300         # 5 分钟空闲自动关闭释放内存


def _get_lock() -> asyncio.Lock:
    global _launch_lock
    if _launch_lock is None:
        _launch_lock = asyncio.Lock()
    return _launch_lock


async def close_browser():
    """主动关闭浏览器（可被 shutdown 钩子调用）。"""
    global _playwright, _browser, _ctx, _page
    async with _get_lock():
        try:
            if _page is not None and not _page.is_closed():
                await _browser.close()
        except Exception:
            pass
        try:
            if _playwright is not None:
                await _playwright.stop()
        except Exception:
            pass
        _playwright = _browser = _ctx = _page = None


async def _sweep_idle_browser():
    """若浏览器空闲超时则关闭并释放内存。"""
    global _playwright, _browser, _ctx, _page
    if _page is None:
        return
    async with _get_lock():
        if _page is None:
            return
        try:
            if _page.is_closed():
                _playwright = _browser = _ctx = _page = None
                return
        except Exception:
            _playwright = _browser = _ctx = _page = None
            return
        if time.time() - _last_use < BROWSER_IDLE_TIMEOUT:
            return
        try:
            await _browser.close()
        except Exception:
            pass
        try:
            await _playwright.stop()
        except Exception:
            pass
        _playwright = _browser = _ctx = _page = None
        log.info("浏览器空闲超时，已关闭释放内存")


class QrLoginError(RuntimeError):
    pass


# 最近一次使用的扫码 SOCKS 代理（用于日志/调试）
_LAST_PROXY_URL = ""


def _get_scan_proxy() -> dict | None:
    """取面板第一个启用中的 socks 代理，作为扫码浏览器的代理。

    返回 playwright 的 proxy 配置（server 为 socks5://…，远端 DNS 解析，等价
    socks5h 行为）；无启用代理则返回 None（直连）。先查环境变量 WCM_QR_PROXY
    覆盖，否则读数据库 proxies 表第一个 enabled。
    """
    global _LAST_PROXY_URL
    import os
    url = os.environ.get("WCM_QR_PROXY", "").strip()
    if not url:
        try:
            from . import database
            rows = database.get_proxies(include_disabled=False)
            if rows:
                url = rows[0].get("url") or database.build_proxy_url(rows[0])
        except Exception as exc:
            log.warning("读取扫码代理失败: %s", exc)
    if not url:
        _LAST_PROXY_URL = ""
        return None
    # playwright 只认 socks5://（socks5h 语义即远端解析，等价）；转成它能吃的形式
    if url.startswith("socks5h://"):
        url = "socks5://" + url[len("socks5h://"):]
    proxy_cfg = {"server": url}
    _LAST_PROXY_URL = url
    log.info("扫码浏览器走 socks5 代理: %s", _LAST_PROXY_URL.split("@")[-1])
    return proxy_cfg


async def _ensure_browser():
    """懒启动 Playwright 浏览器（async 版，锁保护，绑定当前事件循环）。

    若面板配置了启用中的 socks 代理（proxies 表第一个 enabled），会通过该
    socks5 代理访问微博（远端 DNS 解析，等价 socks5h），更稳地规避风控；
    无可用代理则直连。
    """
    global _playwright, _browser, _ctx, _page, _last_use
    async with _get_lock():
        _last_use = time.time()
        if _page is not None:
            try:
                if not _page.is_closed():
                    return _page
            except Exception:
                pass
            _playwright = _browser = _ctx = _page = None

        from playwright.async_api import async_playwright

        pw = await async_playwright().start()
        _playwright = pw
        try:
            browser = await pw.chromium.launch(
                headless=True, args=["--no-sandbox", "--disable-gpu"],
            )
        except Exception:
            # 兜底：用环境变量指定可执行路径
            import os
            exe = os.environ.get("CHROMIUM_PATH", "")
            if not exe:
                raise
            browser = await pw.chromium.launch(
                headless=True, executable_path=exe,
                args=["--no-sandbox", "--disable-gpu"],
            )
        proxy_cfg = _get_scan_proxy()
        ctx_kwargs = dict(
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
        )
        if proxy_cfg:
            ctx_kwargs["proxy"] = proxy_cfg
        ctx = await browser.new_context(**ctx_kwargs)
        page = await ctx.new_page()
        _browser, _ctx, _page = browser, ctx, page
        return page


async def _load_login_page(page, timeout_ms=30000):
    """导航到登录页，等待二维码与 wbBotDetector 就绪。"""
    try:
        await page.goto(_login_url, timeout=timeout_ms, wait_until="domcontentloaded")
    except Exception as exc:
        log.warning("导航登录页: %s", exc)
    # 等待 wbBotDetector 与二维码渲染
    try:
        await page.wait_for_function(
            "window.wbBotDetector && window.wbBotDetector.get",
            timeout=timeout_ms,
        )
    except Exception:
        pass
    await page.wait_for_timeout(1500)


async def _get_rid(page) -> str:
    """从 wbBotDetector 获取真实 rid（过风控的关键）。"""
    try:
        return await page.evaluate(
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


async def _extract_qrcode(page) -> dict:
    """从页面提取二维码信息：图片 URL 与 qrid。"""
    img_src = await page.evaluate(
        """() => {
            const imgs = Array.from(document.images).map(i => i.src);
            const qr = imgs.find(s => s.includes('qr.weibo.cn/inf/gen') || s.includes('/qr/') || s.includes('qrcode'));
            return qr || '';
        }"""
    )
    qrid = ""
    qr_content = ""  # 二维码内容的原始 data URL（扫码 payload）
    if img_src:
        parsed = urlparse(img_src)
        qs = parse_qs(parsed.query)
        data_url = qs.get("data", [""])[0]
        qr_content = data_url
        inner = parse_qs(unquote(data_url))
        qr_param = inner.get("qr", [""])[0]
        qrid = qr_param
        if not qrid and "qr=" in data_url:
            qrid = data_url.split("qr=", 1)[-1].split("&", 1)[0]
    return {"image": img_src, "qrid": qrid, "content": qr_content}


async def _qrcode_png_b64(page, timeout_ms=30000) -> str:
    """对页面上的二维码元素截图，返回 base64 PNG（前端可直接 <img> 显示）。

    微博二维码图片 URL 带防盗链/时效参数，前端直接 <img src=外链> 可能加载失败；
    这里由已加载好二维码的无头浏览器把二维码元素渲染成 PNG 再返回，保证前端 100%
    显示且不带 cookie 依赖。
    """
    import base64 as _b64
    try:
        # 用 locator 定位二维码 <img>（页面渲染好后元素存在）
        loc = page.locator(
            "img[src*='qr.weibo.cn/inf/gen'], img[src*='/qr/'], img[src*='qrcode']"
        ).first
        await loc.wait_for(state="visible", timeout=timeout_ms)
        buf = await loc.screenshot()
        return _b64.b64encode(buf).decode()
    except Exception as exc:
        log.warning("二维码元素截图失败: %s", exc)
        try:
            # 兜底：截取整个可见区域并裁中间方形
            buf = await page.screenshot(clip={"x": 0, "y": 0, "width": 180, "height": 180})
            return _b64.b64encode(buf).decode()
        except Exception as exc2:
            log.warning("整页截图兜底失败: %s", exc2)
            return ""


async def generate_qrcode() -> dict:
    """加载登录页并返回 {qrid, image}。会话缓存在内存。"""
    page = await _ensure_browser()
    await _load_login_page(page)
    rid = await _get_rid(page)
    info = await _extract_qrcode(page)

    if not info["image"] or not info["qrid"]:
        # 重试一次（页面可能未加载完）
        await page.reload()
        await page.wait_for_timeout(2500)
        rid = await _get_rid(page)
        info = await _extract_qrcode(page)

    if not info["image"]:
        raise QrLoginError("未能从页面获取二维码，请稍后重试")

    # 用无头浏览器把二维码渲染成 base64 PNG，前端可 100% 显示（避免外链防盗链）
    png_b64 = await _qrcode_png_b64(page)

    qrid = info["qrid"]
    QR_SESSIONS[qrid] = {
        "image": info["image"],
        "image_b64": png_b64,
        "content": info.get("content", ""),
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

    return {"qrid": qrid, "image": info["image"], "image_b64": png_b64}


async def check_qrcode(qrid: str) -> dict:
    """在真实浏览器上下文里轮询扫码状态，返回可序列化结果。

    扫码确认成功后，微博服务端会把登录态通过 crossdomain 回调写入浏览器
    （触发 passport/weibo 域 cookie 下发）。这里在检测到成功后主动等待并导航到
    crossdomain 页面，确保拿到完整 Cookie，让前端“等待手机确认”后能真正回调成功。
    """
    await _sweep_idle_browser()
    sess = QR_SESSIONS.get(qrid)
    if not sess:
        return {"status": "expired", "message": "二维码不存在或已过期"}
    if time.time() - sess["created_at"] > QR_TTL:
        sess["status"] = "expired"
        return {"status": "expired", "message": "二维码已过期"}

    page = await _ensure_browser()
    rid = sess.get("rid") or await _get_rid(page)
    try:
        result = await page.evaluate(
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
    except Exception as exc:
        log.warning("扫码状态查询异常: %s", exc)
        return {"status": "unknown", "message": f"查询异常：{str(exc)[:100]}"}

    retcode = result.get("code")
    msg = result.get("msg", "")
    # 状态映射
    if retcode == 20000000:
        # 确认成功：等待服务端跨域写入 cookie，并用真实浏览器收集完整登录态
        sess["status"] = "success"
        uid = (result.get("data") or {}).get("uid", "")
        sess["uid"] = uid or sess.get("uid", "")
        cookies = await _finalize_cookies(page)
        sess["cookies"] = cookies
        return {
            "status": "success", "message": msg or "扫码登录成功",
            "cookies": cookies,
            "has_cookie": bool(cookies.get("SUB")),
            "uid": sess["uid"],
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


async def _collect_cookies(page) -> dict:
    """收集当前上下文里 passport/weibo 域的 cookie。"""
    cookies = {}
    try:
        all_c = await page.context.cookies()
        for c in all_c:
            if c["value"]:
                cookies[c["name"]] = c["value"]
    except Exception as exc:
        log.warning("收集 cookie: %s", exc)
    return cookies


# 需要重点保留的登录态 cookie（缺一不可视为未完成登录）
_REQUIRED_COOKIES = ("SUB", "SUBP")


async def _finalize_cookies(page) -> dict:
    """扫码确认后，主动走微博跨域回调，确保拿到完整登录 Cookie。

    扫码成功后仅凭当前页面的 cookie 往往不完整（缺少 SUB 等），
    需导航到 passport 的 crossdomain 回调地址触发 Set-Cookie。
    此处等待回调完成并反复收集，最多重试数次。
    """
    cookies = await _collect_cookies(page)
    if all(cookies.get(k) for k in _REQUIRED_COOKIES):
        return cookies
    try:
        for _ in range(3):
            await page.goto(
                "https://passport.weibo.com/sso/crossdomain",
                timeout=15000, wait_until="domcontentloaded",
            )
            await page.wait_for_timeout(2500)
            cookies = await _collect_cookies(page)
            if all(cookies.get(k) for k in _REQUIRED_COOKIES):
                break
        # 再导航一次 m.weibo.cn，触发 wap 域登录态写入
        if all(cookies.get(k) for k in _REQUIRED_COOKIES):
            try:
                await page.goto(
                    "https://m.weibo.cn",
                    timeout=15000, wait_until="domcontentloaded",
                )
                await page.wait_for_timeout(2000)
                cookies.update(await _collect_cookies(page))
            except Exception:
                pass
    except Exception as exc:
        log.warning("crossdomain 获取 cookie: %s", exc)
    return cookies


async def finalize_login(qrid: str) -> dict:
    """扫码确认后获取完整登录 Cookie。

    允许在 scanned（已扫码/刚确认）状态下调用：会主动触发 crossdomain 回调
    并尝试补全 Cookie；若仍拿不到 SUB 才视为未完成。
    """
    await _sweep_idle_browser()
    sess = QR_SESSIONS.get(qrid)
    if not sess:
        return {"ok": False, "message": "二维码不存在或已过期"}
    if time.time() - sess.get("created_at", 0) > QR_TTL:
        return {"ok": False, "message": "二维码已过期，请重新扫码"}
    if sess.get("status") not in ("success", "scanned"):
        return {"ok": False, "message": "尚未扫码确认，请先扫码并在手机上确认"}

    cookies = sess.get("cookies") or {}
    if not cookies.get("SUB"):
        page = await _ensure_browser()
        try:
            cookies = await _finalize_cookies(page)
            sess["cookies"] = cookies
            if cookies.get("SUB"):
                sess["status"] = "success"
        except Exception as exc:
            log.warning("crossdomain 获取 cookie: %s", exc)

    if cookies.get("SUB"):
        return {
            "ok": True,
            "cookies": cookies,
            "uid": sess.get("uid", ""),
            "username": sess.get("username", ""),
        }
    return {
        "ok": bool(cookies),
        "cookies": cookies,
        "uid": sess.get("uid", ""),
        "username": "",
        "message": "" if cookies else "未获取到完整 Cookie",
    }
