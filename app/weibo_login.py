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
import json
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
    url = (os.environ.get("WCM_QR_PROXY") or "").strip()
    # 兼容旧配置把 WCM_QR_PROXY 设成空壳值（如 "[]"/"None"/"null"）：
    # 视为显式“不启用扫码代理”并直接直连。
    if url.lower() in ("[]", "none", "null", "-", "off"):
        _LAST_PROXY_URL = ""
        return None
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
    # 校验代理协议：只接受 socks5/socks5h/http 前缀，非法地址（含任意垃圾值）
    # 一律退回直连，避免 Playwright ERR_SOCKS_CONNECTION_FAILED。
    low = url.lower()
    if not (low.startswith("socks5://") or low.startswith("socks5h://")
            or low.startswith("http://") or low.startswith("https://")):
        log.warning("扫码代理地址非法，退回直连: %s", url.split("@")[-1])
        _LAST_PROXY_URL = ""
        return None
    # Playwright/Chromium 不支持带认证的 socks5（必然 ERR_SOCKS_CONNECTION_FAILED），
    # 检测到 socks5 地址含 user:pass 凭据时退回直连（微博直连通常可用）。
    if low.startswith("socks5://") or low.startswith("socks5h://"):
        after = url.split("://", 1)[-1]
        if "@" in after:
            credentials = after.split("@", 1)[0]
            if ":" in credentials:
                log.warning("socks5 代理含认证凭据，Playwright 不支持，退回直连")
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
    # 仅清除上次扫码遗留的登录态 cookie（避免跨账号抓到旧账号），但保留 passport
    # 登录页/回调链所需的初始 cookie（XSRF-TOKEN 等）—— 整体 clear_cookies 会清掉
    # 它们，导致扫码确认后的跨域回调无法把登录态写回 .weibo.cn（38 上确认后抓不到
    # 登录态的根因）。
    try:
        for _cn in ("SUB", "SUBP", "SCF", "ALF", "SSOLoginState",
                    "MLOGIN", "ALC", "mweibo_short_token"):
            try:
                await page.context.clear_cookies(name=_cn)
            except Exception:
                pass
    except Exception as exc:
        log.warning("清登录态 cookie: %s", exc)
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
    """检测扫码状态（接口轮询为主，login 验证兜底）。

    状态机：
      1. 会话已成功 → 直接返回已抓 Cookie。
      2. 接口 qrcode/check 轮询（准确知道 pending/已扫码/已确认）：
         - 20000000 已确认 → 等页面跳转 m.weibo.cn → 抓 Cookie → 验证 /api/config
           login:true 才算 success，否则 scanned 继续轮询。
         - 50114002 已扫码待确认 → scanned。
         - 50114001 未扫码 → pending。
         - 其他/出错(400/rid失效) → 不做死判定，交给 ③ 的 login 兜底。
      3. 兜底：无论接口结果，只要页面 /api/config 验证 login:true（真实登录上了
         m.weibo.cn），就直接 success —— 防止接口失效时丢掉真实扫码结果。

    关键：不因页面 url 是 m.weibo.cn 就中断（那是未登录也存在的假路径），
    必须走接口轮询才能检测到用户扫码确认。
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
                "has_cookie": bool(ck.get("SUB")), "uid": sess.get("uid", ""),
                "username": sess.get("username", "")}

    # ② 接口轮询（主判断）
    retcode, msg, retdata = await _query_status(page, sess, qrid)
    log.info("扫码check qrid=%s retcode=%s msg=%r retdata=%s sess_status=%s",
             qrid[:22], retcode, msg, json.dumps(retdata, ensure_ascii=False)[:200] if retdata else "-",
             sess.get("status"))

    if retcode == 20000000:
        # 接口确认成功
        # 诊断：抓 passport 页面确认后的跳转线索（ticket/跨域回调 URL）
        try:
            hints = await page.evaluate("""() => {
                const out = {};
                const mr = document.querySelector('meta[http-equiv="refresh"]');
                out.metaRefresh = mr ? mr.getAttribute('content') : null;
                out.url = location.href;
                out.links = Array.from(document.querySelectorAll('a[href],script[src],iframe[src]'))
                    .map(e => e.getAttribute('href')||e.getAttribute('src')||'')
                    .filter(h => h && /passport|sso|crossdomain|ticket|login/i.test(h))
                    .slice(0, 15);
                out.bodySnippet = (document.body&&document.body.innerText||'').slice(0,300);
                out.docCookie = document.cookie.slice(0,200);
                // window 上的可能登录字段
                out.winKeys = Object.keys(window).filter(k => /ticket|sso|login|user|uid/i.test(k)).slice(0,20);
                return out;
            }""")
            log.info("确认后页面线索: %s", json.dumps(hints, ensure_ascii=False)[:500])
        except Exception as exc:
            log.warning("确认后页面提取: %s", exc)
        # 诊断2：打印所有域 cookie（含域名），看登录态在哪
        try:
            allck = await page.context.cookies()
            summ = [{"n": c["name"], "d": c["domain"], "len": len(c["value"])} for c in allck]
            log.info("确认后全域cookie: %s", json.dumps(summ)[:500])
        except Exception as exc:
            log.warning("确认后cookie dump: %s", exc)
        # ★ 首选：用 requests 完整走跨域链拿全部域有效 cookie（实测 login=true）
        #   浏览器 page.goto 只拿 passport 域半成品(api/config 仍 login=false)，故优先 requests。
        redir = _get_redirect_url(retdata)
        requests_cok = {}
        if redir:
            try:
                cctx = await _collect_cookies(page)
                requests_cok = await _complete_login_via_requests(redir, cctx)
                if _is_real_login(requests_cok):
                    log.info("requests跨域链拿到真实登录态: %s", sorted(requests_cok))
            except Exception as exc:
                log.warning("requests登录链异常: %s", exc)
        if _is_real_login(requests_cok):
            sess["status"] = "success"
            sess["cookies"] = requests_cok
            uid_req, sn_req = "", ""
            try:
                # 用 requests 拿 uid/账号名
                import requests as _requests
                S = _requests.Session()
                S.headers.update({"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 15_0 like Mac OS X)",
                                  "Referer": "https://m.weibo.cn/"})
                for k, v in requests_cok.items():
                    if v:
                        S.cookies.set(k, v, domain=".weibo.cn")
                cfg = S.get("https://m.weibo.cn/api/config", timeout=15).json()
                d = cfg.get("data") or {}
                uid_req = str(d.get("uid") or "")
                if uid_req:
                    try:
                        ui = S.get("https://m.weibo.cn/api/container/getIndex?type=uid&value=" + uid_req,
                                   timeout=15).json().get("data") or {}
                        sn_req = str((ui.get("userInfo") or {}).get("screen_name") or "")
                    except Exception:
                        pass
            except Exception as exc:
                log.warning("requests 补 uid/账号名: %s", exc)
            if uid_req:
                sess["uid"] = uid_req
            if sn_req:
                sess["username"] = sn_req
            return {"status": "success", "message": "扫码登录成功",
                    "cookies": requests_cok, "has_cookie": True,
                    "uid": uid_req or sess.get("uid", ""),
                    "username": sn_req or sess.get("username", "")}
        # 退化：浏览器路径导航（若 requests 失败）
        if redir:
            try:
                log.info("确认后浏览器导航回调: %s", redir[:120])
                try:
                    await page.evaluate("u => { location.replace(u); return true; }", redir)
                except Exception:
                    await page.goto(redir, timeout=20000, wait_until="domcontentloaded")
                try:
                    await page.wait_for_url("**m.weibo.cn**", timeout=15000,
                                            wait_until="domcontentloaded")
                except Exception:
                    pass
                await page.wait_for_timeout(3000)
            except Exception as exc:
                log.warning("主动导航回调失败: %s", exc)
        else:
            try:
                await page.wait_for_url(
                    "**m.weibo.cn**", timeout=12000, wait_until="domcontentloaded")
            except Exception:
                pass
        # ★ 关键：拿到 passport 域 cookie(SUB/SCF)后，必须再落到 m.weibo.cn
        #   触发 wap 域跨域 cookie(SSOLoginState/WEIBOCN_FROM/MLOGIN/_T_WM 等)落地，
        #   否则 m.weibo.cn api/config 仍 login=false（账号7只拿了passport域cookie）。
        try:
            if "m.weibo.cn" not in (page.url or ""):
                log.info("导航 m.weibo.cn 补全 m 域登录cookie")
                await page.goto("https://m.weibo.cn", timeout=20000,
                                wait_until="domcontentloaded")
                await page.wait_for_timeout(3500)
            else:
                await page.wait_for_timeout(2000)
        except Exception as exc:
            log.warning("导航 m.weibo.cn 补全失败: %s", exc)
        cookies, uid, logged_in, screen_name = await _finalize_with_uid(page, sess)
        log.info("确认finalize(20000000): logged_in=%s uid=%s cks=%s",
                 logged_in, uid, sorted(cookies.keys()) if cookies else "{}")
        if logged_in:
            sess["status"] = "success"
            return {"status": "success", "message": msg or "扫码登录成功",
                    "cookies": cookies, "has_cookie": True, "uid": uid, "username": screen_name}
        # 门户还没拿到真实登录态 → 让前端继续轮询
        sess["status"] = "scanned"
        return {"status": "scanned", "message": "已确认，正在获取登录态…"}

    if retcode == 50114002:
        sess["status"] = "scanned"
        return {"status": "scanned", "message": msg or "已扫码，请在手机上确认"}

    if retcode == 50114001:
        # passport 明确“未使用”（未扫码），无需 login 验证，直接 pending。
        sess["status"] = "pending"
        return {"status": "pending", "message": msg or "等待扫码"}

    if retcode in (50114003, 50114004):
        # 50114003(过期) / 50114004(该二维码已登录)。
        # 50114004 是【登录已成功但 cookie 未落地】的竞态结果：轮询错过了
        # 20000000（确认成功）瞬间，直接落到这里。此时 retdata 可能已无
        # redirect_url，必须主动尝试触发 passport 跨域回调落地 cookie。
        log.info("状态5011400x(%s) qrid=%s url=%s retdata=%s cks=%s",
                 retcode, qrid[:22], page.url[:80],
                 json.dumps(retdata, ensure_ascii=False)[:200] if retdata else "-",
                 [c["name"] for c in (await page.context.cookies())[:12]])
        # 诊断：抓取 passport 页面里可能的跳转线索（meta refresh / 链接 / script 里的 ticket）
        try:
            hints = await page.evaluate("""() => {
                const out = {};
                // meta refresh
                const mr = document.querySelector('meta[http-equiv="refresh"]');
                out.metaRefresh = mr ? mr.getAttribute('content') : null;
                // 所有 http(s) 链接
                out.links = Array.from(document.querySelectorAll('a[href],script[src],iframe[src]'))
                    .map(e => e.getAttribute('href')||e.getAttribute('src')||'')
                    .filter(h => h && (h.includes('passport')||h.includes('sso')||h.includes('crossdomain')||h.includes('ticket')))
                    .slice(0, 10);
                // body 文本里找 ticket= / redirect / crossdomain
                const txt = (document.body&&document.body.innerText||'').slice(0,500);
                out.bodySnippet = txt;
                out.docCookie = document.cookie.slice(0,200);
                return out;
            }""")
            log.info("50114004 页面线索: %s", json.dumps(hints, ensure_ascii=False)[:400])
        except Exception as exc:
            log.warning("50114004 页面提取: %s", exc)
        cookies, uid, logged_in, screen_name = await _finalize_with_uid(page, sess)
        if logged_in:
            sess["status"] = "success"
            return {"status": "success", "message": "扫码登录成功", "cookies": cookies,
                    "has_cookie": True, "uid": uid, "username": screen_name}
        sess["status"] = "pending"
        return {"status": "pending", "message": "等待扫码（二维码待刷新）…"}

    # retcode in (None, -1, 其他未知) → 接口查询失败（400/rid失效/风控）。用 login 兜底判断。
    cookies, uid, logged_in, screen_name = await _finalize_with_uid(page, sess)
    if logged_in:
        sess["status"] = "success"
        return {"status": "success", "message": "扫码登录成功", "cookies": cookies,
                "has_cookie": True, "uid": uid, "username": screen_name}
    sess["status"] = "pending"
    return {"status": "pending", "message": msg or "正在确认扫码状态…"}


def _get_redirect_url(retdata: dict) -> str:
    """从 qrcode/check 确认后的 retdata 提取 passport 跨域回调地址。

    passport 返回 {retcode,msg,data:{...}}，确认(20000000)后跳转 url 在
    data.data.url（passport 前端 JS 用 window.location.replace 它完成跨域
    登录 cookie 落地）。兼容退化结构：也查 retdata.url / redirect_url 顶层。
    """
    if not isinstance(retdata, dict):
        return ""
    # 嵌套: retdata.data.url (passport 标准)
    inner = retdata.get("data")
    if isinstance(inner, dict):
        for key in ("url", "redirect_url", "location"):
            v = inner.get(key)
            if isinstance(v, str) and v.startswith("http"):
                return v
    # 顶层退化
    for key in ("redirect_url", "url", "new_url", "return_url", "location"):
        v = retdata.get(key)
        if isinstance(v, str) and v.startswith("http"):
            return v
    return ""


async def _query_status(page, sess: dict, qrid: str) -> tuple:
    """调用 qrcode/check 接口查询状态；返回 (retcode, msg, retdata)。"""
    rid = sess.get("rid") or ""
    if not rid:
        try:
            rid = await _get_rid(page)
        except Exception:
            rid = ""
    if not rid or rid in ("getriderror", "nodetector"):
        return None, "正在确认扫码状态…", {}
    try:
        result = await page.evaluate(
            """([qrid, rid]) => fetch(
                '/sso/v2/qrcode/check?entry=wapsso&source=wapsso'
                + '&url=' + encodeURIComponent('https://m.weibo.cn')
                + '&qrid=' + encodeURIComponent(qrid)
                + '&rid=' + encodeURIComponent(rid)
                + '&ver=20250520',
                {credentials: 'include', headers: {'Accept': 'application/json'}}
            ).then(r => r.text()).then(t => {
                try {
                    const d = JSON.parse(t);
                    // passport 返回 {retcode, msg, data:{...}}，确认后跳转 url 在 data.data.url
                    const rd = d.data || null;
                    return {code: d.retcode, msg: d.msg, retdata: rd};
                } catch(e) { return {code: -1, msg: String(e), retdata: null}; }
            }).catch(e => ({code: -1, msg: String(e), retdata: null}))""",
            [qrid, rid],
        )
        return result.get("code"), result.get("msg") or "", result.get("retdata") or {}
    except Exception as exc:
        log.warning("扫码状态查询异常: %s", exc)
        return None, "正在确认扫码状态…", {}


async def _check_login(page) -> tuple:
    """在 m.weibo.cn 页面调 /api/config 验证是否真实登录。

    关键：m.weibo.cn 会给所有未登录访问者也 Set-Cookie 一个假 SUB，不能只看
    cookie 里有没有 SUB。必须调 /api/config，仅当 data.login=true 才算真正登录。
    返回 (logged_in, uid, screen_name)。
    """
    try:
        r = await page.evaluate(
            """() => fetch('/api/config', {credentials: 'include'})
                .then(r => r.text())
                .then(t => {
                    // 非 JSON（HTML/404/风控页）时安全降级，不抛 SyntaxError
                    if (!t || !t.trim().startsWith('{')) return {login:false, uid:'', name:''};
                    let d = JSON.parse(t);
                    const data = (d && d.data) || {};
                    let ui = data.userInfo || {};
                    let uid = (ui && ui.id) ? String(ui.id) : (data.uid ? String(data.uid) : '');
                    if (!uid && window.config && window.config.userInfo && window.config.userInfo.id) {
                        uid = String(window.config.userInfo.id);
                        ui = window.config.userInfo;
                    }
                    let name = (ui && ui.screen_name) ? String(ui.screen_name) : '';
                    // /api/config 无 screen_name；拿 uid 后调用户资料接口补账号名
                    if (!name && uid) {
                        try {
                            return fetch('/api/container/getIndex?type=uid&value=' + uid,
                                        {credentials: 'include'}).then(r => r.text()).then(ut => {
                                let nm = '';
                                if (ut && ut.trim().startsWith('{')) {
                                    const uj = JSON.parse(ut);
                                    const uu = ((uj && uj.data && uj.data.userInfo) || {});
                                    if (uu.screen_name) nm = String(uu.screen_name);
                                }
                                return {login: !!(data.login || uid), uid: uid, name: nm};
                            });
                        } catch(e) {
                            return {login: !!(data.login || uid), uid: uid, name: name};
                        }
                    }
                    return {login: !!(data.login || uid), uid: uid, name: name};
                }).catch(e => ({login:false, uid:'', name:''}))"""
        )
        return bool(r.get("login")), str(r.get("uid") or ""), str(r.get("name") or "")
    except Exception:
        return False, "", ""


def _is_real_login(cookies: dict) -> bool:
    """判断是否真实登录（区分假 SUB）。

    m.weibo.cn 会给所有未登录访问者也 Set-Cookie 一个孤零零的假 SUB，
    此时没有 SCF/SSOLoginState/ALF 等配套登录态。真实登录时这些同时存在。
    因此 SUB 不能单独作证，须 SUB + (SCF 或 SSOLoginState 或 ALF) 同时具备。
    """
    if not cookies.get("SUB"):
        return False
    return bool(cookies.get("SCF") or cookies.get("SSOLoginState")
                or cookies.get("ALF"))


async def _complete_login_via_requests(redir: str, ctx_cookies: dict) -> dict:
    """用 requests 完整走 passport 确认后的跨域登录链，收集所有域有效 cookie。

    passport 确认后返回 redir(passport.weibo.com/sso/v2/login?alt=...) ，
    用 requests 带 passport 页面 cookie 访问并 follow 全部 302，能收集到
    各域(.weibo.com/.weibo.cn/.sina.cn 等)的完整登录 cookie，含 SSOLoginState、
    WEIBOCN_FROM、MLOGIN 等 m.weibo.cn 域 cookie —— 这些是 api/config
    login=true 的关键。浏览器 page.goto 只拿 passport 域半成品，故改用 requests。
    """
    import requests as _requests
    UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 15_0 like Mac OS X) "
          "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 "
          "MicroMessenger/8.0.49")
    try:
        S = _requests.Session()
        S.headers.update({"User-Agent": UA, "Referer": "https://passport.weibo.com/"})
        for k, v in (ctx_cookies or {}).items():
            if v:
                S.cookies.set(k, v, domain=".weibo.com")
        r = S.get(redir, timeout=30, allow_redirects=True)
        # 收集各域 cookie；跨域同名以 weibo.cn 域为准（签到用该域）
        result = {}
        sink = []
        for c in S.cookies:
            nm = c.name
            dom = c.domain or ""
            if nm not in result:
                sink.append(nm)
            if nm not in result or "weibo.cn" in dom:
                result[nm] = c.value
        log.info("requests跨域链: final_url=%s collected=%s", r.url[:80], sorted(result))
        return result
    except Exception as exc:
        log.warning("requests 完整登录链失败: %s", exc)
        return {}


async def _finalize_with_uid(page, sess: dict):
    """页面已确认/跳转后抓取完整 Cookie 并验证真实登录。

    返回 (cookies, uid, logged_in, screen_name)。
    关键：登录成功那一刻 passport 的确认窗口很窄，且页面跳转有延迟。不能只靠
    _check_login 先跑一次（页面还在 passport 域时 /api/config 会 404 → login:false），
    必须【先抓完整 cookie】，以【真实登录标志】（SUB+SCF/SSOLoginState/ALF 并存）
    判定成功，并在有限时间内多轮重试等待跳转与登录态写入。
    """
    logged_in = False
    cookies = {}
    uid, screen_name = "", ""
    # 成功判定：cookie 有有效 SUB 且 /api/config 实测 login=true（权威），
    # 不强制要求 SCF/SSOLoginState（wapsso headless 下这些跨域 cookie 可能
    # 不落地，但 SUB 已足以签到）。
    for i in range(8):  # 最多 ~24s，覆盖跳转/写 cookie 延迟与被动回调失败场景
        try:
            cookies = await _finalize_cookies(page, allow_nav=True)
            if _is_real_login(cookies):
                logged_in = True
                break
        except Exception as exc:
            log.warning("finalize_with_uid 第 %d 轮: %s", i, exc)
        await page.wait_for_timeout(3000)
    if logged_in:
        # 1) 先尝试用页面拿 uid/账号名（此时页面已在 m.weibo.cn）
        try:
            lg, uid, screen_name = await _check_login(page)
        except Exception:
            pass
        # 2) 页面拿不到时，用已抓 cookie + requests 调微博接口补 uid/账号名（不依赖页面态）
        if (not uid or not screen_name) and cookies.get("SUB"):
            try:
                import requests as _requests
                S = _requests.Session()
                S.headers.update({"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 15_0 like Mac OS X)",
                                  "Referer": "https://m.weibo.cn/"})
                for k, v in cookies.items():
                    if v and k in ("SUB", "SUBP", "SCF", "ALF", "SSOLoginState",
                                   "X-CSRF-TOKEN", "MLOGIN"):
                        S.cookies.set(k, v, domain=".weibo.cn")
                cfg = S.get("https://m.weibo.cn/api/config", timeout=15).json()
                data = cfg.get("data") or {}
                if not uid and data.get("uid"):
                    uid = str(data.get("uid"))
                if (not screen_name) and uid:
                    try:
                        ui = S.get("https://m.weibo.cn/api/container/getIndex?type=uid&value="
                                   + str(uid), timeout=15).json().get("data") or {}
                        sn = (ui.get("userInfo") or {}).get("screen_name") or ""
                        if sn:
                            screen_name = str(sn)
                    except Exception:
                        pass
            except Exception as exc:
                log.warning("requests 补账号名失败: %s", exc)
    sess["cookies"] = cookies
    if uid:
        sess["uid"] = uid
    if screen_name:
        sess["username"] = screen_name
    return cookies, uid, logged_in, screen_name


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


async def _finalize_cookies(page, allow_nav: bool = True) -> dict:
    """扫码确认后获取完整登录 Cookie。

    按实际回调流程：扫码确认后，passport 会**自动**走跨域回调链并最终重定向到
    m.weibo.cn，登录态 cookie（SUB/SUBP/SSOLoginState 等）由这条回调链自动写入
    browser context。这里只需**等待页面自动跳转到 m.weibo.cn**，然后直接从
    context 读取全部 cookie —— 不再主动导航裸的 crossdomain（不带回调参数
    不会触发 Set-Cookie，反而会打断自动跳转）。

    allow_nav=False 时只抓当前 cookie（用于多轮重试的后续轮，避免反复 goto 导航打断页面）；
    否则在第一轮允许导航 m.weibo.cn 触发 wap 域跨域 cookie 落齐。
    """
    try:
        if allow_nav:
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
        # 3) 拿到 passport 域 SUB 后，必须再导航 m.weibo.cn 触发 m.weibo.cn 域
        #    登录 cookie(SSOLoginState/WEIBOCN_FROM/MLOGIN/_T_WM 等)落地 ——
        #    只有这些落地，m.weibo.cn /api/config 才 login=true（否则 cookie 无效）。
        #    只要已登录(SUB+SCF等) 且缺 m 域关键cookie，就反复导航 m.weibo.cn。
        m_keys = ("SSOLoginState", "WEIBOCN_FROM", "MLOGIN", "_T_WM", "XSRF-TOKEN")
        has_login = _is_real_login(cookies)
        lacks_m = not any(cookies.get(k) for k in m_keys)
        if has_login and lacks_m and allow_nav:
            for _try in range(3):
                try:
                    await page.goto("https://m.weibo.cn", timeout=20000,
                                    wait_until="domcontentloaded")
                    await page.wait_for_timeout(2500)
                    cookies = await _collect_cookies(page)
                    if any(cookies.get(k) for k in m_keys):
                        break
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
    if not _is_real_login(cookies):
        page = await _ensure_browser()
        try:
            cookies = await _finalize_cookies(page)
            sess["cookies"] = cookies
            if _is_real_login(cookies):
                sess["status"] = "success"
        except Exception as exc:
            log.warning("crossdomain 获取 cookie: %s", exc)

    if _is_real_login(cookies):
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
