"""
微博超话签到管理面板 - 数据库层
使用 SQLite 单文件数据库。
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path

# 数据库文件默认放在项目根目录下
DB_PATH = Path(__file__).resolve().parent.parent / "data" / "weibo_checkin.db"

log = logging.getLogger("weibo.database")

_local = threading.local()


def _get_conn() -> sqlite3.Connection:
    """获取当前线程的数据库连接（线程本地）。"""
    conn = getattr(_local, "conn", None)
    if conn is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        _local.conn = conn
    return conn


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def init_db() -> None:
    """初始化数据库表结构。"""
    conn = _get_conn()
    conn.executescript(
        """
        -- 全局设置（键值对）
        CREATE TABLE IF NOT EXISTS settings (
            key        TEXT PRIMARY KEY,
            value      TEXT NOT NULL
        );

        -- 微博账号
        CREATE TABLE IF NOT EXISTS accounts (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            name         TEXT NOT NULL DEFAULT '未命名账号',
            cookie       TEXT NOT NULL DEFAULT '',
            cookie_raw   TEXT NOT NULL DEFAULT '',   -- 原始 cookie 字符串（可选）
            enabled      INTEGER NOT NULL DEFAULT 1,  -- 是否启用自动签到
            proxy        TEXT NOT NULL DEFAULT '',     -- 该账号使用的 socks 链接（归属地由 proxy_geo 识别）
            proxy_index  INTEGER NOT NULL DEFAULT 0,  -- 兼容旧字段（已弃用，改用 proxy）
            remark       TEXT NOT NULL DEFAULT '',
            last_status  TEXT NOT NULL DEFAULT 'unknown', -- success/failed/partial/unknown
            last_checkin TEXT,                         -- 上次签到时间
            last_message TEXT NOT NULL DEFAULT '',
            created_at   TEXT NOT NULL,
            updated_at   TEXT NOT NULL
        );

        -- 签到日志
        CREATE TABLE IF NOT EXISTS checkin_logs (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id    INTEGER,
            account_name  TEXT NOT NULL,
            task_id       TEXT NOT NULL DEFAULT '',
            status        TEXT NOT NULL,               -- success/partial/failed
            channel       TEXT NOT NULL DEFAULT 'direct',
            total         INTEGER NOT NULL DEFAULT 0,
            success       INTEGER NOT NULL DEFAULT 0,
            fail          INTEGER NOT NULL DEFAULT 0,
            detail        TEXT NOT NULL DEFAULT '',     -- JSON 明细
            message       TEXT NOT NULL DEFAULT '',
            created_at    TEXT NOT NULL,
            FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE SET NULL
        );

        -- 执行任务（每次手动或定时触发的运行）
        CREATE TABLE IF NOT EXISTS tasks (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id       TEXT NOT NULL UNIQUE,       -- UUID
            trigger_type  TEXT NOT NULL DEFAULT 'manual', -- manual/schedule
            status        TEXT NOT NULL DEFAULT 'running', -- running/success/partial/failed
            started_at    TEXT NOT NULL,
            finished_at   TEXT,
            summary       TEXT NOT NULL DEFAULT ''
        );

        -- 通知记录
        CREATE TABLE IF NOT EXISTS notifications (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            channel    TEXT NOT NULL DEFAULT 'telegram',
            title      TEXT NOT NULL DEFAULT '',
            body       TEXT NOT NULL DEFAULT '',
            success    INTEGER NOT NULL DEFAULT 0,
            error      TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );

        -- 用户（登录）
        CREATE TABLE IF NOT EXISTS users (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            username      TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            created_at    TEXT NOT NULL
        );

        -- SOCKS5 代理节点（归属地自动识别）
        CREATE TABLE IF NOT EXISTS proxies (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            label        TEXT NOT NULL DEFAULT '',
            ip           TEXT NOT NULL DEFAULT '',
            port         INTEGER NOT NULL DEFAULT 0,
            username     TEXT NOT NULL DEFAULT '',
            password     TEXT NOT NULL DEFAULT '',
            url          TEXT NOT NULL DEFAULT '',    -- 完整 socks5:// 链接
            geo_country   TEXT NOT NULL DEFAULT '',
            geo_region    TEXT NOT NULL DEFAULT '',
            geo_country_code TEXT NOT NULL DEFAULT '',
            geo_ip        TEXT NOT NULL DEFAULT '',
            enabled      INTEGER NOT NULL DEFAULT 1,
            remark       TEXT NOT NULL DEFAULT '',
            last_test     TEXT NOT NULL DEFAULT '',     -- 最近测试结果(ok/fail)
            created_at   TEXT NOT NULL,
            updated_at   TEXT NOT NULL
        );

        -- 登录会话（token 存库，便于登出/校验）
        CREATE TABLE IF NOT EXISTS sessions (
            token      TEXT PRIMARY KEY,
            user_id    INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_logs_account ON checkin_logs(account_id);
        CREATE INDEX IF NOT EXISTS idx_logs_time   ON checkin_logs(created_at);
        """
    )
    conn.commit()
    _migrate(conn)
    _seed_defaults(conn)


def _migrate(conn: sqlite3.Connection) -> None:
    """兼容旧数据库的增量迁移。"""
    account_cols = [r["name"] for r in conn.execute("PRAGMA table_info(accounts)").fetchall()]
    if "proxy" not in account_cols:
        conn.execute("ALTER TABLE accounts ADD COLUMN proxy TEXT NOT NULL DEFAULT ''")
        log.info("accounts 表已迁移：新增 proxy 列")

    # 代理测试结果需要持久显示，避免前端刷新后延迟/结果消失。
    proxy_cols = [r["name"] for r in conn.execute("PRAGMA table_info(proxies)").fetchall()]
    additions = {
        "last_latency_ms": "INTEGER NOT NULL DEFAULT 0",
        "last_test_at": "TEXT NOT NULL DEFAULT ''",
        "last_test_message": "TEXT NOT NULL DEFAULT ''",
    }
    for name, definition in additions.items():
        if name not in proxy_cols:
            conn.execute(f"ALTER TABLE proxies ADD COLUMN {name} {definition}")
            log.info("proxies 表已迁移：新增 %s", name)
    conn.commit()


def _seed_defaults(conn: sqlite3.Connection) -> None:
    """写入默认设置。"""
    defaults = {
        "tg_bot_token": "",
        "tg_user_id": "",
        "tg_enabled": "0",
        "schedule_enabled": "1",       # 是否启用自动定时签到
        "schedule_cron": "0 7 * * *",  # 每天 7 点
        "anti_ban_enabled": "1",       # 防封策略总开关
        "anti_ban_wait_min": "120",    # 账号间最小随机等待（秒）
        "anti_ban_wait_max": "300",    # 账号间最大随机等待（秒）
        "anti_ban_window_hour": "7",   # 凌晨 N 点前启用等待
        "proxies": "",                # SOCKS5 节点，一行一个
        "proxy_force": "0",           # 严格代理（失败不回退直连）
        "proxy_fallback": "1",        # 允许失败回退直连
        "checkin_delay_min": "3",     # 超话间随机延时（秒）
        "checkin_delay_max": "8",
        "cookie_valid": "0",          # 最近一次 Cookie 是否校验过（占位）
        "auth_enabled": "1",          # 是否启用登录保护
        "admin_initialized": "0",     # 默认管理员是否已初始化
        # ---- v7.0 新增 ----
        "tg_quote_enabled": "1",      # TG 推送附带每日一言
        "tg_only_on_change": "0",     # 仅在有失败/异常时推送
        "tg_silent": "0",             # 静默推送（不震动提示）
        "log_retention_days": "30",   # 日志保留天数（0=不清理）
    }
    for key, value in defaults.items():
        conn.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
            (key, value),
        )
    conn.commit()


# ========================= 设置 =========================

def get_setting(key: str, default: str = "") -> str:
    row = _get_conn().execute(
        "SELECT value FROM settings WHERE key = ?", (key,)
    ).fetchone()
    return row["value"] if row else default


def get_settings() -> dict:
    rows = _get_conn().execute("SELECT key, value FROM settings").fetchall()
    return {r["key"]: r["value"] for r in rows}


def set_settings(values: dict) -> None:
    conn = _get_conn()
    for key, value in values.items():
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )
    conn.commit()


# ========================= 账号 =========================

def add_proxy(data: dict) -> int:
    conn = _get_conn()
    now = _now()
    cur = conn.execute(
        """
        INSERT INTO proxies
            (label, ip, port, username, password, url, geo_country, geo_region,
             geo_country_code, geo_ip, enabled, remark, last_test, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (data.get("label", ""), data.get("ip", ""), int(data.get("port", 0) or 0),
         data.get("username", ""), data.get("password", ""), data.get("url", ""),
         data.get("geo_country", ""), data.get("geo_region", ""),
         data.get("geo_country_code", ""), data.get("geo_ip", ""),
         1 if data.get("enabled", True) else 0, data.get("remark", ""),
         data.get("last_test", ""), now, now),
    )
    conn.commit()
    return cur.lastrowid


def update_proxy(proxy_id: int, data: dict) -> bool:
    conn = _get_conn()
    allowed = ("label", "ip", "port", "username", "password", "url",
               "geo_country", "geo_region", "geo_country_code", "geo_ip",
               "enabled", "remark", "last_test", "last_latency_ms",
               "last_test_at", "last_test_message")
    fields = {k: v for k, v in data.items() if k in allowed}
    if "enabled" in fields:
        fields["enabled"] = 1 if fields["enabled"] else 0
    if not fields:
        return False
    sets = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values())
    values.append(_now())
    values.append(proxy_id)
    conn.execute(f"UPDATE proxies SET {sets}, updated_at = ? WHERE id = ?", values)
    conn.commit()
    return True


