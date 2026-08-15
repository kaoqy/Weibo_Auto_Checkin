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
    """检测扫码状态（以页面自动跳转 m.weibo.cn 为准，接口仅作辅助提示）。

    核心逻辑（按用户反馈）：扫码确认成功后，后端浏览器里的登录页会自动重定向到
    m.weibo.cn，无需依赖 qrcode/check 接口的 retcode 判断成败。只要检测到页面
    已跳转，就从浏览器上下文抓取完整 Cookie（SUB/SUBP 等）。接口查询失败（400、
    rid 失效等）不再误判为「过期」——过期仅由二维码生命周期 TTL 兜底判定。
    """
    await _sweep_idle_browser()
    sess = QR_SESSIONS.get(qrid)
    if not sess:
        return {"status": "expired", "message": "二维码不存在或已过期"}
    if time.time() - sess["created_at"] > QR_TTL:
        sess["status"] = "expired"
        return {"status": "expired", "message": "二维码已过期，请重新生成"}

    page = await _ensure_browser()

    # ① 会话已成功 → 直接返回已抓 Cookie
    if sess.get("status") == "success":
        ck = sess.get("cookies") or {}
        return {"status": "success", "message": "扫码登录成功", "cookies": ck,
                "has_cookie": bool(ck.get("SUB")), "uid": sess.get("uid", "")}

    # ② 关键：页面是否已自动重定向到 m.weibo.cn（扫码确认成功的标志）
    try:
        cur_url = (page.url or "").lower()
    except Exception:
        cur_url = ""
    if "m.weibo.cn" in cur_url:
        cookies, uid = await _finalize_with_uid(page, sess)
        # 必须拿到真实登录态 SUB 才算成功（X-CSRF-TOKEN 等临时 cookie 不算）
        if cookies.get("SUB"):
            sess["status"] = "success"
            return {"status": "success", "message": "扫码登录成功", "cookies": cookies,
                    "has_cookie": True, "uid": uid}
        sess["status"] = "pending"
        return {"status": "pending", "message": "等待扫码或在手机上确认…"}

    # ③ 接口仅作状态提示（pending/scanned）；出错/400 不判过期
    retcode, msg = await _query_status(page, sess, qrid)

    if retcode == 20000000:
        # 接口确认成功：passport 回调链需要一点时间把页面自动跳转到 m.weibo.cn
        # 并把登录态 cookie 写入 context。等跳转（最多 ~12s），再读 cookie。
        jumped = False
        try:
            await page.wait_for_url(
                "**m.weibo.cn**", timeout=12000, wait_until="domcontentloaded")
            jumped = True
        except Exception:
            jumped = False
        cookies, uid = await _finalize_with_uid(page, sess)
        if cookies.get("SUB"):
            sess["status"] = "success"
            return {"status": "success", "message": msg or "扫码登录成功",
                    "cookies": cookies, "has_cookie": True, "uid": uid}
        # 页面还没拿到登录态 → 让前端继续轮询
        sess["status"] = "scanned"
        return {"status": "scanned", "message": "已确认，正在获取登录态…"}

    if retcode == 50114002:
        sess["status"] = "scanned"
        return {"status": "scanned", "message": msg or "已扫码，请在手机上确认"}

    if retcode == 50114001:
        sess["status"] = "pending"
        return {"status": "pending", "message": msg or "等待扫码"}

    if retcode in (50114003, 50114004):
        # 接口认为过期：但可能是 rid 失效误报，用户可能其实已扫。不判死，
        # 交由页面跳转检测与 TTL 兜底；这里保持 pending 提示。
        sess["status"] = "pending"
        return {"status": "pending", "message": "等待扫码（二维码待刷新）…"}

    # retcode in (None, -1, 其他未知) → 接口查询失败（400/rid失效/风控），不判过期
    sess["status"] = "pending"
    return {"status": "pending", "message": msg or "正在确认扫码状态…"}


