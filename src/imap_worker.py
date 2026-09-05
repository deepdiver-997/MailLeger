"""IMAP 拉取模块。

安全纪律（崩溃恢复与不丢信的关键）：
- 全程使用 UID 命令；
- 所有 FETCH 均使用 BODY.PEEK，不设置 \\Seen，扫描过程不改变服务器状态；
- 本模块唯一的“写”入口是 mark_seen / move_to_folder，由引擎在本地决策
  持久化到 SQLite 之后才允许调用。

说明：第一版为简单轮询（由 watch 调度），接口已按可复用设计，
后续可在此基础上扩展 IMAP IDLE 推送式监听。
"""
from __future__ import annotations

import email
import email.policy
import email.utils
import html.parser
import imaplib
import logging
import re
from dataclasses import dataclass
from email.message import Message
from typing import Iterator, Optional

from .config import AccountConfig

logger = logging.getLogger(__name__)

_FETCH_TIMEOUT = 30  # 秒
_HEADERS_TO_FETCH = "(MESSAGE-ID FROM SUBJECT DATE)"


class ImapError(RuntimeError):
    """IMAP 连接/命令失败。"""


@dataclass
class MailMeta:
    """一封邮件的元数据（预写进 scan_tasks 用）。"""
    uid: str
    message_id: str
    from_addr: str       # 解析出的纯邮箱地址
    from_display: str    # 原始 From 头（含显示名）
    subject: str
    date: str            # 优先 INTERNALDATE（服务器收到时间），回退 Date 头


# ----------------------------------------------------------------------
# 正文提取
# ----------------------------------------------------------------------

_BLOCK_TAGS = {"p", "div", "br", "tr", "li", "ul", "ol", "table", "h1", "h2", "h3",
               "h4", "h5", "h6", "section", "article", "header", "footer", "nav",
               "hr", "blockquote", "pre"}
_SKIP_TAGS = {"script", "style", "head", "noscript", "title"}