def delete_proxy(proxy_id: int) -> bool:
    conn = _get_conn()
    cur = conn.execute("DELETE FROM proxies WHERE id = ?", (proxy_id,))
    conn.commit()
    return cur.rowcount > 0


def get_proxy(proxy_id: int) -> dict | None:
    row = _get_conn().execute("SELECT * FROM proxies WHERE id = ?", (proxy_id,)).fetchone()
    return dict(row) if row else None


def get_proxies(include_disabled: bool = True) -> list[dict]:
    if include_disabled:
        rows = _get_conn().execute("SELECT * FROM proxies ORDER BY id").fetchall()
    else:
        rows = _get_conn().execute(
            "SELECT * FROM proxies WHERE enabled = 1 ORDER BY id").fetchall()
    return [dict(r) for r in rows]


def mask_proxy_url(url: str) -> str:
    """把 socks5://user:pass@host:port 打码成 socks5://***@host:port（v7.1）。

    面板/API 只应看到打码结果；真实凭据只在服务端发起请求时使用。
    """
    s = (url or "").strip()
    if not s:
        return ""
    if "@" not in s:
        return s
    head, _, tail = s.partition("://")
    if not tail:
        return "***@" + s.split("@", 1)[-1]
    return f"{head}://***@" + tail.split("@", 1)[-1]


