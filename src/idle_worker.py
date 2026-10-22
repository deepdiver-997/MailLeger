"""IMAP IDLE 推送监听（RFC 2177）——agent 账号的即时反应通道。

服务端（ProtoRelay）在 IDLE 期间会主动推 `* N EXISTS`（轮询定时器 ~1s），
本模块让 Leger 不再干等 scan_interval 轮询：每个开启 idle 的账号一条专线
连接 + 一个后台线程，收到 EXISTS 就点亮 wake 事件，主循环立即扫描。

与轮询的关系：**IDLE 只是加速器，不是正确性依赖**。轮询扫描照旧运行
（scan_interval_seconds），IDLE 线程崩溃/服务器不支持/网络抖动时无感降级。

resync 握手（防推送抑制，关键）：
服务端只在"计数 > 已宣告基线"时推送，基线在 SELECT 时置为当次计数。
on_processed=move 的账号扫描后 INBOX 计数下降，若 IDLE 连接不重选，
基线停在高位，之后的新邮件计数可能一直 ≤ 基线 → 永远不推送。
所以约定：主循环每次扫完某账号后调 request_resync()，watcher 收到后
断开重连（fresh SELECT 刷新基线为处理后的真实计数）。EXISTS 触发后
watcher 也等 resync（上限 120s 超时自走）再重连——顺序不能反：先扫后选，
基线才是"处理完"的计数。

imaplib 没有 IDLE 支持，用 send()/readline() 原语手工实现（这两个方法
走 imaplib 自己的缓冲文件，与后续命令共用状态，不会错位）。_new_tag()
虽是私有方法，但比自造 tag 安全（imaplib 的 tagpre 是随机两字母，撞上
会话错乱；用它自己的发号器绝无冲突）。
"""
from __future__ import annotations

import logging
import socket
import threading
import time

from .config import AccountConfig
from .imap_worker import ImapClient, ImapError

logger = logging.getLogger(__name__)

# RFC 2177：客户端应不超过 29 分钟重新发起 IDLE（服务器可能丢静默连接）
_REIDLE_SECONDS = 25 * 60
# IDLE 等待期间的醒着节拍：查 stop / resync 标志（不是心跳，不发任何字节）
_TICK_SECONDS = 10
# EXISTS 触发后等主循环 resync 的上限：主循环在忙（如大模型生成）时不能干等
_RESYNC_WAIT_SECONDS = 120
_DONE_TIMEOUT_SECONDS = 10
_RECONNECT_MAX_SECONDS = 300


def _readline_alive(conn) -> bytes:
    """readline() 且把"连接已死"翻译成异常。

    Python 3.12+ 的 imaplib.readline 在 EOF/ConnectionError 时不再抛异常，
    而是返回空行（b""）——若不识别，IDLE 等待循环会在死连接上空转。
    IMAP 等待期服务器只会发未打标签响应，空行必然意味着连接完了。
    旧版 Python 的 EOF 走 imaplib.abort 异常，调用方统一按 ImapError 捕获。
    """
    line = conn.readline()
    if not line:
        raise ImapError("连接已关闭（readline 返回空行）")
    return line


