"""retention 端到端测试：进程内带邮箱状态的假 IMAP 服务器 + 真实 imaplib。

覆盖：删旧留新、monitor 只清自己发的通知（不碰用户邮件）、中文文件夹名
（modified UTF-7）、retention_days=0 完全不连、关闭的文件夹跳过不炸。
"""
import socket
import threading
import time
import unittest
from datetime import date, timedelta

from src.config import AccountConfig
from src.retention import imap_date, run_retention


class FakeMailboxServer:
    """最小 IMAP 服务器：维护 {文件夹: [邮件]} 状态，实现本次需要的子集。

    邮件字段：uid / internal_date(str, IMAP 格式) / from_addr / deleted。
    SEARCH 只支持 ALL / FROM "<addr>"（与真实 ProtoRelay 一致——它会静默忽略
    BEFORE 日期判据，所以年龄过滤必须在客户端做）。支持 FETCH INTERNALDATE。
    """

    def __init__(self, mailboxes: dict[str, list[dict]]):
        self.mailboxes = mailboxes          # 线程内访问，测试单线程驱动即可
        self.logins = 0
        self.port = None
        self._srv = socket.socket()
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(4)
        self.port = self._srv.getsockname()[1]
        threading.Thread(target=self._accept_loop, daemon=True).start()

    def close(self) -> None:
        try:
            self._srv.close()
        except OSError:
            pass

    def _accept_loop(self) -> None:
        while True:
            try:
                conn, _ = self._srv.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn: socket.socket) -> None:
        conn.settimeout(10)
        conn.sendall(b"* OK retention fake ready\r\n")
        selected: str | None = None
        f = conn.makefile("rb")
        while True:
            line = f.readline()
            if not line:
                break
            parts = line.decode(errors="replace").strip().split(None, 1)
            if not parts:
                continue
            tag, rest = parts[0], (parts[1] if len(parts) > 1 else "")
            upper = rest.upper()
            if upper.startswith("UID "):
                # UID SEARCH / UID STORE：剥掉前缀，按裸命令分发
                rest = rest[4:]
                upper = upper[4:]
            if upper.startswith("CAPABILITY"):
                conn.sendall(b"* CAPABILITY IMAP4rev1\r\n")
                conn.sendall(f"{tag} OK CAPABILITY completed\r\n".encode())
            elif upper.startswith("LOGIN"):
                self.logins += 1
                conn.sendall(f"{tag} OK LOGIN completed\r\n".encode())
            elif upper.startswith("SELECT"):
                name = rest.split(None, 1)[1].strip().strip('"')
                selected = name if name in self.mailboxes else None
                count = len(self.mailboxes.get(name, []))
                conn.sendall(f"* {count} EXISTS\r\n".encode())
                conn.sendall(f"{tag} OK [READ-WRITE] SELECT completed\r\n".encode())
            elif upper.startswith("SEARCH"):
                uids = self._search(selected, rest[len("SEARCH"):].strip())
                conn.sendall(f"* SEARCH{''.join(' ' + u for u in uids)}\r\n".encode())
                conn.sendall(f"{tag} OK SEARCH completed\r\n".encode())
            elif upper.startswith("FETCH"):
                conn.sendall(self._fetch(selected, rest[len("FETCH"):].strip()).encode())
                conn.sendall(f"{tag} OK FETCH completed\r\n".encode())
            elif upper.startswith("STORE"):
                self._store(selected, rest[len("STORE"):].strip())
                conn.sendall(f"{tag} OK STORE completed\r\n".encode())
            elif upper.startswith("EXPUNGE"):
                box = self.mailboxes.get(selected or "", [])
                before = len(box)
                self.mailboxes[selected or ""] = [m for m in box if not m["deleted"]]
                removed = before - len(self.mailboxes[selected or ""])
                for seq in range(before, before - removed, -1):
                    conn.sendall(f"* {seq} EXPUNGE\r\n".encode())
                conn.sendall(f"{tag} OK EXPUNGE completed\r\n".encode())
            elif upper.startswith("LOGOUT"):
                conn.sendall(b"* BYE bye\r\n")
                conn.sendall(f"{tag} OK LOGOUT completed\r\n".encode())
                break
            else:
                conn.sendall(f"{tag} OK noop\r\n".encode())
        try:
            conn.close()
        except OSError:
            pass

    def _search(self, folder: str | None, criteria: str) -> list[str]:
        box = self.mailboxes.get(folder or "", [])
        frm = None
        tokens = criteria.split()
        i = 0
        while i < len(tokens):
            if tokens[i].upper() == "FROM":
                frm = tokens[i + 1].strip('"').lower()
                i += 2
            else:
                i += 1   # ALL 或不认识的判据：真实服务器会静默忽略
        out = []
        for m in box:
            if m["deleted"]:
                continue
            if frm is not None and frm not in m["from_addr"].lower():
                continue
            out.append(m["uid"])
        return out

    def _fetch(self, folder: str | None, rest: str) -> str:
        uid_set, _, _attrs = rest.partition(" ")
        uids = set(uid_set.split(","))
        lines = []
        box = self.mailboxes.get(folder or "", [])
        for seq, m in enumerate(box, start=1):
            if m["uid"] in uids and not m["deleted"]:
                lines.append(f'* {seq} FETCH (UID {m["uid"]} '
                             f'INTERNALDATE "{m["internal_date"]} 10:00:00 +0000")\r\n')
        return "".join(lines)

    def _store(self, folder: str | None, rest: str) -> None:
        uid_set, _, flag_part = rest.partition(" ")
        uids = set(uid_set.split(","))
        box = self.mailboxes.get(folder or "", [])
        for m in box:
            if m["uid"] in uids and "\\DELETED" in flag_part.upper():
                m["deleted"] = True

    def live_uids(self, folder: str) -> list[str]:
        return [m["uid"] for m in self.mailboxes.get(folder, []) if not m["deleted"]]