def find_proxy_by_url(url: str) -> dict | None:
    """按完整链接反查代理记录（用于账号 proxy 字段 → 代理 id 映射）。"""
    s = (url or "").strip()
    if not s:
        return None
    for p in get_proxies(include_disabled=True):
        full = p.get("url") or build_proxy_url(p)
        if full and full == s:
            return p
    # 退化匹配：host:port 相同即认为同一节点
    tail = s.split("@", 1)[-1]
    for p in get_proxies(include_disabled=True):
        if p.get("ip") and f"{p['ip']}:{p.get('port')}" == tail:
            return p
    return None


def proxy_display_label(p: dict) -> str:
    """代理的人类可读名称（不含凭据）。"""
    if not p:
        return ""
    if p.get("label"):
        return str(p["label"])
    if p.get("geo_country"):
        return f"{p['geo_country']} {p.get('geo_region') or ''}".strip()
    if p.get("ip"):
        return f"{p['ip']}:{p.get('port')}"
    return "代理节点"


def build_proxy_url(p: dict) -> str:
    """由字段拼接 socks5 链接。"""
    ip, port = p.get("ip", ""), p.get("port", 0)
    if not ip or not port:
        return p.get("url", "") or ""
    user = p.get("username", "")
    pwd = p.get("password", "")
    auth = f"{user}:{pwd}@" if user else ""
    return f"socks5://{auth}{ip}:{port}"


