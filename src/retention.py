"""保留期清理：回收 Leger 自己制造、价值随时间归零的邮件。

只删两类东西（都限定在 Leger 的托管范围内）：
- agent 账号：processed_folder（命令邮件原件）与 sent_folder（已发送副本）
  里的全部超龄邮件——这两个文件夹完全由 Leger 维护，超龄整删安全；
- monitor 账号：INBOX 里 Leger 自己发出的通知（From = reply_from），
  用户的其余邮件一律不碰。

年龄过滤在**客户端**做：搜索只圈范围（FROM / ALL），逐封 FETCH
INTERNALDATE 后本地比对日期。不依赖服务器的 BEFORE 判据——我们的
ProtoRelay 会静默忽略它（2026-10-22 生产实测），任何其他服务器也难说。

删除是破坏性操作，retention_days=0（默认）时完全不启用；清理时机由
scheduler 挂在每日汇总时刻之后，也可用 `mailagent clean` 手动触发。

纯 IMAP 实现，不涉及 SMTP。
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta

from .config import AccountConfig
from .imap_worker import ImapClient, imap_utf7_encode

logger = logging.getLogger(__name__)

_STORE_CHUNK = 500
_FETCH_CHUNK = 200

# IMAP SEARCH 的日期要求英文缩写月份，strftime("%b") 跟随 locale 不可靠
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_INTERNALDATE_RE = re.compile(rb'INTERNALDATE "(\d{1,2})-([A-Za-z]{3})-(\d{4})')
_MONTH_INDEX = {m: i + 1 for i, m in enumerate(_MONTHS)}


def imap_date(d: date) -> str:
    """date → IMAP 日期（dd-Mon-yyyy，如 01-Jan-2025）。"""
    return f"{d.day:02d}-{_MONTHS[d.month - 1]}-{d.year}"


def cutoff_date(days: int) -> date:
    """保留 N 天 → 截止日（今天往前推 N 天；internal date 早于该日才算超龄）。"""
    return date.today() - timedelta(days=days)


def run_retention(account: AccountConfig, dry_run: bool = False) -> dict[str, int]:
    """清理一个账号的超龄托管邮件。返回 {文件夹: 删除数}；未启用返回 {}。"""
    if account.retention_days <= 0:
        return {}
    cutoff = cutoff_date(account.retention_days)
    stats: dict[str, int] = {}
    with ImapClient(account) as client:
        if account.role == "agent":
            folders = dict.fromkeys(
                f for f in (account.processed_folder, account.sent_folder) if f)
            for folder in folders:
                stats[folder] = _purge(client, folder, cutoff, None, dry_run)
        else:
            # monitor：INBOX 里只清 Leger 自己发的通知，用户邮件绝不触碰
            stats["INBOX"] = _purge(client, "INBOX", cutoff,
                                    account.reply_from, dry_run)
    return stats


def _purge(client: ImapClient, folder: str, cutoff: date,
           from_addr: str | None, dry_run: bool) -> int:
    """删除文件夹里 internal date 早于 cutoff（且可选 From 匹配）的邮件。"""
    assert client.conn is not None
    wire_name = f'"{imap_utf7_encode(folder)}"'
    try:
        typ, _ = client.conn.select(wire_name)
    except Exception as e:
        logger.warning("保留期清理：选择 %s 失败（%s），跳过", folder, e)
        return 0
    if typ != "OK":
        logger.warning("保留期清理：选择 %s 被拒（%s），跳过", folder, typ)
        return 0

    # 圈范围：agent 文件夹全量拉 INTERNALDATE 逐封判龄；monitor 用 FROM 缩小
    criteria = "ALL" if from_addr is None else f'FROM "{from_addr}"'
    typ, data = client.conn.uid("SEARCH", None, criteria)
    if typ != "OK" or not data:
        raise RuntimeError(f"SEARCH {criteria} 失败: {typ}")
    uids = [t.decode("ascii", "replace") for t in (data[0] or b"").split()]
    if not uids:
        return 0

    expired = [u for u, day in
               zip(uids, _fetch_internal_dates(client.conn, uids))
               if day is not None and day < cutoff]
    if not expired:
        return 0
    if dry_run:
        logger.info("保留期清理（dry-run）：%s 有 %d 封超龄", folder, len(expired))
        return len(expired)

    for i in range(0, len(expired), _STORE_CHUNK):
        uid_set = ",".join(expired[i:i + _STORE_CHUNK])
        typ, _ = client.conn.uid("STORE", uid_set, "+FLAGS.SILENT", "(\\Deleted)")
        if typ != "OK":
            raise RuntimeError(f"STORE \\Deleted 失败: {typ}")
    client.conn.expunge()
    logger.info("保留期清理：%s 删除 %d 封（早于 %s）", folder, len(expired), cutoff)
    return len(expired)


def _fetch_internal_dates(conn, uids: list[str]) -> list[date | None]:
    """批量 FETCH INTERNALDATE，返回与 uids 对齐的日期列表（解析失败为 None）。"""
    out: list[date | None] = []
    for i in range(0, len(uids), _FETCH_CHUNK):
        uid_set = ",".join(uids[i:i + _FETCH_CHUNK])
        typ, data = conn.uid("FETCH", uid_set, "(INTERNALDATE)")
        if typ != "OK":
            raise RuntimeError(f"FETCH INTERNALDATE 失败: {typ}")
        by_uid = _parse_internal_dates(data)
        out.extend(by_uid.get(u) for u in uids[i:i + _FETCH_CHUNK])
    return out


def _parse_internal_dates(data: list) -> dict[str, date]:
    """imaplib FETCH 响应 → {uid: date}。

    imaplib 对纯属性 FETCH 返回扁平 bytes（b'1 (UID 3 INTERNALDATE "...")')，
    带邮件体的 FETCH 才是 tuple——两种都吃。
    """
    out: dict[str, date] = {}
    for item in data:
        if isinstance(item, tuple):
            item = item[0] if item else b""
        if not isinstance(item, (bytes, bytearray)):
            continue
        line = bytes(item)
        uid_m = re.search(rb"UID\s+(\d+)", line)
        date_m = _INTERNALDATE_RE.search(line)
        if not uid_m or not date_m:
            continue
        try:
            out[uid_m.group(1).decode()] = date(
                int(date_m.group(3)), _MONTH_INDEX[date_m.group(2).decode()], int(date_m.group(1)))
        except (KeyError, ValueError):
            logger.warning("INTERNALDATE 解析失败：%r", line[:80])
    return out