def _mk_mail(uid, days_ago, from_addr="boss@x.y"):
    return {"uid": uid, "internal_date": imap_date(date.today() - timedelta(days=days_ago)),
            "from_addr": from_addr, "deleted": False}


def _agent(days: int) -> AccountConfig:
    return AccountConfig(name="a", imap_host="127.0.0.1", imap_port=0,
                         imap_use_ssl=False, imap_user="a@x.y", imap_password="pw",
                         role="agent", allowed_senders=["boss@x.y"],
                         smtp_host="127.0.0.1",
                         processed_folder="Done", sent_folder="发件箱",
                         retention_days=days)


def _monitor(days: int) -> AccountConfig:
    return AccountConfig(name="m", imap_host="127.0.0.1", imap_port=0,
                         imap_use_ssl=False, imap_user="m@x.y", imap_password="pw",
                         notification_from="m@x.y", notification_to="notify@x.y",
                         smtp_host="127.0.0.1", retention_days=days)


class RetentionTest(unittest.TestCase):
    def test_agent_purges_old_keeps_fresh(self) -> None:
        srv = FakeMailboxServer({
            "Done": [_mk_mail("1", 100), _mk_mail("2", 3)],
            "&U9FO9nux-": [_mk_mail("9", 100), _mk_mail("8", 1)],   # 发件箱 (UTF-7)
        })
        acc = _agent(90)
        acc.imap_port = srv.port
        stats = run_retention(acc)
        self.assertEqual(stats, {"Done": 1, "发件箱": 1})
        self.assertEqual(srv.live_uids("Done"), ["2"])
        self.assertEqual(srv.live_uids("&U9FO9nux-"), ["8"])
        srv.close()

    def test_monitor_only_own_notifications(self) -> None:
        srv = FakeMailboxServer({
            "INBOX": [_mk_mail("1", 100, "m@x.y"),      # 自己发的旧通知 → 删
                      _mk_mail("2", 100, "someone@else.z"),  # 用户邮件 → 留
                      _mk_mail("3", 1, "m@x.y")],        # 新通知 → 留
        })
        acc = _monitor(30)
        acc.imap_port = srv.port
        stats = run_retention(acc)
        self.assertEqual(stats, {"INBOX": 1})
        self.assertEqual(srv.live_uids("INBOX"), ["2", "3"])
        srv.close()

    def test_disabled_makes_no_connection(self) -> None:
        srv = FakeMailboxServer({"Done": [_mk_mail("1", 999)]})
        acc = _agent(0)
        acc.imap_port = srv.port
        self.assertEqual(run_retention(acc), {})
        self.assertEqual(srv.logins, 0)   # 未启用：连 IMAP 都不该碰
        srv.close()

    def test_missing_folder_skipped_not_fatal(self) -> None:
        srv = FakeMailboxServer({})   # Done / 发件箱 都不存在
        acc = _agent(30)
        acc.imap_port = srv.port
        stats = run_retention(acc)
        self.assertEqual(stats, {"Done": 0, "发件箱": 0})
        srv.close()

    def test_imap_date_format_is_english_and_padded(self) -> None:
        self.assertEqual(imap_date(date(2025, 1, 5)), "05-Jan-2025")
        self.assertEqual(imap_date(date(2026, 12, 31)), "31-Dec-2026")


if __name__ == "__main__":
    unittest.main()
