"""SQLite 持久层的幂等语义（崩溃恢复/去重的存储基础）。"""
import tempfile
import unittest
from pathlib import Path

from src.config import AccountConfig
from src.db import Database


def _account() -> AccountConfig:
    return AccountConfig(
        name="main",
        imap_host="imap.example.com",
        imap_user="u@example.com",
        smtp_host="smtp.example.com",
        smtp_user="u@example.com",
        notification_to="to@example.com",
    )


class TestDbIdempotency(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self._tmp.name) / "t.db")
        self.account_id = self.db.upsert_account(_account())

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()

    def test_scan_task_same_uid_insert_is_idempotent(self) -> None:
        first = self.db.insert_scan_task(
            self.account_id, "123", "msg-a", "a@x.com", "s", "2026-09-05 10:00:00", "{}")
        second = self.db.insert_scan_task(
            self.account_id, "123", "msg-a", "a@x.com", "s", "2026-09-05 10:00:00", "{}")
        self.assertIsNotNone(first)
        self.assertIsNone(second)  # 同 UID 已预写，重复扫描不产生重复任务
        self.assertEqual(len(self.db.get_pending_tasks(self.account_id)), 1)

    def test_outbox_dedup_key_enqueues_once(self) -> None:
        a = self.db.enqueue_outbox(self.account_id, 1, "to@example.com", "S", "B",
                                   "2026-09-05 10:00:00", "notify:1:msg")
        b = self.db.enqueue_outbox(self.account_id, 1, "to@example.com", "S", "B",
                                   "2026-09-05 10:00:00", "notify:1:msg")
        self.assertIsNotNone(a)
        self.assertIsNone(b)  # 幂等入队：崩溃重放不会重复排队
        self.assertEqual(len(self.db.due_outbox(self.account_id, max_retries=5)), 1)

    def test_message_processed_dedup(self) -> None:
        self.assertFalse(self.db.is_message_processed(self.account_id, "m1"))
        self.db.mark_message_processed(self.account_id, "m1")
        self.db.mark_message_processed(self.account_id, "m1")
        self.assertTrue(self.db.is_message_processed(self.account_id, "m1"))
        n = self.db.conn.execute(
            "SELECT COUNT(*) FROM processed_messages WHERE account_id=? AND message_id=?",
            (self.account_id, "m1")).fetchone()[0]
        self.assertEqual(n, 1)


if __name__ == "__main__":
    unittest.main()
