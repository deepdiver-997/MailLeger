"""CLI 入口。

用法（在项目根目录）：
    python -m src.cli init     [--config PATH]   生成默认配置 + 初始化数据库
    python -m src.cli scan     [--config PATH]   执行一次扫描（处理所有账号 pending）
    python -m src.cli summary  [--config PATH]   立即生成并发送当日汇总
    python -m src.cli watch    [--config PATH]   持续运行（轮询扫描 + 定时汇总）

通过 pyproject.toml 安装后亦可直接使用 `mailagent` 命令。
"""
from __future__ import annotations

import argparse
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .config import AppConfig, ConfigError, init_config_file, load_config
from .db import Database
from .engine import Engine

logger = logging.getLogger("mailagent")


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", default="config/config.yaml",
                        help="配置文件路径（默认 config/config.yaml）")
    parser = argparse.ArgumentParser(prog="mailagent",
                                     description="MailAgent：AI 邮件助手（扫描/分类/通知/汇总）")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init", parents=[common], help="生成默认配置文件并初始化数据库")
    sub.add_parser("scan", parents=[common], help="执行一次扫描，处理所有账号的 pending 任务并发送到期通知")
    sub.add_parser("summary", parents=[common], help="立即生成并发送当日汇总")
    sub.add_parser("watch", parents=[common], help="持续运行：轮询扫描 + 定时汇总")
    return parser


def setup_logging(cfg: AppConfig) -> None:
    """同时输出到控制台与 logs/mailagent.log（5MB 轮转，保留 3 份）。"""
    level = getattr(logging, cfg.log_level, logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)
    cfg.log_file.parent.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(cfg.log_file, maxBytes=5 * 1024 * 1024,
                                       backupCount=3, encoding="utf-8")
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)


def cmd_init(args: argparse.Namespace) -> int:
    path = Path(args.config)
    if init_config_file(path):
        print(f"已生成默认配置文件：{path}")
    else:
        print(f"配置文件已存在，跳过生成：{path}")
    db_path = Path("data/mailagent.db")
    try:
        cfg = load_config(path)
        db_path = cfg.database_path
        setup_logging(cfg)
    except ConfigError as e:
        print(f"[提示] 配置暂不可用（{e}），先按默认路径初始化数据库")
    db = Database(db_path)
    db.close()
    Path("logs").mkdir(parents=True, exist_ok=True)
    print(f"数据库已初始化：{db_path}")
    print("下一步：")
    print("  1. 编辑配置文件，填入 IMAP/SMTP 与内部通知邮箱")
    print("  2. cp .env.example .env 并填入密码与 API Key（或直接设置同名环境变量）")
    print("  3. 运行 python -m src.cli scan 验证；长期运行用 watch 或 cron/systemd")
    return 0


def cmd_scan(cfg: AppConfig, engine: Engine) -> int:
    for account in cfg.accounts:
        print(f"== 扫描账号 {account.name}（{account.imap_user}）==")
        try:
            stats = engine.run_account_scan(account)
            readable = ", ".join(f"{k}={v}" for k, v in stats.items() if v)
            print("  完成：" + (readable or "无待处理内容"))
        except Exception:
            logger.exception("账号 %s 扫描失败", account.name)
            print("  扫描失败，详见日志")
    return 0


def cmd_summary(cfg: AppConfig, engine: Engine) -> int:
    for account in cfg.accounts:
        try:
            if engine.generate_summary(account, force=True):
                sent = engine.send_outbox(account)
                print(f"账号 {account.name}：汇总已生成（发送 {sent} 封）")
            else:
                print(f"账号 {account.name}：本次未生成新汇总（今日已生成过）")
        except Exception:
            logger.exception("账号 %s 生成汇总失败", account.name)
    return 0


def cmd_watch(cfg: AppConfig, engine: Engine) -> int:
    import signal

    from .scheduler import WatchScheduler

    def _on_term(signum, frame):  # systemd stop 时优雅退出
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _on_term)
    try:
        WatchScheduler(cfg, engine).run()
    except KeyboardInterrupt:
        logger.info("收到停止信号，watch 退出")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        if args.command == "init":
            return cmd_init(args)
        cfg = load_config(args.config)
        setup_logging(cfg)
        db = Database(cfg.database_path)
        try:
            engine = Engine(cfg, db)
            if args.command == "scan":
                return cmd_scan(cfg, engine)
            if args.command == "summary":
                return cmd_summary(cfg, engine)
            if args.command == "watch":
                return cmd_watch(cfg, engine)
        finally:
            db.close()
    except ConfigError as e:
        logger.error("配置错误：%s", e)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