class _HTMLTextExtractor(html.parser.HTMLParser):
    """把 HTML 转成近似纯文本：丢弃 script/style 等导航噪声载体，块级标签转换行。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:  # type: ignore[override]
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        elif tag in _BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag in _BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            self._chunks.append(data)

    def get_text(self) -> str:
        raw = "".join(self._chunks)
        lines = [re.sub(r"[ \t\xa0]+", " ", ln).strip() for ln in raw.splitlines()]
        return "\n".join(ln for ln in lines if ln)


def _decode_part(part: Message) -> str:
    try:
        content = part.get_content()
        if isinstance(content, str):
            return content
    except Exception:
        pass
    payload = part.get_payload(decode=True) or b""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except (LookupError, UnicodeDecodeError):
        return payload.decode("utf-8", errors="replace")


def extract_text_body(msg: Message, max_chars: int) -> str:
    """提取纯文本正文：优先 text/plain，其次剥离 HTML 标签；截断到 max_chars。"""
    plain: Optional[str] = None
    html_text: Optional[str] = None
    for part in msg.walk():
        if part.get_content_disposition() == "attachment":
            continue
        ctype = part.get_content_type()
        if ctype == "text/plain" and plain is None:
            plain = _decode_part(part)
        elif ctype == "text/html" and html_text is None:
            html_text = _decode_part(part)
    text = plain if plain and plain.strip() else None
    if text is None and html_text:
        extractor = _HTMLTextExtractor()
        try:
            extractor.feed(html_text)
            text = extractor.get_text()
        except Exception:
            text = html_text
    if text is None:
        return ""
    return text[:max_chars]


# ----------------------------------------------------------------------
# IMAP 客户端
# ----------------------------------------------------------------------

def _chunked(items: list[str], size: int) -> Iterator[list[str]]:
    for i in range(0, len(items), size):
        yield items[i:i + size]


def _header_str(msg: Message, name: str) -> str:
    try:
        value = msg.get(name)
        return str(value) if value else ""
    except Exception:
        return ""


def _meta_from_message(uid: str, resp_line: bytes, msg: Message) -> MailMeta:
    internal = re.search(rb'INTERNALDATE "([^"]+)"', resp_line)
    date_str = internal.group(1).decode("ascii", "replace") if internal else _header_str(msg, "Date")
    from_raw = _header_str(msg, "From")
    addr = email.utils.parseaddr(from_raw)[1] or from_raw
    return MailMeta(
        uid=uid,
        message_id=_header_str(msg, "Message-ID").strip(),
        from_addr=addr.strip(),
        from_display=from_raw,
        subject=_header_str(msg, "Subject"),
        date=date_str,
    )


class ImapClient:
    """单个账号的 IMAP 会话，配合 with 使用；所有命令基于 UID。"""

    def __init__(self, account: AccountConfig) -> None:
        self.account = account
        self.conn: Optional[imaplib.IMAP4] = None

    def __enter__(self) -> "ImapClient":
        self.connect()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def connect(self) -> None:
        try:
            if self.account.imap_use_ssl:
                self.conn = imaplib.IMAP4_SSL(
                    self.account.imap_host, self.account.imap_port, timeout=_FETCH_TIMEOUT)
            else:
                self.conn = imaplib.IMAP4(
                    self.account.imap_host, self.account.imap_port, timeout=_FETCH_TIMEOUT)
            self.conn.login(self.account.imap_user, self.account.imap_password)
            typ, data = self.conn.select("INBOX")
            if typ != "OK":
                raise ImapError(f"选择 INBOX 失败: {data!r}")
        except ImapError:
            raise
        except (imaplib.IMAP4.error, OSError) as e:
            raise ImapError(f"IMAP 连接/登录失败: {e}") from e

    def close(self) -> None:
        if self.conn is not None:
            try:
                self.conn.logout()
            except Exception:
                pass
            self.conn = None

    def search_unseen(self) -> list[str]:
        """SEARCH UNSEEN，返回 UID 列表。只读操作，不改变任何服务器状态。"""
        assert self.conn is not None
        typ, data = self.conn.uid("SEARCH", None, "UNSEEN")
        if typ != "OK" or not data:
            raise ImapError(f"SEARCH UNSEEN 失败: {typ}")
        return [tok.decode("ascii", "replace") for tok in (data[0] or b"").split()]

    def fetch_headers(self, uids: list[str]) -> list[MailMeta]:
        """批量抓取元数据（INTERNALDATE + 少量头字段），BODY.PEEK 不置已读。"""
        assert self.conn is not None
        metas: list[MailMeta] = []
        for chunk in _chunked(uids, 100):
            uid_set = ",".join(chunk)
            typ, data = self.conn.uid(
                "FETCH", uid_set,
                f"(INTERNALDATE BODY.PEEK[HEADER.FIELDS {_HEADERS_TO_FETCH}])")
            if typ != "OK":
                raise ImapError(f"FETCH 头失败: {typ}")
            metas.extend(self._parse_fetch_response(data))
        return metas

    @staticmethod
    def _parse_fetch_response(data: list) -> list[MailMeta]:
        metas: list[MailMeta] = []
        for item in data:
            if not isinstance(item, tuple) or len(item) < 2:
                continue
            resp_line, header_bytes = item[0], item[1]
            if isinstance(resp_line, str):
                resp_line = resp_line.encode("ascii", "replace")
            uid_match = re.search(rb"UID\s+(\d+)", resp_line)
            if not uid_match:
                continue
            uid = uid_match.group(1).decode()
            try:
                msg = email.message_from_bytes(header_bytes, policy=email.policy.default)
            except Exception:
                logger.warning("UID %s 头解析失败，跳过", uid)
                continue
            metas.append(_meta_from_message(uid, resp_line, msg))
        return metas

    def fetch_message(self, uid: str) -> Message:
        """抓取完整原始邮件（BODY.PEEK[]），解析为 Message 对象。"""
        assert self.conn is not None
        typ, data = self.conn.uid("FETCH", uid, "(BODY.PEEK[])")
        if typ != "OK":
            raise ImapError(f"FETCH 正文失败: {typ}")
        for item in data:
            if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], (bytes, bytearray)):
                return email.message_from_bytes(bytes(item[1]), policy=email.policy.default)
        raise ImapError(f"未获取到 UID {uid} 的邮件数据")

    def mark_seen(self, uid: str) -> None:
        """仅在本地决策持久化之后由引擎调用。"""
        assert self.conn is not None
        typ, _ = self.conn.uid("STORE", uid, "+FLAGS", "(\\Seen)")
        if typ != "OK":
            raise ImapError(f"标记已读失败: {typ}")

    def move_to_folder(self, uid: str, folder: str) -> None:
        """复制到目标文件夹后删除原邮件；目标文件夹不存在则尝试创建。"""
        assert self.conn is not None
        quoted = '"' + folder.replace('"', '\\"') + '"'
        self.conn.create(quoted)  # 已存在时服务器返回 NO，忽略结果
        typ, data = self.conn.uid("COPY", uid, quoted)
        if typ != "OK":
            raise ImapError(f"复制到 {folder} 失败: {data!r}")
        self.conn.uid("STORE", uid, "+FLAGS", "(\\Deleted)")
        self.conn.expunge()
