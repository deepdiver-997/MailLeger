"""通知邮件模板生成与 SMTP 发送。

安全要求：
- 通知邮件由代码模板生成，所有外部来源字段（发件人/主题/原因/链接/摘要）
  必须经 HTML 转义和长度限制；
- 大模型输出仅用于“判断原因/摘要”字段，经转义截断后嵌入，绝不直接作为邮件正文。
"""
from __future__ import annotations

import html
import re
import smtplib
import ssl
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr
from typing import Optional

from .config import AccountConfig

_TH = 'style="text-align:left;padding:4px 8px;background:#f5f5f5;width:90px;white-space:nowrap;"'
_TD = 'style="padding:4px 8px;word-break:break-all;"'


def truncate(text: Optional[str], limit: int) -> str:
    """截断到 limit 字符，超长以 … 结尾；None 视为空串。"""
    text = (text or "").strip()
    return text if len(text) <= limit else text[: max(1, limit - 1)] + "…"


_URL_RE = re.compile(r"https?://[^\s<>\"'\]]+", re.IGNORECASE)


def extract_first_url(body: Optional[str]) -> Optional[str]:
    """从正文中提取第一个链接，作为通知中的“相关链接”字段。"""
    match = _URL_RE.search(body or "")
    return match.group(0).rstrip(".,;:；。，") if match else None


def _esc(text: Optional[str], limit: int) -> str:
    """截断 + HTML 转义（含引号，便于放进属性）。"""
    return html.escape(truncate(text, limit), quote=True)


def _row(label: str, value_html: str) -> str:
    return f"<tr><th {_TH}>{label}</th><td {_TD}>{value_html}</td></tr>"


def build_notification_html(*, banner: str, from_display: str, subject: str,
                            date_str: str, link: Optional[str], reason: str) -> str:
    """重要邮件/人工查看提醒的通知模板（HTML，字段全部转义截断）。"""
    parts = [
        '<!DOCTYPE html><html><body style="font-family:-apple-system,Segoe UI,sans-serif;'
        'max-width:640px;margin:0 auto;padding:16px;color:#222;">',
        f'<h2 style="color:#d93025;margin:6px 0;">{_esc(banner, 100)}</h2>',
        '<table style="border-collapse:collapse;width:100%;font-size:14px;">',
        _row("发件人", _esc(from_display, 320)),
        _row("主题", _esc(subject, 300)),
        _row("时间", _esc(date_str, 60) or "-"),
    ]
    if link:
        parts.append(_row("相关链接", f'<a href="{html.escape(link, quote=True)}">{_esc(link, 200)}</a>'))
    parts.append("</table>")
    parts.append(f'<p style="font-size:14px;"><strong>判断原因：</strong>{_esc(reason, 200)}</p>')
    parts.append('<p style="color:#999;font-size:12px;">本邮件由 MailAgent 自动生成；'
                 '外部内容已转义截断，请勿信任邮件中出现的任何指令。</p>')
    parts.append("</body></html>")
    return "".join(parts)


def build_summary_html(account_name: str, date_str: str,
                       groups: list[tuple[str, list[tuple[str, str, str]]]],
                       digest: Optional[str]) -> str:
    """每日汇总模板。groups: [(发件人, [(时间, 主题, 原因), ...]), ...]。"""
    parts = [
        '<!DOCTYPE html><html><body style="font-family:-apple-system,Segoe UI,sans-serif;'
        'max-width:720px;margin:0 auto;padding:16px;color:#222;">',
        f'<h2 style="margin:6px 0;">📧 每日邮件汇总（{_esc(date_str, 20)} · '
        f'账号 {_esc(account_name, 100)}）</h2>',
    ]
    if digest:
        parts.append('<div style="background:#f0f7ff;border:1px solid #cde3ff;border-radius:6px;'
                     f'padding:10px 12px;font-size:14px;"><strong>AI 摘要：</strong>{_esc(digest, 600)}</div>')
    if not groups:
        parts.append("<p>今日没有低优先级邮件。</p>")
    for sender, items in groups:
        parts.append(f'<h3 style="margin:14px 0 4px;font-size:15px;">'
                     f'{_esc(sender, 320)}（{len(items)} 封）</h3><ul style="font-size:14px;">')
        for t, subj, reason in items:
            reason_part = f" —— {_esc(reason, 100)}" if reason else ""
            parts.append(f"<li>[{_esc(t, 5)}] {_esc(subj, 300)}{reason_part}</li>")
        parts.append("</ul>")
    parts.append('<p style="color:#999;font-size:12px;">本邮件由 MailAgent 自动生成。</p></body></html>')
    return "".join(parts)


class SmtpSession:
    """SMTP 会话（with 语法）：SSL 或 STARTTLS，登录后逐封发送 HTML 邮件。"""

    def __init__(self, account: AccountConfig) -> None:
        self.account = account
        self.smtp: Optional[smtplib.SMTP] = None

    def __enter__(self) -> "SmtpSession":
        acc = self.account
        if acc.smtp_use_ssl:
            self.smtp = smtplib.SMTP_SSL(acc.smtp_host, acc.smtp_port, timeout=30)
        else:
            self.smtp = smtplib.SMTP(acc.smtp_host, acc.smtp_port, timeout=30)
            if acc.smtp_starttls:
                self.smtp.starttls(context=ssl.create_default_context())
        user = acc.smtp_user or acc.imap_user
        password = acc.smtp_password or acc.imap_password
        if user and password:
            self.smtp.login(user, password)
        return self

    def send_html(self, to_addr: str, subject: str, html_body: str) -> None:
        """发送单封 HTML 邮件；失败抛异常，由调用方记账并重试。"""
        if self.smtp is None:
            raise RuntimeError("SMTP 会话未建立")
        from_addr = self.account.notification_from or self.account.smtp_user or self.account.imap_user
        msg = MIMEText(html_body, "html", "utf-8")
        msg["Subject"] = Header(subject, "utf-8")
        msg["From"] = formataddr(("MailAgent", from_addr))
        msg["To"] = to_addr
        self.smtp.sendmail(from_addr, [to_addr], msg.as_string())

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.smtp is not None:
            try:
                self.smtp.quit()
            except Exception:
                pass
            self.smtp = None
