"""Engine 崩溃恢复语义：写前日志下"at-least-once 但绝不重复发信"。

用空 api_key 触发 no_llm 降级路径，避开真实大模型/网络调用。
"""
import json
import tempfile
import unittest
from pathlib import Path

from src.config import AccountConfig, AppConfig, LLMConfig
from src.db import Database
from src.engine import Engine


def _cfg(tmpdir: str) -> AppConfig:
    return AppConfig(
        config_path=Path("config.yaml"),
        database_path=Path(tmpdir) / "e.db",
        log_file=Path(tmpdir) / "e.log",
        max_body_chars=2000,
        max_extract_chars=20000,
        reason_max_chars=100,
        max_send_retries=3,
        summary_llm_max_chars=4000,
        llm=LLMConfig(api_key=""),  # 未配置 key → 决策走 no_llm，不触网
    )


def _account() -> AccountConfig:
    return AccountConfig(
        name="main",
        imap_host="imap.example.com",
        imap_user="u@example.com",
        smtp_host="smtp.example.com",
        smtp_user="u@example.com",
        notification_to="to@example.com",
        notification_from="from@example.com",
        on_processed="mark_seen",
    )


class _StubImap:
    """记录对服务器的改动；正文已缓存在 raw_json，fetch_message 不应被调用。"""

    def __init__(self) -> None:
        self.seen: list[str] = []
        self.moves: list[tuple[str, str]] = []

    def mark_seen(self, uid: str) -> None:
        self.seen.append(uid)

    def move_to_folder(self, uid: str, folder: str) -> None:
        self.moves.append((uid, folder))

    def fetch_message(self, uid: str):  # pragma: no cover - 不应走到网络拉取
        raise AssertionError("不应触发网络拉取：body_text 已在 raw_json 缓存")


class TestEngineCrashSemantics(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self._tmp.name) / "e.db")
        self.acc = _account()
        self.account_id = self.db.upsert_account(self.acc)
        self.cfg = _cfg(self._tmp.name)
        self.engine = Engine(self.cfg, self.db)

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()

    def _mk_task(self, uid: str = "1", mid: str = "msg-1") -> int:
        raw = json.dumps({
            "uid": uid, "message_id": mid,
            "from": "alerts@example.com", "from_addr": "alerts@example.com",
            "subject": "ping", "date": "2026-09-05 10:00:00",
            "body_text": "short body",
        }, ensure_ascii=False)
        return self.db.insert_scan_task(
            self.account_id, uid, mid, "alerts@example.com", "ping",
            "2026-09-05 10:00:00", raw)

    def _due_outbox(self) -> int:
        return len(self.db.due_outbox(self.account_id, self.cfg.max_send_retries))

    def test_no_api_key_still_notifies_not_drops(self) -> None:
        # 降级保证"不静默丢信"：未配 key 时发人工查看模板，而不是丢弃
        tid = self._mk_task()
        imap = _StubImap()
        ok, outcome = self.engine._process_task(self.acc, self.account_id, self.db.get_task(tid), imap)
        self.assertTrue(ok)
        self.assertEqual(outcome, "no_llm")
        self.assertEqual(self._due_outbox(), 1)
        self.assertEqual(imap.seen, ["1"])  # 本地落库后才标已读

    def test_duplicate_reprocess_does_not_double_send(self) -> None:
        # 崩溃恢复会重复处理任务：第二次必须走 Message-ID 去重，不再入队
        tid = self._mk_task()
        imap = _StubImap()
        self.engine._process_task(self.acc, self.account_id, self.db.get_task(tid), imap)
        ok, outcome = self.engine._process_task(self.acc, self.account_id, self.db.get_task(tid), imap)
        self.assertTrue(ok)
        self.assertEqual(outcome, "dedup")
        self.assertEqual(self._due_outbox(), 1)  # 仍只有 1 封，没重复

    def test_crash_between_enqueue_and_mark_processed_no_dup(self) -> None:
        # 崩溃点：outbox 已入队、processed_messages 尚未写入就退出。
        # 重跑时 enqueue 因 dedup_key 幂等返回 None → 不重复发，且去重表补齐。
        tid = self._mk_task()
        self.db.enqueue_outbox(
            self.account_id, tid, "to@example.com", "SUB", "B",
            "2026-09-05 10:00:00", f"notify:{self.account_id}:msg-1")
        imap = _StubImap()
        ok, _ = self.engine._process_task(self.acc, self.account_id, self.db.get_task(tid), imap)
        self.assertTrue(ok)
        self.assertEqual(self._due_outbox(), 1)  # 未新增重复
        self.assertTrue(self.db.is_message_processed(self.account_id, "msg-1"))


if __name__ == "__main__":
    unittest.main()
