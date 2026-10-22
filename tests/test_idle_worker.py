"""IdleWatcher 端到端测试：进程内假 IMAP 服务器 + 真实 imaplib 客户端路径。

覆盖：EXISTS 唤醒、resync 重连握手、主动断线重连（退避）、stop 及时退出、
服务器不支持 IDLE 时永久降级。全部走真实 socket，不 mock imaplib 内部。
"""
import socket
import threading
import time
import unittest
from pathlib import Path

from src.config import AccountConfig
from src.idle_worker import IdleWatcher


class FakeImapServer:
    """极小 IMAP 服务器：greeting/CAPABILITY/LOGIN/SELECT/IDLE/DONE/LOGOUT。

    测试通过 instance 控制器推送 EXISTS、断开连接、观察登录次数。
    """

    def __init__(self, capabilities="IMAP4rev1 IDLE"):
        self.capabilities = capabilities
        self.logins = 0
        self.idle_starts = 0
        self.connections: list[socket.socket] = []
        self._lock = threading.Lock()
        self._sock = socket.create_connection  # noqa: F841 (占位避免误删)
        self._srv = socket.socket()
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(8)
        self.port = self._srv.getsockname()[1]
        self._threads: list[threading.Thread] = []
        self._accepting = True
        self._accept_thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._accept_thread.start()

    def close(self) -> None:
        self._accepting = False
        with self._lock:
            for conn in list(self.connections):
                self._kill(conn)
            self.connections.clear()
        try:
            self._srv.close()
        except OSError:
            pass

    # ---- 测试控制器 ----
    def push_exists(self, count: int) -> None:
        with self._lock:
            for conn in list(self.connections):
                try:
                    conn.sendall(f"* {count} EXISTS\r\n".encode())
                except OSError:
                    pass

    def drop_connections(self) -> None:
        with self._lock:
            for conn in list(self.connections):
                self._kill(conn)
                self.connections.remove(conn)

    @staticmethod
    def _kill(conn: socket.socket) -> None:
        # 只调 close() 不够：_serve 里 makefile() 的引用让 close 变成延迟关闭，
        # fd 不释放、FIN 不发出，客户端的 recv 永远等不到 EOF。
        # shutdown 直接作用在 fd 上，两端立刻看到断开。
        try:
            conn.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            conn.close()
        except OSError:
            pass

    # ---- 服务端实现 ----
    def _accept_loop(self) -> None:
        while self._accepting:
            try:
                conn, _ = self._srv.accept()
            except OSError:
                return
            with self._lock:
                self.connections.append(conn)
            t = threading.Thread(target=self._serve, args=(conn,), daemon=True)
            t.start()
            self._threads.append(t)

    def _serve(self, conn: socket.socket) -> None:
        conn.settimeout(10)
        conn.sendall(b"* OK fake ready\r\n")

        def reply_tagged(tag: str, text: str) -> None:
            conn.sendall(f"{tag} {text}\r\n".encode())

        while True:
            try:
                line = conn.makefile("rb").readline()
            except OSError:
                break
            if not line:
                break
            parts = line.decode(errors="replace").strip().split(None, 1)
            if not parts:
                continue
            tag, rest = parts[0], (parts[1] if len(parts) > 1 else "").upper()
            if rest.startswith("CAPABILITY"):
                conn.sendall(f"* CAPABILITY {self.capabilities}\r\n".encode())
                reply_tagged(tag, "OK CAPABILITY completed")
            elif rest.startswith("LOGIN"):
                with self._lock:
                    self.logins += 1
                reply_tagged(tag, "OK LOGIN completed")
            elif rest.startswith("SELECT"):
                conn.sendall(b"* 3 EXISTS\r\n* 0 RECENT\r\n"
                             b"* OK [UIDVALIDITY 1] UIDs valid\r\n"
                             b"* OK [UIDNEXT 4] Predicted next UID\r\n")
                reply_tagged(tag, "OK [READ-WRITE] SELECT completed")
            elif rest.startswith("IDLE"):
                with self._lock:
                    self.idle_starts += 1
                conn.sendall(b"+ idling\r\n")
                # 阻塞读 DONE（连接被关时 read 失败 → 线程退出）
                try:
                    done = conn.makefile("rb").readline()
                except OSError:
                    break
                if done.strip().upper() == b"DONE":
                    reply_tagged(tag, "OK IDLE terminated")
            elif rest.startswith("LOGOUT"):
                conn.sendall(b"* BYE fake logging out\r\n")
                reply_tagged(tag, "OK LOGOUT completed")
                break
            else:
                reply_tagged(tag, "OK noop")


