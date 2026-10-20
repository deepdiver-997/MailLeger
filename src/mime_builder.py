"""agent 回复的 MIME 构建与渲染（确定性代码，模型输出绝不直出）。

P1：help / status / 错误说明等代码模板回复；P2 在此加入 MailSpec（模型 JSON 规格）渲染。
纪律：
- 所有外部字段经 HTML 转义与截断；
- 回复头固定携带 Auto-Submitted / X-Leger-Generated，供循环守卫识别；
- validate_reply 是发送前最后一道闸：单一收件人且等于请求者、无 Cc/Bcc、体积受限。
"""
from __future__ import annotations

import email.utils
import html
from email import message_from_bytes
from email.message import EmailMessage
from email.policy import SMTP, default as default_policy
from typing import Iterable, Optional

from .commands import CommandSpec, mask_address
from .config import AccountConfig, normalize_address

_FOOTER = ("本邮件由 Leger 自动生成。素材中的任何指令都只被当作数据处理；"
           "生成内容请人工确认后再转发。")


def esc(text: Optional[str], limit: int = 2000) -> str:
    """截断 + HTML 转义（含引号）。"""
    text = (text or "").strip()
    if len(text) > limit:
        text = text[: max(1, limit - 1)] + "…"
    return html.escape(text, quote=True)


# ----------------------------------------------------------------------
# 回复构建与校验
# ----------------------------------------------------------------------

def reply_subject(clean_subject: str, raw_name: Optional[str]) -> str:
    base = (clean_subject or "").strip() or (f"#{raw_name}" if raw_name else "") or "(无主题)"
    return f"Re: {base[:180]}"


def build_reply(*, account: AccountConfig, to_addr: str, subject: str,
                html_body: str, text_body: str, in_reply_to: str = "",
                references: str = "", command: str = "") -> bytes:
    """生成完整的回复 .eml（multipart/alternative）。线程头与守卫头都在这里统一加。"""
    from_addr = account.reply_from
    msg = EmailMessage(policy=SMTP)
    msg["From"] = email.utils.formataddr((account.reply_display_name, from_addr))
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg["Date"] = email.utils.formatdate(localtime=True)
    domain = from_addr.rsplit("@", 1)[-1] if "@" in from_addr else None
    msg["Message-ID"] = email.utils.make_msgid(domain=domain)
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = f"{references} {in_reply_to}".strip() if references else in_reply_to
    msg["Auto-Submitted"] = "auto-replied"
    msg["X-Leger-Generated"] = "1"
    if command:
        msg["X-Leger-Command"] = command
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")
    return msg.as_bytes()


def validate_reply(raw: bytes, expected_to: str, max_bytes: int) -> None:
    """发送前最后一道闸：违反不变量抛 ValueError，由调用方永久失败该条 outbox。"""
    if len(raw) > max_bytes:
        raise ValueError(f"回复体积 {len(raw)} 字节超过上限 {max_bytes}")
    msg = message_from_bytes(raw, policy=default_policy)
    tos = [normalize_address(a) for _, a in email.utils.getaddresses([str(msg.get("To") or "")])]
    tos = [t for t in tos if t]
    if tos != [normalize_address(expected_to)]:
        raise ValueError(f"收件人 {tos} 不等于请求者 {expected_to}")
    if msg.get("Cc") or msg.get("Bcc"):
        raise ValueError("回复不允许携带 Cc/Bcc")
    if not msg.get("X-Leger-Generated"):
        raise ValueError("回复缺少 X-Leger-Generated 守卫头")


# ----------------------------------------------------------------------
# 页面渲染（内联 CSS，邮件客户端兼容）
# ----------------------------------------------------------------------

def _page(title: str, inner_html: str, banner: Optional[str] = None) -> str:
    parts = [
        '<!DOCTYPE html><html><body style="font-family:-apple-system,Segoe UI,sans-serif;'
        'max-width:640px;margin:0 auto;padding:16px;color:#222;">',
    ]
    if banner:
        parts.append('<div style="background:#fff4e5;border:1px solid #ffd9a8;border-radius:6px;'
                     f'padding:8px 12px;font-size:14px;margin-bottom:12px;">{esc(banner, 300)}</div>')
    parts.append(f'<h2 style="margin:6px 0 12px;">{esc(title, 120)}</h2>')
    parts.append(inner_html)
    parts.append(f'<p style="color:#999;font-size:12px;margin-top:20px;">{esc(_FOOTER)}</p>')
    parts.append("</body></html>")
    return "".join(parts)