def add_account(data: dict) -> int:
    conn = _get_conn()
    now = _now()
    cur = conn.execute(
        """
        INSERT INTO accounts
            (name, cookie, cookie_raw, enabled, proxy, remark,
             last_status, last_message, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, 'unknown', '', ?, ?)
        """,
        (
            data.get("name", "未命名账号"),
            data.get("cookie", ""),
            data.get("cookie_raw", ""),
            1 if data.get("enabled", True) else 0,
            data.get("proxy", ""),   # socks 链接或标识
            data.get("remark", ""),
            now, now,
        ),
    )
    conn.commit()
    return cur.lastrowid


def update_account(account_id: int, data: dict) -> bool:
    conn = _get_conn()
    allowed = ("name", "cookie", "cookie_raw", "enabled", "proxy", "proxy_index", "remark")
    fields = {k: v for k, v in data.items() if k in allowed}
    if not fields:
        return False
    # 兼容：旧字段 proxy_index 映射到 proxy 非空时才用
    if "proxy_index" in fields and "proxy" not in fields:
        pi = fields.pop("proxy_index")
        if pi:
            fields["proxy"] = str(pi)
    sets = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values())
    values.append(_now())
    values.append(account_id)
    conn.execute(
        f"UPDATE accounts SET {sets}, updated_at=? WHERE id=?",
        values,
    )
    conn.commit()
    return True


def delete_account(account_id: int) -> bool:
    conn = _get_conn()
    cur = conn.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
    conn.commit()
    return cur.rowcount > 0


def get_account(account_id: int) -> dict | None:
    row = _get_conn().execute(
        "SELECT * FROM accounts WHERE id = ?", (account_id,)
    ).fetchone()
    return dict(row) if row else None


def get_accounts() -> list[dict]:
    rows = _get_conn().execute(
        "SELECT * FROM accounts ORDER BY id"
    ).fetchall()
    return [dict(r) for r in rows]


def get_enabled_accounts() -> list[dict]:
    rows = _get_conn().execute(
        "SELECT * FROM accounts WHERE enabled = 1 ORDER BY id"
    ).fetchall()
    return [dict(r) for r in rows]


def touch_account_result(account_id: int, status: str, message: str) -> None:
    conn = _get_conn()
    conn.execute(
        """
        UPDATE accounts
        SET last_status = ?, last_message = ?, last_checkin = ?, updated_at = ?
        WHERE id = ?
        """,
        (status, message, _now(), _now(), account_id),
    )
    conn.commit()


# ========================= 日志 =========================