class IdleWatcher:
    """单账号 IDLE 监听线程。wake 由主循环提供；stop 可外部注入以便测试。"""

    def __init__(self, account: AccountConfig, wake: threading.Event,
                 stop: threading.Event | None = None,
                 backoff_seconds: float = 5.0) -> None:
        self.account = account
        self.wake = wake
        self._stop = stop or threading.Event()
        self._resync = threading.Event()
        self._backoff = backoff_seconds          # 测试可调小
        self._thread: threading.Thread | None = None
        self._conn: object | None = None         # 仅 stop() 时强制关闭用
        self.idle_gave_up = False                # 服务器不支持 IDLE → 永久降级

    # ------------------------------------------------------------------
    # 外部接口（主循环调用）
    # ------------------------------------------------------------------
    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, name=f"idle-{self.account.name}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """请求退出并尽快解除阻塞（shutdown 让阻塞中的 recv 立刻失败）。

        只调 close() 在 macOS 上不会唤醒另一个线程里阻塞的 recv；
        shutdown(SHUT_RDWR) 两端都生效。
        """
        self._stop.set()
        conn = self._conn
        if conn is not None:
            try:
                sock = conn.socket()
                sock.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
            try:
                conn.socket().close()
            except Exception:
                pass
        if self._thread is not None:
            self._thread.join(timeout=5)

    def request_resync(self) -> None:
        """主循环扫完该账号后调用：watcher 断开重连，刷新服务端 EXISTS 基线。"""
        self._resync.set()

    # ------------------------------------------------------------------
    # 线程体
    # ------------------------------------------------------------------
    def _run(self) -> None:
        backoff = self._backoff
        while not self._stop.is_set():
            try:
                reason = self._session()
            except Exception as e:   # 连接/协议层任何失败：记录后重连
                logger.warning("账号 %s IDLE 断开：%s", self.account.name, e)
                reason = "error"
            if reason == "stop":
                return
            if reason == "error":
                if not self._stop.wait(backoff):
                    backoff = min(backoff * 2, _RECONNECT_MAX_SECONDS)
                continue
            backoff = self._backoff   # 正常 resync：立即重连，不退避

    def _session(self) -> str:
        """一条连接的完整生命周期。返回 'stop' | 'resync' | 'error'。"""
        with ImapClient(self.account) as client:
            self._conn = client.conn
            try:
                if "IDLE" not in getattr(client.conn, "capabilities", ()):
                    logger.info("账号 %s 的服务器不支持 IDLE，永久退回轮询", self.account.name)
                    self.idle_gave_up = True
                    return "stop"
                while not self._stop.is_set():
                    fired = self._idle_slice(client.conn)
                    if fired:
                        logger.info("账号 %s IDLE 收到新邮件推送", self.account.name)
                        self.wake.set()
                        # 等主循环扫完（它 resync）再重连；超时自走保证活性
                        self._resync.wait(timeout=_RESYNC_WAIT_SECONDS)
                        self._resync.clear()
                        return "resync"
                    if self._resync.is_set():
                        self._resync.clear()
                        return "resync"
                    # _idle_slice 正常超时返回 False → 重新进 IDLE（≤25min）
                return "stop"
            finally:
                self._conn = None

    def _idle_slice(self, conn) -> bool:
        """进一次 IDLE，等到 EXISTS / 周期到点 / 停止。返回是否收到 EXISTS。"""
        sock = conn.socket()
        old_timeout = sock.gettimeout()
        tag = conn._new_tag()
        entered = False
        try:
            conn.send(tag + b" IDLE\r\n")
            sock.settimeout(_TICK_SECONDS)
            while True:   # 等 "+ idling" continuation；tagged 行 = 服务器拒绝
                line = _readline_alive(conn)
                if line.startswith(b"+"):
                    entered = True
                    break
                if line.startswith(tag):
                    raise ImapError(f"IDLE 被拒绝: {line!r}")

            deadline = time.time() + _REIDLE_SECONDS
            while not self._stop.is_set():
                remaining = deadline - time.time()
                if remaining <= 0:
                    return False   # 周期到点：上层重进 IDLE（刷新连接状态）
                sock.settimeout(min(_TICK_SECONDS, remaining))
                try:
                    line = _readline_alive(conn)
                except (TimeoutError, socket.timeout):
                    continue       # 节拍醒：查 stop 标志后继续等
                text = line.strip().upper()
                if text.endswith(b"EXISTS"):
                    return True
                if text.startswith(b"* BYE"):
                    raise ImapError("服务器在 IDLE 中关闭会话")
            return False
        finally:
            if entered:
                # 无论从哪条路退出，先把 IDLE 收尾（DONE → 吃到 tagged 行），
                # 否则连接上残留的 EXISTS/tagged 会污染下一次登录后的解析。
                try:
                    sock.settimeout(_DONE_TIMEOUT_SECONDS)
                    conn.send(b"DONE\r\n")
                    while True:
                        line = _readline_alive(conn)
                        if line.startswith(tag):
                            break
                except Exception:
                    pass
            sock.settimeout(old_timeout)