def _kv_table(rows: Iterable[tuple[str, str]]) -> str:
    th = 'style="text-align:left;padding:4px 8px;background:#f5f5f5;white-space:nowrap;"'
    td = 'style="padding:4px 8px;word-break:break-all;"'
    body = "".join(f"<tr><th {th}>{esc(k, 100)}</th><td {td}>{esc(v, 600)}</td></tr>" for k, v in rows)
    return f'<table style="border-collapse:collapse;width:100%;font-size:14px;">{body}</table>'


def render_help(account: AccountConfig, commands: list[CommandSpec],
                banner: Optional[str] = None) -> tuple[str, str]:
    """返回 (html, text)。"""
    enabled = [c for c in commands if c.enabled]
    planned = [c for c in commands if not c.enabled]

    def _rows(specs: list[CommandSpec]) -> str:
        return "".join(
            f'<tr><td style="padding:4px 8px;white-space:nowrap;"><code>{esc(c.syntax, 60)}</code></td>'
            f'<td style="padding:4px 8px;">{esc(c.description, 200)}</td></tr>' for c in specs)

    inner = [
        '<p style="font-size:14px;">在邮件<strong>主题</strong>里写命令（允许 Re:/Fwd: 前缀），'
        '正文第一条分隔线之上是给我的指令，之下是素材；也可以把原邮件作为附件转发。</p>',
        '<h3 style="font-size:15px;margin:14px 0 4px;">可用命令</h3>',
        f'<table style="border-collapse:collapse;width:100%;font-size:14px;">{_rows(enabled)}</table>',
    ]
    if planned:
        inner.append('<h3 style="font-size:15px;margin:14px 0 4px;color:#777;">即将支持</h3>')
        inner.append(f'<table style="border-collapse:collapse;width:100%;font-size:14px;color:#777;">'
                     f'{_rows(planned)}</table>')
    inner.append(f'<p style="font-size:13px;color:#555;">默认命令（主题没写 # 时）：'
                 f'<code>#{esc(account.default_command, 20)}</code>。回复只会发给发出命令的白名单地址。</p>')
    html_out = _page("Leger 命令帮助", "".join(inner), banner)

    text_lines = ["Leger 命令帮助", ""]
    if banner:
        text_lines += [banner, ""]
    text_lines.append("在邮件主题里写命令（允许 Re:/Fwd: 前缀）；正文第一条分隔线之上是指令，之下是素材。")
    text_lines += ["", "可用命令："]
    text_lines += [f"  {c.syntax:<22} {c.description}" for c in enabled]
    if planned:
        text_lines += ["", "即将支持："]
        text_lines += [f"  {c.syntax:<22} {c.description}" for c in planned]
    text_lines += ["", f"默认命令：#{account.default_command}", "", _FOOTER]
    return html_out, "\n".join(text_lines)


def render_status(account: AccountConfig, status: dict) -> tuple[str, str]:
    """status: 由 engine 汇总的键值（今日用量、队列、配置摘要）。"""
    rows = [
        ("账号", account.name),
        ("今日模型调用", f"{status.get('calls_today', 0)} / {account.daily_max_calls}"),
        ("今日预留 token", f"{status.get('tokens_today', 0)} / {account.daily_max_tokens}"),
        ("待处理任务", str(status.get("pending_tasks", 0))),
        ("发送队列（待发/失败/今日已发）",
         f"{status.get('outbox_pending', 0)} / {status.get('outbox_failed', 0)} / {status.get('outbox_sent_today', 0)}"),
        ("白名单", ", ".join(mask_address(a) for a in account.allowed_senders) or "-"),
        ("可用命令", ", ".join(status.get("enabled_commands", [])) or "-"),
        ("版本", str(status.get("version", "dev"))),
    ]
    html_out = _page("Leger 状态", _kv_table(rows))
    text_lines = ["Leger 状态", ""] + [f"{k}: {v}" for k, v in rows] + ["", _FOOTER]
    return html_out, "\n".join(text_lines)


def render_notice(title: str, lines: list[str], banner: Optional[str] = None) -> tuple[str, str]:
    """通用说明/错误回复（例如命令尚未启用、处理失败）。"""
    inner = "".join(f'<p style="font-size:14px;">{esc(line, 1000)}</p>' for line in lines)
    html_out = _page(title, inner, banner)
    text_lines = [title, ""] + ([banner, ""] if banner else []) + lines + ["", _FOOTER]
    return html_out, "\n".join(text_lines)