def add_log(entry: dict) -> int:
    conn = _get_conn()
    cur = conn.execute(
        """
        INSERT INTO checkin_logs
            (account_id, account_name, task_id, status, channel,
             total, success, fail, detail, message, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            entry.get("account_id"),
            entry.get("account_name", ""),
            entry.get("task_id", ""),
            entry.get("status", "failed"),
            entry.get("channel", "direct"),
            int(entry.get("total", 0)),
            int(entry.get("success", 0)),
            int(entry.get("fail", 0)),
            json.dumps(entry.get("detail", []), ensure_ascii=False),
            entry.get("message", ""),
            _now(),
        ),
    )
    conn.commit()
    return cur.lastrowid


def _safe_channel(value: str) -> str:
    """读取层脱敏：旧日志可能存了完整代理 URL（含用户名密码），
    统一折叠为“SOCKS5 代理 / 直连”，避免历史数据在面板泄露凭据。"""
    s = (value or "").strip()
    if not s:
        return ""
    low = s.lower()
    if "socks" in low:
        return "SOCKS5 代理"
    if "direct" in low or "直连" in s:
        return "直连"
    if "@" in s:  # 其他带认证信息的形式，一律不展示原值
        return "代理"
    return s


def _row_to_log(row) -> dict:
    d = dict(row)
    try:
        d["detail"] = json.loads(d["detail"]) if d["detail"] else []
    except json.JSONDecodeError:
        d["detail"] = []
    d["channel"] = _safe_channel(d.get("channel", ""))
    return d


def get_logs(limit: int = 50, account_id: int | None = None) -> list[dict]:
    conn = _get_conn()
    if account_id is not None:
        rows = conn.execute(
            "SELECT * FROM checkin_logs WHERE account_id = ? "
            "ORDER BY id DESC LIMIT ?",
            (account_id, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM checkin_logs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [_row_to_log(r) for r in rows]


def get_log_stats() -> dict:
    conn = _get_conn()
    total = conn.execute("SELECT COUNT(*) c FROM checkin_logs").fetchone()["c"]
    success = conn.execute(
        "SELECT COUNT(*) c FROM checkin_logs WHERE status='success'"
    ).fetchone()["c"]
    fail = conn.execute(
        "SELECT COUNT(*) c FROM checkin_logs WHERE status='failed'"
    ).fetchone()["c"]
    partial = conn.execute(
        "SELECT COUNT(*) c FROM checkin_logs WHERE status='partial'"
    ).fetchone()["c"]
    today = datetime.now().strftime("%Y-%m-%d")
    today_count = conn.execute(
        "SELECT COUNT(*) c FROM checkin_logs WHERE created_at LIKE ?",
        (today + "%",),
    ).fetchone()["c"]
    topics_signed = conn.execute(
        "SELECT COALESCE(SUM(success),0) s FROM checkin_logs"
    ).fetchone()["s"]
    last_row = conn.execute(
        "SELECT created_at FROM checkin_logs ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return {
        "total": total,
        "success": success,
        "partial": partial,
        "fail": fail,
        "today": today_count,
        "topics_signed": topics_signed,
        "last_log_at": last_row["created_at"] if last_row else "",
        "success_rate": round(success / total * 100) if total else 0,
    }


def get_daily_trend(days: int = 7) -> list[dict]:
    """近 N 天每天签到统计（用于仪表盘趋势图）。"""
    conn = _get_conn()
    rows = conn.execute(
        """
        SELECT substr(created_at, 1, 10) AS day,
               COUNT(*) AS runs,
               COALESCE(SUM(success), 0) AS success,
               COALESCE(SUM(fail), 0) AS fail
        FROM checkin_logs
        WHERE created_at >= ?
        GROUP BY day ORDER BY day
        """,
        ((datetime.now() - timedelta(days=days - 1)).strftime("%Y-%m-%d"),),
    ).fetchall()
    by_day = {r["day"]: dict(r) for r in rows}
    result = []
    for i in range(days - 1, -1, -1):
        day = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
        item = by_day.get(day) or {"day": day, "runs": 0, "success": 0, "fail": 0}
        result.append({
            "day": day,
            "label": day[5:],
            "runs": item["runs"],
            "success": item["success"],
            "fail": item["fail"],
        })
    return result


def purge_old_logs(days: int) -> int:
    """删除 N 天前的日志，返回删除条数。days<=0 时不做任何事。"""
    if days <= 0:
        return 0
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d 00:00:00")
    conn = _get_conn()
    cur = conn.execute("DELETE FROM checkin_logs WHERE created_at < ?", (cutoff,))
    conn.commit()
    return cur.rowcount or 0


def clear_logs() -> int:
    """清空全部签到日志，返回删除条数。"""
    conn = _get_conn()
    count = conn.execute("SELECT COUNT(*) c FROM checkin_logs").fetchone()["c"]
    conn.execute("DELETE FROM checkin_logs")
    conn.commit()
    return count


def get_logs_grouped(limit: int = 20) -> list[dict]:
    """获取日志，按执行日期分组；同一次 task 的所有账号归并到一组。
    返回结构：
    [
      {
        "date": "2026-08-14",
        "groups": [   # 同一天内的多次执行
          {
            "task_id": "...", "trigger_type": "...",
            "started_at": "...", "accounts": [...],
            "total": N, "success": N, "fail": N, "status": "..."
          }
        ]
      }
    ]
    """
    conn = _get_conn()
    # 取最近的日志（按 id 倒序），再倒回正向拼装
    rows = conn.execute(
        "SELECT * FROM checkin_logs ORDER BY id DESC LIMIT ?", (limit * 5,)
    ).fetchall()
    entries = []
    for r in reversed(rows):  # 变回时间正序
        entries.append(_row_to_log(r))

    # 按 task_id 归组
    tasks = {}
    task_order = []
    for e in entries:
        tid = e.get("task_id") or f"manual-{e['id']}"
        if tid not in tasks:
            tasks[tid] = {
                "task_id": tid,
                "trigger_type": "",
                "created_at": e.get("created_at", ""),
                "accounts": [],
                "total": 0, "success": 0, "fail": 0,
            }
            task_order.append(tid)
        g = tasks[tid]
        g["accounts"].append(e)
        g["total"] += e.get("total", 0)
        g["success"] += e.get("success", 0)
        g["fail"] += e.get("fail", 0)
        if not g["trigger_type"] and e.get("message"):
            pass

    # 按日期分组
    by_date = {}
    date_order = []
    for tid in task_order:
        g = tasks[tid]
        date = (g["created_at"] or "")[:10] or "未知"
        if date not in by_date:
            by_date[date] = []
            date_order.append(date)
        # 组状态：任一 failed → failed；任一 partial → partial；否则 success
        st = "success"
        for a in g["accounts"]:
            if a.get("status") == "failed": st = "failed"; break
            if a.get("status") == "partial": st = "partial"
        g["status"] = st
        by_date[date].append(g)

    result = []
    for date in reversed(date_order):  # 最新日期在前
        result.append({"date": date, "groups": by_date[date]})
    return result


# ========================= 任务 =========================

def create_task(task_id: str, trigger_type: str = "manual") -> None:
    conn = _get_conn()
    conn.execute(
        "INSERT INTO tasks (task_id, trigger_type, status, started_at) VALUES (?, ?, 'running', ?)",
        (task_id, trigger_type, _now()),
    )
    conn.commit()


def finish_task(task_id: str, status: str, summary: str) -> None:
    conn = _get_conn()
    conn.execute(
        "UPDATE tasks SET status = ?, finished_at = ?, summary = ? WHERE task_id = ?",
        (status, _now(), summary, task_id),
    )
    conn.commit()


def get_tasks(limit: int = 20) -> list[dict]:
    rows = _get_conn().execute(
        "SELECT * FROM tasks ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


# ========================= 通知记录 =========================

def add_notification(title: str, body: str, success: bool, error: str = "") -> None:
    conn = _get_conn()
    conn.execute(
        "INSERT INTO notifications (title, body, success, error, created_at) VALUES (?, ?, ?, ?, ?)",
        (title, body, 1 if success else 0, error, _now()),
    )
    conn.commit()


def get_notifications(limit: int = 10) -> list[dict]:
    rows = _get_conn().execute(
        "SELECT * FROM notifications ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


# ========================= 用户 & 会话 =========================

def create_user(username: str, password_hash: str) -> int:
    conn = _get_conn()
    cur = conn.execute(
        "INSERT OR IGNORE INTO users (username, password_hash, created_at) VALUES (?, ?, ?)",
        (username, password_hash, _now()),
    )
    conn.commit()
    return cur.lastrowid


def update_user_password(user_id: int, password_hash: str) -> None:
    conn = _get_conn()
    conn.execute(
        "UPDATE users SET password_hash = ? WHERE id = ?",
        (password_hash, user_id),
    )
    conn.commit()


def get_user_by_name(username: str) -> dict | None:
    row = _get_conn().execute(
        "SELECT * FROM users WHERE username = ?", (username,)
    ).fetchone()
    return dict(row) if row else None


def get_user(user_id: int) -> dict | None:
    row = _get_conn().execute(
        "SELECT * FROM users WHERE id = ?", (user_id,)
    ).fetchone()
    return dict(row) if row else None


def count_users() -> int:
    return _get_conn().execute("SELECT COUNT(*) c FROM users").fetchone()["c"]


def create_session(token: str, user_id: int, ttl_hours: int = 168) -> None:
    """创建会话，默认 7 天有效。"""
    from datetime import timedelta

    conn = _get_conn()
    expires = (
        datetime.now() + timedelta(hours=ttl_hours)
    ).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "INSERT INTO sessions (token, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
        (token, user_id, _now(), expires),
    )
    conn.commit()


def get_session_user(token: str) -> dict | None:
    """根据 token 返回用户（校验有效期）。"""
    if not token:
        return None
    conn = _get_conn()
    row = conn.execute(
        "SELECT s.user_id, s.expires_at FROM sessions s WHERE s.token = ?", (token,)
    ).fetchone()
    if not row:
        return None
    if row["expires_at"] < _now():
        conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
        conn.commit()
        return None
    # 清理过期会话
    conn.execute("DELETE FROM sessions WHERE expires_at < ?", (_now(),))
    conn.commit()
    return get_user(row["user_id"])


def delete_session(token: str) -> None:
    conn = _get_conn()
    conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
    conn.commit()


def delete_user_sessions(user_id: int) -> None:
    conn = _get_conn()
    conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
    conn.commit()