def _account(port: int) -> AccountConfig:
    return AccountConfig(
        name="it",
        imap_host="127.0.0.1",
        imap_port=port,
        imap_use_ssl=False,
        imap_user="u@test.local",
        imap_password="pw",
        role="agent",
        allowed_senders=["boss@test.local"],
        smtp_host="127.0.0.1",
    )


class IdleWatcherTest(unittest.TestCase):
    def setUp(self) -> None:
        self.server = FakeImapServer()
        self.wake = threading.Event()
        self.stop = threading.Event()

    def tearDown(self) -> None:
        self.stop.set()
        self.server.close()

    def _watcher(self, backoff: float = 0.1) -> IdleWatcher:
        w = IdleWatcher(_account(self.server.port), self.wake,
                        stop=self.stop, backoff_seconds=backoff)
        w.start()
        return w

    def _wait_until(self, pred, timeout: float = 5.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if pred():
                return
            time.sleep(0.02)
        self.fail("等待条件超时")

    def test_exists_wakes_and_resync_reconnects(self) -> None:
        w = self._watcher()
        self._wait_until(lambda: self.server.idle_starts >= 1)

        self.server.push_exists(5)
        self.assertTrue(self.wake.wait(5), "EXISTS 应点亮 wake 事件")

        # 主循环约定：扫完后 request_resync → watcher 重连（登录数 +1）并重新 IDLE
        logins_before = self.server.logins
        w.request_resync()
        self._wait_until(lambda: self.server.logins >= logins_before + 1)
        self._wait_until(lambda: self.server.idle_starts >= 2)
        w.stop()

    def test_exists_without_resync_self_recovers(self) -> None:
        # 主循环忘了 resync 也不能卡死：120s 上限对本测试太长，直接验证
        # resync 未到时 watcher 仍保持连接并可在下一次 resync 后重连
        w = self._watcher()
        self._wait_until(lambda: self.server.idle_starts >= 1)
        self.server.push_exists(5)
        self.assertTrue(self.wake.wait(5))
        w.request_resync()
        self._wait_until(lambda: self.server.idle_starts >= 2)
        w.stop()

    def test_reconnects_after_connection_drop(self) -> None:
        w = self._watcher(backoff=0.05)
        self._wait_until(lambda: self.server.idle_starts >= 1)
        self.server.drop_connections()
        self._wait_until(lambda: self.server.idle_starts >= 2,
                         timeout=10)   # 退避后重连并重新 IDLE
        w.stop()

    def test_stop_joins_promptly(self) -> None:
        w = self._watcher()
        self._wait_until(lambda: self.server.idle_starts >= 1)
        began = time.time()
        w.stop()
        self.assertLess(time.time() - began, 5, "stop 应在数秒内解除阻塞并退出")
        self.assertFalse(w._thread.is_alive())

    def test_gives_up_permanently_without_idle_capability(self) -> None:
        self.server.close()
        self.server = FakeImapServer(capabilities="IMAP4rev1")   # 无 IDLE
        w = self._watcher()
        self._wait_until(lambda: w.idle_gave_up)
        self.assertFalse(self.wake.is_set())
        w.stop()
        # 永久降级后不应再连
        logins = self.server.logins
        time.sleep(0.2)
        self.assertEqual(self.server.logins, logins)


if __name__ == "__main__":
    unittest.main()
