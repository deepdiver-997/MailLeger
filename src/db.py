"""SQLite 持久化层（预写日志式崩溃恢复的存储基础）。

要点：
- WAL 模式 + 每步写操作立即提交：任一时刻崩溃，已提交状态均可恢复；
- scan_tasks：扫描任务预写日志（pending → processed）；
- processed_messages：Message-ID 去重表，保证通知不重复发送；
- outbox：待发送队列，dedup_key 唯一约束实现幂等入队；
- api_usage：大模型调用账本（调用前写预估预留，调用后回填实际消耗）。

命名说明：规格中的 `from` 列在本实现中命名为 `from_addr`（避免 SQL 关键字），
`date` 命名为 `email_date`，字段语义不变；另在 scan_tasks 上补充了
priority / reason 两列，便于每日汇总直接按优先级查询。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

from .config import AccountConfig

_SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    imap_host TEXT NOT NULL,
    imap_port INTEGER NOT NULL DEFAULT 993,
    imap_user TEXT NOT NULL,
    smtp_host TEXT,
    smtp_port INTEGER,
    smtp_user TEXT,
    notification_to TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scan_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL,
    email_uid TEXT NOT NULL,
    message_id TEXT,
    from_addr TEXT,
    subject TEXT,
    email_date TEXT,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'processed')),
    priority TEXT,
    reason TEXT,
    raw_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (account_id, email_uid)
);
CREATE INDEX IF NOT EXISTS idx_scan_tasks_status ON scan_tasks (account_id, status);

CREATE TABLE IF NOT EXISTS processed_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL,
    message_id TEXT NOT NULL,
    processed_at TEXT NOT NULL,
    UNIQUE (account_id, message_id)
);

CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL,
    task_id INTEGER,
    to_addr TEXT NOT NULL,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'sent', 'failed')),
    created_at TEXT NOT NULL,
    scheduled_time TEXT,
    sent_time TEXT,
    retry_count INTEGER NOT NULL DEFAULT 0,
    dedup_key TEXT UNIQUE
);
CREATE INDEX IF NOT EXISTS idx_outbox_status ON outbox (status);

CREATE TABLE IF NOT EXISTS api_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL,
    request_time TEXT NOT NULL,
    estimated_tokens INTEGER NOT NULL,
    actual_tokens INTEGER,
    model TEXT,
    task_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_api_usage_time ON api_usage (account_id, request_time);
"""


