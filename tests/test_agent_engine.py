"""agent 账号的引擎语义：鉴权静默、回复只给请求者、线程头、幂等、失败计数、发送不变量。

全程不触网：IMAP 用桩返回预置的原始邮件字节，SmtpSession 用假对象记录发送。
"""
import email
import email.policy
import json
import tempfile
import unittest
from email.message import EmailMessage
from pathlib import Path
from unittest import mock

from src import engine as engine_module
from src.config import AccountConfig, AppConfig, LLMConfig
from src.db import Database
from src.engine import Engine

REQUESTER = "sakura@example.com"


def _cfg(tmpdir: str) -> AppConfig:
    return AppConfig(
        config_path=Path("config.yaml"),
        database_path=Path(tmpdir) / "data" / "e.db",
        log_file=Path(tmpdir) / "e.log",
        max_send_retries=3,
        llm=LLMConfig(api_key=""),
    )


def _agent(**overrides) -> AccountConfig:
    kwargs = dict(
        name="leger", imap_host="imap.example.com", imap_user="leger@example.com",
        smtp_host="smtp.example.com", smtp_user="leger@example.com",
        role="agent", allowed_senders=[REQUESTER], on_processed="mark_seen",
        max_command_attempts=2,
    )
    kwargs.update(overrides)
    return AccountConfig(**kwargs)


def _mail(from_: str, subject: str, body: str = "hi", message_id: str = "<m1@example.com>",
          **headers: str) -> bytes:
    m = EmailMessage()
    m["From"] = from_
    m["To"] = "leger@example.com"
    m["Subject"] = subject
    m["Message-ID"] = message_id
    for k, v in headers.items():
        m[k.replace("_", "-")] = v
    m.set_content(body)
    return m.as_bytes()


class _StubImap:
    def __init__(self, mails: dict[str, bytes]) -> None:
        self.mails = mails
        self.fetches = 0
        self.seen: list[str] = []
        self.moves: list[tuple[str, str]] = []

    def fetch_raw(self, uid: str) -> bytes:
        self.fetches += 1
        return self.mails[uid]

    def mark_seen(self, uid: str) -> None:
        self.seen.append(uid)

    def move_to_folder(self, uid: str, folder: str) -> None:
        self.moves.append((uid, folder))


class _FakeSmtp:
    sent: list[tuple] = []

    def __init__(self, account: AccountConfig) -> None:
        self.account = account

    def __enter__(self) -> "_FakeSmtp":
        return self

    def __exit__(self, *exc) -> bool:
        return False

    def send_html(self, to_addr: str, subject: str, html_body: str) -> None:
        _FakeSmtp.sent.append(("html", to_addr, subject))

    def send_raw(self, from_addr: str, to_addr: str, raw: bytes) -> None:
        _FakeSmtp.sent.append(("raw", from_addr, to_addr, raw))


