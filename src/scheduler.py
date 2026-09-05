"""watch 模式：持续轮询扫描 + 到点生成每日汇总。

第一版为简单轮询（间隔 scan_interval_seconds）；每 10 秒检查一次是否到达
各账号的 daily_summary_time。后续可在 ImapClient 基础上扩展 IMAP IDLE
实现推送式监听。
"""
from __future__ import annotations

import logging
import time

from .config import AppConfig
from .engine import Engine

logger = logging.getLogger(__name__)


class WatchScheduler:
    def __init__(self, cfg: AppConfig, engine: Engine) -> None:
        self.cfg = cfg
        self.engine = engine
        self.interval = max(30, cfg.scan_interval_seconds)

    def run(self) -> None:
        logger.info("watch 启动：每 %d 秒扫描一次；每日汇总时间按各账号配置", self.interval)
        next_scan = 0.0  # 立即执行第一轮扫描
        while True:
            now = time.time()
            if now >= next_scan:
                next_scan = now + self.interval
                self._scan_all()
            else:
                self._summary_check()
            time.sleep(10)

    def _scan_all(self) -> None:
        for account in self.cfg.accounts:
            try:
                stats = self.engine.run_account_scan(account)
                logger.info("账号 %s 扫描完成：%s", account.name, stats)
            except Exception:
                logger.exception("账号 %s 扫描异常", account.name)

    def _summary_check(self) -> None:
        """非扫描周期内：检查是否到达各账号的汇总时间，到点则生成并发送。"""
        for account in self.cfg.accounts:
            try:
                if self.engine.generate_summary(account, force=False):
                    self.engine.send_outbox(account)
            except Exception:
                logger.exception("账号 %s 每日汇总异常", account.name)