def now_str() -> str:
    """本地时间戳（入库统一格式，便于按天聚合）。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


class Database:
    """对所有表的读写封装。写操作内部立即提交，保证状态变更即时落盘。"""

    def __init__(self, db_path: str | Path) -> None:
        p = Path(db_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(p))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------------
    # accounts
    # ------------------------------------------------------------------
    def upsert_account(self, account: AccountConfig) -> int:
        """把配置文件中的账号快照写入 accounts 表，返回其 id。"""
        ts = now_str()
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO accounts
                    (name, imap_host, imap_port, imap_user,
                     smtp_host, smtp_port, smtp_user, notification_to,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    imap_host=excluded.imap_host,
                    imap_port=excluded.imap_port,
                    imap_user=excluded.imap_user,
                    smtp_host=excluded.smtp_host,
                    smtp_port=excluded.smtp_port,
                    smtp_user=excluded.smtp_user,
                    notification_to=excluded.notification_to,
                    updated_at=excluded.updated_at
                """,
                (account.name, account.imap_host, account.imap_port, account.imap_user,
                 account.smtp_host, account.smtp_port, account.smtp_user,
                 account.notification_to, ts, ts),
            )
        row = self.conn.execute("SELECT id FROM accounts WHERE name = ?", (account.name,)).fetchone()
        return int(row["id"])

    # ------------------------------------------------------------------
    # scan_tasks（预写日志）
    # ------------------------------------------------------------------
    def insert_scan_task(self, account_id: int, email_uid: str, message_id: str,
                         from_addr: str, subject: str, email_date: str,
                         raw_json: str) -> Optional[int]:
        """以 pending 状态预写扫描任务；同 UID 已存在（幂等）时返回 None。"""
        exists = self.conn.execute(
            "SELECT id FROM scan_tasks WHERE account_id=? AND email_uid=?",
            (account_id, email_uid),
        ).fetchone()
        if exists:
            return None
        ts = now_str()
        with self.conn:
            cur = self.conn.execute(
                """
                INSERT INTO scan_tasks
                    (account_id, email_uid, message_id, from_addr, subject,
                     email_date, status, raw_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
                """,
                (account_id, email_uid, message_id or None, (from_addr or "")[:320],
                 (subject or "")[:500], email_date or "", raw_json, ts, ts),
            )
        return int(cur.lastrowid)

    def get_pending_tasks(self, account_id: int) -> list[sqlite3.Row]:
        """所有待处理任务（含上次运行崩溃遗留的，用于启动恢复）。"""
        return self.conn.execute(
            "SELECT * FROM scan_tasks WHERE account_id=? AND status='pending' ORDER BY id",
            (account_id,),
        ).fetchall()

    def get_task(self, task_id: int) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM scan_tasks WHERE id=?", (task_id,)
        ).fetchone()

    def update_task_body(self, task_id: int, raw_json: str) -> None:
        """把已抓取的正文缓存进 raw_json，避免崩溃恢复后重复拉取。"""
        with self.conn:
            self.conn.execute(
                "UPDATE scan_tasks SET raw_json=?, updated_at=? WHERE id=?",
                (raw_json, now_str(), task_id),
            )

    def finish_task(self, task_id: int, priority: str, reason: str) -> None:
        """任务处理完成：pending → processed，同时记录优先级与原因。"""
        with self.conn:
            self.conn.execute(
                "UPDATE scan_tasks SET status='processed', priority=?, reason=?, updated_at=? WHERE id=?",
                (priority, reason, now_str(), task_id),
            )

    # ------------------------------------------------------------------
    # processed_messages（Message-ID 去重）
    # ------------------------------------------------------------------
    def is_message_processed(self, account_id: int, message_id: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM processed_messages WHERE account_id=? AND message_id=?",
            (account_id, message_id),
        ).fetchone() is not None

    def mark_message_processed(self, account_id: int, message_id: str) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO processed_messages (account_id, message_id, processed_at) "
                "VALUES (?, ?, ?)",
                (account_id, message_id, now_str()),
            )

    # ------------------------------------------------------------------
    # outbox（发送队列）
    # ------------------------------------------------------------------
    def enqueue_outbox(self, account_id: int, task_id: Optional[int], to_addr: str,
                       subject: str, body: str, scheduled_time: str,
                       dedup_key: str) -> Optional[int]:
        """入队待发邮件；dedup_key 已存在时返回 None（幂等，防止重复发送）。"""
        if self.conn.execute("SELECT 1 FROM outbox WHERE dedup_key=?", (dedup_key,)).fetchone():
            return None
        with self.conn:
            cur = self.conn.execute(
                """
                INSERT INTO outbox
                    (account_id, task_id, to_addr, subject, body, status,
                     created_at, scheduled_time, retry_count, dedup_key)
                VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, 0, ?)
                """,
                (account_id, task_id, to_addr, subject, body, now_str(), scheduled_time, dedup_key),
            )
        return int(cur.lastrowid)

    def due_outbox(self, account_id: int, max_retries: int) -> list[sqlite3.Row]:
        """到期且未超过重试上限的待发邮件（含失败重试）。"""
        return self.conn.execute(
            """
            SELECT * FROM outbox
            WHERE account_id=? AND status IN ('pending', 'failed') AND retry_count < ?
              AND (scheduled_time IS NULL OR scheduled_time <= ?)
            ORDER BY id
            """,
            (account_id, max_retries, now_str()),
        ).fetchall()

    def mark_outbox_sent(self, outbox_id: int) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE outbox SET status='sent', sent_time=? WHERE id=?",
                (now_str(), outbox_id),
            )

    def mark_outbox_failed(self, outbox_id: int) -> None:
        """发送失败：保留任务并累计重试次数，下次运行继续尝试。"""
        with self.conn:
            self.conn.execute(
                "UPDATE outbox SET status='failed', retry_count=retry_count+1 WHERE id=?",
                (outbox_id,),
            )

    def summary_enqueued(self, dedup_key: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM outbox WHERE dedup_key=?", (dedup_key,)
        ).fetchone() is not None

    # ------------------------------------------------------------------
    # api_usage（成本控制）
    # ------------------------------------------------------------------
    def record_api_usage(self, account_id: int, task_id: Optional[int],
                         estimated_tokens: int, model: str) -> int:
        """调用前写入预估 token（预留配额），返回该记录 id。"""
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO api_usage (account_id, request_time, estimated_tokens, model, task_id) "
                "VALUES (?, ?, ?, ?, ?)",
                (account_id, now_str(), max(1, estimated_tokens), model, task_id),
            )
        return int(cur.lastrowid)

    def update_api_usage_actual(self, usage_id: int, actual_tokens: Optional[int]) -> None:
        if actual_tokens is None:
            return
        with self.conn:
            self.conn.execute(
                "UPDATE api_usage SET actual_tokens=? WHERE id=?", (actual_tokens, usage_id)
            )

    def today_usage(self, account_id: int) -> tuple[int, int]:
        """返回（当天调用次数, 当天累计“预留”token），用于调用前配额判断。"""
        row = self.conn.execute(
            """
            SELECT COUNT(*) AS calls, COALESCE(SUM(estimated_tokens), 0) AS tokens
            FROM api_usage
            WHERE account_id=? AND date(request_time) = date('now', 'localtime')
            """,
            (account_id,),
        ).fetchone()
        return int(row["calls"]), int(row["tokens"])

    # ------------------------------------------------------------------
    # 每日汇总查询
    # ------------------------------------------------------------------
    def today_low_priority(self, account_id: int) -> list[sqlite3.Row]:
        """当天已处理且优先级为 low 的邮件，按时间排序供汇总。"""
        return self.conn.execute(
            """
            SELECT * FROM scan_tasks
            WHERE account_id=? AND status='processed' AND priority='low'
              AND date(updated_at) = date('now', 'localtime')
            ORDER BY updated_at
            """,
            (account_id,),
        ).fetchall()