class TestAgentEngine(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.cfg = _cfg(self._tmp.name)
        self.db = Database(self.cfg.database_path)
        self.acc = _agent()
        self.account_id = self.db.upsert_account(self.acc)
        self.engine = Engine(self.cfg, self.db)
        _FakeSmtp.sent = []
        self._patch = mock.patch.object(engine_module, "SmtpSession", _FakeSmtp)
        self._patch.start()

    def tearDown(self) -> None:
        self._patch.stop()
        self.db.close()
        self._tmp.cleanup()

    def _task(self, uid: str, mid: str, from_addr: str, subject: str) -> int:
        raw = json.dumps({"uid": uid, "message_id": mid, "from": from_addr, "from_addr": from_addr,
                          "subject": subject, "date": "2026-10-20 10:00:00", "body_text": None})
        return self.db.insert_scan_task(self.account_id, uid, mid, from_addr, subject,
                                        "2026-10-20 10:00:00", raw)

    def _run(self, uid: str, raw: bytes, from_addr: str, subject: str, mid: str = "<m1@example.com>"):
        tid = self._task(uid, mid, from_addr, subject)
        imap = _StubImap({uid: raw})
        ok, outcome = self.engine._process_task(self.acc, self.account_id, self.db.get_task(tid), imap)
        return ok, outcome, imap, tid

    def _outbox(self):
        return self.db.due_outbox(self.account_id, self.cfg.max_send_retries)

    def _reply_message(self, row):
        return email.message_from_bytes(Path(row["raw_path"]).read_bytes(), policy=email.policy.default)

    # ------------------------------------------------------------------
    def test_unauthorized_sender_is_silently_dropped(self) -> None:
        ok, outcome, imap, tid = self._run("1", _mail("mallory@example.com", "#help"), "mallory@example.com", "#help")
        self.assertTrue(ok)
        self.assertEqual(outcome, "unauthorized")
        self.assertEqual(self._outbox(), [])                     # 不回、不弹
        self.assertEqual(self.db.get_task(tid)["status"], "processed")
        self.assertEqual(imap.seen, ["1"])                       # 落库后才改服务器状态
        self.assertTrue(self.db.is_message_processed(self.account_id, "<m1@example.com>"))

    def test_help_reply_only_to_requester_with_thread_and_guard_headers(self) -> None:
        raw = _mail(f"Sakura <{REQUESTER.upper()}>", "Fwd: #help", References="<r0@example.com>")
        ok, outcome, imap, _ = self._run("2", raw, REQUESTER, "Fwd: #help")
        self.assertTrue(ok)
        self.assertEqual(outcome, "command")
        rows = self._outbox()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["to_addr"], REQUESTER)
        self.assertTrue(rows[0]["raw_path"] and Path(rows[0]["raw_path"]).is_file())
        reply = self._reply_message(rows[0])
        self.assertEqual(str(reply["To"]), REQUESTER)
        self.assertEqual(str(reply["In-Reply-To"]), "<m1@example.com>")
        self.assertEqual(str(reply["References"]), "<r0@example.com> <m1@example.com>")
        self.assertEqual(str(reply["Auto-Submitted"]), "auto-replied")
        self.assertEqual(str(reply["X-Leger-Generated"]), "1")
        self.assertEqual(str(reply["X-Leger-Command"]), "help")
        self.assertEqual(str(reply["Subject"]), "Re: #help")
        self.assertEqual(reply.get_content_type(), "multipart/alternative")
        self.assertIn("可用命令", reply.get_body(("html",)).get_content())
        self.assertIn("#status", reply.get_body(("plain",)).get_content())
        self.assertEqual(imap.seen, ["2"])

    def test_no_hash_uses_default_command_with_banner(self) -> None:
        _, outcome, _, _ = self._run("3", _mail(REQUESTER, "随便聊聊"), REQUESTER, "随便聊聊")
        self.assertEqual(outcome, "command")
        html = self._reply_message(self._outbox()[0]).get_body(("html",)).get_content()
        self.assertIn("已按默认命令处理", html)

    def test_unknown_command_gets_help_with_banner(self) -> None:
        self._run("4", _mail(REQUESTER, "#foobar"), REQUESTER, "#foobar")
        html = self._reply_message(self._outbox()[0]).get_body(("html",)).get_content()
        self.assertIn("未知命令 #foobar", html)

    def test_disabled_command_gets_notice(self) -> None:
        self._run("5", _mail(REQUESTER, "#compose 周报"), REQUESTER, "#compose 周报")
        reply = self._reply_message(self._outbox()[0])
        self.assertEqual(str(reply["Subject"]), "Re: 周报")
        self.assertIn("尚未启用", reply.get_body(("plain",)).get_content())

    def test_status_reply_masks_whitelist(self) -> None:
        self._run("6", _mail(REQUESTER, "#status"), REQUESTER, "#status")
        text = self._reply_message(self._outbox()[0]).get_body(("plain",)).get_content()
        self.assertIn("Leger 状态", text)
        self.assertIn("s***a@example.com", text)
        self.assertNotIn(REQUESTER, text)

    def test_auto_submitted_mail_is_ignored(self) -> None:
        raw = _mail(REQUESTER, "#help", Auto_Submitted="auto-replied")
        _, outcome, _, _ = self._run("7", raw, REQUESTER, "#help")
        self.assertEqual(outcome, "ignored")
        self.assertEqual(self._outbox(), [])

    def test_reprocess_does_not_double_reply(self) -> None:
        tid = self._task("8", "<m8@example.com>", REQUESTER, "#help")
        imap = _StubImap({"8": _mail(REQUESTER, "#help", message_id="<m8@example.com>")})
        self.engine._process_task(self.acc, self.account_id, self.db.get_task(tid), imap)
        ok, outcome = self.engine._process_task(self.acc, self.account_id, self.db.get_task(tid), imap)
        self.assertTrue(ok)
        self.assertEqual(outcome, "dedup")
        self.assertEqual(len(self._outbox()), 1)

    def test_raw_cache_avoids_refetch_and_is_dropped_after_finish(self) -> None:
        tid = self._task("9", "<m9@example.com>", REQUESTER, "#help")
        imap = _StubImap({"9": _mail(REQUESTER, "#help", message_id="<m9@example.com>")})
        task = self.db.get_task(tid)
        self.engine._load_raw(self.acc, task, imap)
        self.engine._load_raw(self.acc, task, imap)
        self.assertEqual(imap.fetches, 1)                        # 第二次读缓存
        cache = self.engine._raw_cache_path(self.acc, task)
        self.assertTrue(cache.is_file())
        self.engine._process_task(self.acc, self.account_id, task, imap)
        self.assertFalse(cache.exists())                         # 完成后清理

    def test_failure_counts_attempts_then_replies_error_notice(self) -> None:
        tid = self._task("10", "<m10@example.com>", REQUESTER, "#status")
        imap = _StubImap({"10": _mail(REQUESTER, "#status", message_id="<m10@example.com>")})
        with mock.patch.object(Engine, "_run_command", side_effect=RuntimeError("boom")):
            ok, _ = self.engine._process_task(self.acc, self.account_id, self.db.get_task(tid), imap)
            self.assertFalse(ok)                                 # 第 1 次：保持 pending
            self.assertEqual(self.db.get_task(tid)["status"], "pending")
            self.assertEqual(self.db.get_task(tid)["attempts"], 1)
            self.assertEqual(self._outbox(), [])
            ok, outcome = self.engine._process_task(self.acc, self.account_id, self.db.get_task(tid), imap)
        self.assertTrue(ok)                                      # 第 2 次到上限：回错误说明并终结
        self.assertEqual(outcome, "command")
        self.assertEqual(self.db.get_task(tid)["status"], "processed")
        text = self._reply_message(self._outbox()[0]).get_body(("plain",)).get_content()
        self.assertIn("处理失败", text)
        self.assertIn("boom", text)

    # ------------------------------------------------------------------
    def test_send_outbox_sends_raw_then_unlinks_file(self) -> None:
        self._run("11", _mail(REQUESTER, "#help"), REQUESTER, "#help")
        row = self._outbox()[0]
        self.assertEqual(self.engine.send_outbox(self.acc), 1)
        self.assertEqual(len(_FakeSmtp.sent), 1)
        kind, from_addr, to_addr, raw = _FakeSmtp.sent[0]
        self.assertEqual((kind, from_addr, to_addr), ("raw", "leger@example.com", REQUESTER))
        self.assertIn(b"X-Leger-Generated: 1", raw)
        self.assertFalse(Path(row["raw_path"]).exists())
        self.assertEqual(self._outbox(), [])

    def test_send_outbox_dead_letters_non_whitelisted_recipient(self) -> None:
        # 不变量兜底：即使有人把白名单外的收件人塞进 outbox，也不会发出去
        attacker = "mallory@example.com"
        eml = self.cfg.outbox_dir / "evil.eml"
        eml.parent.mkdir(parents=True, exist_ok=True)
        m = EmailMessage()
        m["From"] = "leger@example.com"
        m["To"] = attacker
        m["Subject"] = "x"
        m["X-Leger-Generated"] = "1"
        m.set_content("x")
        eml.write_bytes(m.as_bytes())
        oid = self.db.enqueue_outbox(self.account_id, None, attacker, "x", "", "2026-01-01 00:00:00",
                                     "cmd:evil", raw_path=str(eml))
        self.assertEqual(self.engine.send_outbox(self.acc), 0)
        self.assertEqual(_FakeSmtp.sent, [])
        self.assertEqual(self._outbox(), [])                     # 永久失败，不再捞取
        row = self.db.conn.execute("SELECT status, retry_count FROM outbox WHERE id=?", (oid,)).fetchone()
        self.assertEqual(row["status"], "failed")
        self.assertGreaterEqual(row["retry_count"], self.cfg.max_send_retries)

    def test_send_outbox_dead_letters_when_file_missing(self) -> None:
        self.db.enqueue_outbox(self.account_id, None, REQUESTER, "x", "", "2026-01-01 00:00:00",
                               "cmd:missing", raw_path=str(self.cfg.outbox_dir / "nope.eml"))
        self.assertEqual(self.engine.send_outbox(self.acc), 0)
        self.assertEqual(self._outbox(), [])

    def test_monitor_html_path_unchanged(self) -> None:
        # 非 raw 条目仍走 send_html（monitor 账号行为不变）
        self.db.enqueue_outbox(self.account_id, None, REQUESTER, "SUB", "<b>B</b>", "2026-01-01 00:00:00", "notify:x")
        self.assertEqual(self.engine.send_outbox(self.acc), 1)
        self.assertEqual(_FakeSmtp.sent, [("html", REQUESTER, "SUB")])


if __name__ == "__main__":
    unittest.main()
