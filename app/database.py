"""
微博超话签到管理面板 - 数据库层
使用 SQLite 单文件数据库。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

# 数据库文件默认放在项目根目录下
DB_PATH = Path(__file__).resolve().parent.parent / "data" / "weibo_checkin.db"

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
            proxy_index  INTEGER NOT NULL DEFAULT 0,  -- 使用的代理节点序号
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
    _seed_defaults(conn)


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

def add_account(data: dict) -> int:
    conn = _get_conn()
    now = _now()
    cur = conn.execute(
        """
        INSERT INTO accounts
            (name, cookie, cookie_raw, enabled, proxy_index, remark,
             last_status, last_message, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, 'unknown', '', ?, ?)
        """,
        (
            data.get("name", "未命名账号"),
            data.get("cookie", ""),
            data.get("cookie_raw", ""),
            1 if data.get("enabled", True) else 0,
            int(data.get("proxy_index", 0)),
            data.get("remark", ""),
            now, now,
        ),
    )
    conn.commit()
    return cur.lastrowid


def update_account(account_id: int, data: dict) -> bool:
    conn = _get_conn()
    allowed = ("name", "cookie", "cookie_raw", "enabled", "proxy_index", "remark")
    fields = {k: v for k, v in data.items() if k in allowed}
    if not fields:
        return False
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
    result = []
    for r in rows:
        d = dict(r)
        try:
            d["detail"] = json.loads(d["detail"]) if d["detail"] else []
        except json.JSONDecodeError:
            d["detail"] = []
        result.append(d)
    return result


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
    return {
        "total": total,
        "success": success,
        "partial": partial,
        "fail": fail,
        "today": today_count,
    }


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
        d = dict(r)
        try:
            d["detail"] = json.loads(d["detail"]) if d["detail"] else []
        except json.JSONDecodeError:
            d["detail"] = []
        entries.append(d)

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
