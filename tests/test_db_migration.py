"""旧版本数据库（无 attempts / raw_path 列）打开时自动补列，且老数据可继续使用。"""
import sqlite3
import tempfile
import unittest
from pathlib import Path

from src.db import Database

_OLD_SCHEMA = """
CREATE TABLE scan_tasks (
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
CREATE TABLE outbox (
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
INSERT INTO scan_tasks (account_id, email_uid, message_id, status, created_at, updated_at)
    VALUES (1, '7', '<old@x>', 'pending', '2026-01-01 00:00:00', '2026-01-01 00:00:00');
INSERT INTO outbox (account_id, to_addr, subject, body, status, created_at, dedup_key)
    VALUES (1, 'to@x', 's', 'b', 'pending', '2026-01-01 00:00:00', 'notify:old');
"""


class TestDbMigration(unittest.TestCase):
    def test_old_schema_gets_new_columns_and_keeps_rows(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "old.db"
            conn = sqlite3.connect(path)
            conn.executescript(_OLD_SCHEMA)
            conn.commit()
            conn.close()

            db = Database(path)
            try:
                cols = {r["name"] for r in db.conn.execute("PRAGMA table_info(scan_tasks)")}
                self.assertIn("attempts", cols)
                cols = {r["name"] for r in db.conn.execute("PRAGMA table_info(outbox)")}
                self.assertIn("raw_path", cols)

                task = db.get_pending_tasks(1)[0]
                self.assertEqual(task["attempts"], 0)
                self.assertEqual(db.increment_task_attempts(task["id"]), 1)

                rows = db.due_outbox(1, 50)
                self.assertEqual(len(rows), 1)
                self.assertIsNone(rows[0]["raw_path"])            # 老数据默认 NULL → 走 send_html
                db.enqueue_outbox(1, None, "to@x", "s", "", "2026-01-01 00:00:00", "cmd:new", raw_path="/tmp/a.eml")
                self.assertEqual(len(db.due_outbox(1, 50)), 2)

                # 再次打开幂等，不会因为列已存在而报错
                db.close()
                Database(path).close()
            finally:
                try:
                    db.close()
                except Exception:
                    pass


if __name__ == "__main__":
    unittest.main()
