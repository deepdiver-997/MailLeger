"""watch 模式：持续扫描 + 到点生成每日汇总 + agent 账号 IDLE 即时唤醒。

节奏设计：
- agent 账号（idle: true）：后台 IdleWatcher 线程挂着 IMAP IDLE，服务端
  推 `* N EXISTS` 时点亮 wake 事件，主循环立刻扫描该账号——命令秒级响应；
- 所有账号照旧按 scan_interval_seconds 周期兜底扫描（IDLE 挂了也无感降级）；
- 每日汇总在非扫描周期内按各账号 daily_summary_time 检查。

每轮扫描一个 idle 账号后调 request_resync()：扫描（尤其 on_processed=move）
会让 INBOX 计数下降，IDLE 连接必须重选刷新服务端基线，否则后续新邮件可能
因"没超过基线"而不再推送（详见 idle_worker 模块注释）。
"""
from __future__ import annotations

import logging
import threading
import time

from .config import AppConfig
from .engine import Engine
from .idle_worker import IdleWatcher

logger = logging.getLogger(__name__)


class WatchScheduler:
    def __init__(self, cfg: AppConfig, engine: Engine) -> None:
        self.cfg = cfg
        self.engine = engine
        self.interval = max(30, cfg.scan_interval_seconds)
        self._wake = threading.Event()          # 任一 watcher 收到 EXISTS
        self._watchers: dict[str, IdleWatcher] = {}

    def run(self) -> None:
        self._start_watchers()
        logger.info("watch 启动：每 %d 秒兜底扫描一次；IDLE 账号 %d 个；每日汇总时间按各账号配置",
                    self.interval, len(self._watchers))
        next_scan = 0.0  # 立即执行第一轮扫描
        try:
            while True:
                now = time.time()
                if now >= next_scan:
                    next_scan = now + self.interval
                    self._scan_all()
                else:
                    self._scan_woken()
                    self._summary_check()
                self._wake.wait(timeout=10)
                self._wake.clear()
        finally:
            self._stop_watchers()

    # ------------------------------------------------------------------
    # IDLE watcher 生命周期
    # ------------------------------------------------------------------
    def _start_watchers(self) -> None:
        for account in self.cfg.accounts:
            if not account.idle:
                continue
            watcher = IdleWatcher(account, self._wake)
            watcher.start()
            self._watchers[account.name] = watcher

    def _stop_watchers(self) -> None:
        for watcher in self._watchers.values():
            watcher.stop()
        self._watchers.clear()

    # ------------------------------------------------------------------
    # 扫描
    # ------------------------------------------------------------------
    def _scan_woken(self) -> None:
        """IDLE 点名的账号立即扫描（不等周期兜底）。"""
        for name, watcher in self._watchers.items():
            if not watcher.wake.is_set():
                continue
            watcher.wake.clear()
            account = next((a for a in self.cfg.accounts if a.name == name), None)
            if account is not None:
                self._scan_account(account)

    def _scan_all(self) -> None:
        for account in self.cfg.accounts:
            self._scan_account(account)

    def _scan_account(self, account) -> None:
        try:
            stats = self.engine.run_account_scan(account)
            logger.info("账号 %s 扫描完成：%s", account.name, stats)
        except Exception:
            logger.exception("账号 %s 扫描异常", account.name)
        finally:
            watcher = self._watchers.get(account.name)
            if watcher is not None:
                watcher.request_resync()

    def _summary_check(self) -> None:
        """非扫描周期内：检查是否到达各账号的汇总时间，到点则生成并发送。"""
        for account in self.cfg.accounts:
            try:
                if self.engine.generate_summary(account, force=False):
                    self.engine.send_outbox(account)
            except Exception:
                logger.exception("账号 %s 每日汇总异常", account.name)