async def _query_status(page, sess: dict, qrid: str) -> tuple:
    """调用 qrcode/check 接口查询状态；失败返回 (None, 提示)，不抛异常。"""
    rid = sess.get("rid") or ""
    if not rid:
        try:
            rid = await _get_rid(page)
        except Exception:
            rid = ""
    if not rid or rid in ("getriderror", "nodetector"):
        return None, "正在确认扫码状态…"
    try:
        result = await page.evaluate(
            """([qrid, rid]) => fetch(
                '/sso/v2/qrcode/check?entry=wapsso&source=wapsso'
                + '&url=' + encodeURIComponent('https://m.weibo.cn')
                + '&qrid=' + encodeURIComponent(qrid)
                + '&rid=' + encodeURIComponent(rid)
                + '&ver=20250520',
                {credentials: 'include', headers: {'Accept': 'application/json'}}
            ).then(r => r.json()).then(d => ({code: d.retcode, msg: d.msg}))
             .catch(e => ({code: -1, msg: String(e)}))""",
            [qrid, rid],
        )
        return result.get("code"), result.get("msg") or ""
    except Exception as exc:
        log.warning("扫码状态查询异常: %s", exc)
        return None, "正在确认扫码状态…"


async def _finalize_with_uid(page, sess: dict):
    """页面已跳转/确认后抓取完整 Cookie，并尽力提取 uid。"""
    cookies = await _finalize_cookies(page)
    sess["cookies"] = cookies
    uid = sess.get("uid", "")
    if not uid:
        uid = await _extract_uid(page)
        if uid:
            sess["uid"] = uid
    return cookies, uid


async def _extract_uid(page) -> str:
    """从 m.weibo.cn 页面或接口提取当前登录用户 uid。"""
    try:
        v = await page.evaluate(
            """() => {
                try {
                    if (window.config && window.config.userInfo && window.config.userInfo.id)
                        return String(window.config.userInfo.id);
                } catch(e){}
                return '';
            }"""
        )
        if v and isinstance(v, str) and v.strip():
            return v
    except Exception:
        pass
    # 兜底：SUB cookie 常含 uid（SUB 格式 "xxxx;xxxx"），解析首段数字
    try:
        allc = await page.context.cookies()
        for c in allc:
            if (c["name"] == "SUB" or c["name"] == "SUBP") and c["value"]:
                seg = c["value"].split(";")[0].split(":")[0]
                if seg.isdigit():
                    return seg
    except Exception:
        pass
    return ""


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
    """扫码确认后获取完整登录 Cookie。

    按实际回调流程：扫码确认后，passport 会**自动**走跨域回调链并最终重定向到
    m.weibo.cn，登录态 cookie（SUB/SUBP/SSOLoginState 等）由这条回调链自动写入
    browser context。这里只需**等待页面自动跳转到 m.weibo.cn**，然后直接从
    context 读取全部 cookie —— 不再主动导航裸的 crossdomain（不带回调参数
    不会触发 Set-Cookie，反而会打断自动跳转）。
    """
    try:
        # 1) 优先等页面自动跳转到 m.weibo.cn（扫码确认后的目标页，最多 15s）
        try:
            await page.wait_for_url("**m.weibo.cn**", timeout=15000,
                                    wait_until="domcontentloaded")
        except Exception:
            pass
        # 2) 若页面不在 m.weibo.cn，导航过去触发 wap 域跨域 cookie 落齐
        if "m.weibo.cn" not in (page.url or ""):
            try:
                await page.goto("https://m.weibo.cn", timeout=15000,
                                wait_until="domcontentloaded")
                await page.wait_for_timeout(2000)
            except Exception:
                pass
        cookies = await _collect_cookies(page)
        # 3) 若仍缺 SUB，再导航 m.weibo.cn 一次让回调链 cookie 落齐
        if not cookies.get("SUB"):
            try:
                await page.goto("https://m.weibo.cn", timeout=15000,
                                wait_until="domcontentloaded")
                await page.wait_for_timeout(2500)
                cookies = await _collect_cookies(page)
            except Exception:
                pass
    except Exception as exc:
        log.warning("finalize cookies: %s", exc)
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
