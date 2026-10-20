"""agent 回复的 MIME 构建与渲染（确定性代码，模型输出绝不直出）。

- help / status / 错误说明：代码模板回复；
- MailSpec（模型产出的 JSON 规格）→ render_spec：表格布局 + 内联 CSS 的邮件 HTML 与纯文本替代版本，
  所有字符串转义、链接只放行 http(s)/mailto，排版质量由模板决定而不依赖模型发挥；
- build_draft_eml：独立的草稿 .eml（From 是请求者本人），作为 message/rfc822 附件回给请求者。
纪律：
- 所有外部字段经 HTML 转义与截断；
- 回复头固定携带 Auto-Submitted / X-Leger-Generated，供循环守卫识别；
- validate_reply 是发送前最后一道闸：单一收件人且等于请求者、无 Cc/Bcc、体积受限。
"""
from __future__ import annotations

import email.utils
import html
import re
from email import message_from_bytes
from email.message import EmailMessage
from email.policy import SMTP, default as default_policy
from typing import Iterable, Optional

from .commands import CommandSpec, mask_address
from .composer import Block, MailSpec, valid_url
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
                references: str = "", command: str = "",
                rfc822_attachments: Optional[list[tuple[str, bytes]]] = None) -> bytes:
    """生成完整的回复 .eml（multipart/alternative，可再带 message/rfc822 附件）。

    线程头与守卫头都在这里统一加；rfc822_attachments 为 [(文件名, 原始 .eml 字节)]。
    """
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
    for filename, raw in rfc822_attachments or []:
        inner = message_from_bytes(raw, policy=SMTP)
        msg.add_attachment(inner, filename=filename)
    return msg.as_bytes()


def build_draft_eml(*, from_addr: str, to_addrs: list[str], subject: str,
                    html_body: str, text_body: str,
                    attachments: Iterable[tuple[str, str, bytes]] = ()) -> bytes:
    """独立草稿：From 是请求者本人，To 来自规格（可为空）。Leger 不会发送它，只作为文件回给请求者。"""
    msg = EmailMessage(policy=SMTP)
    msg["From"] = from_addr
    if to_addrs:
        msg["To"] = ", ".join(to_addrs)
    msg["Subject"] = subject
    msg["Date"] = email.utils.formatdate(localtime=True)
    domain = from_addr.rsplit("@", 1)[-1] if "@" in from_addr else None
    msg["Message-ID"] = email.utils.make_msgid(domain=domain)
    msg["X-Leger-Draft"] = "1"
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")
    for filename, content_type, data in attachments:
        maintype, _, subtype = (content_type or "").lower().partition("/")
        # message/* 与 multipart/* 不能作为字节附件挂载，一律退化为二进制流
        if not subtype or maintype in ("message", "multipart"):
            maintype, subtype = "application", "octet-stream"
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)
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


# ----------------------------------------------------------------------
# MailSpec 渲染：模板调色板 + 块渲染 + 纯文本替代版本
# ----------------------------------------------------------------------

_PALETTES = {
    "card":   dict(page_bg="#f4f5f7", card_bg="#ffffff", accent="#2f6fed", text="#222222",
                   muted="#6b7280", border="#e5e7eb", th_bg="#f3f4f6"),
    "report": dict(page_bg="#ffffff", card_bg="#ffffff", accent="#0f766e", text="#1f2937",
                   muted="#6b7280", border="#d1d5db", th_bg="#f3f4f6"),
    "notice": dict(page_bg="#fffaf0", card_bg="#ffffff", accent="#d97706", text="#222222",
                   muted="#78716c", border="#fcd9a8", th_bg="#fff7ed"),
    "plain":  dict(page_bg="#ffffff", card_bg="#ffffff", accent="#333333", text="#222222",
                   muted="#777777", border="#dddddd", th_bg="#f5f5f5"),
}
_CALLOUT = {
    "info":    ("#eff6ff", "#bfdbfe"),
    "warn":    ("#fff7ed", "#fed7aa"),
    "success": ("#ecfdf5", "#a7f3d0"),
}
_FONT = "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'PingFang SC','Microsoft YaHei',sans-serif;"

_LINK_RE = re.compile(r"\[([^\]\n]{1,200})\]\(((?:https?://|mailto:)[^\s)]{1,500})\)", re.IGNORECASE)
_BOLD_RE = re.compile(r"\*\*([^*\n]{1,300})\*\*")


def _bold(escaped: str) -> str:
    return _BOLD_RE.sub(r"<strong>\1</strong>", escaped)


def inline_html(text: str) -> str:
    """段内标记 → 安全 HTML：先转义再识别 **加粗** 与 [文字](链接)；链接只放行 http(s)/mailto。"""
    out: list[str] = []
    pos = 0
    for m in _LINK_RE.finditer(text):
        out.append(_bold(html.escape(text[pos:m.start()], quote=True)))
        label, url = m.group(1), m.group(2)
        if valid_url(url):
            out.append(f'<a href="{html.escape(url, quote=True)}" style="color:inherit;">'
                       f'{_bold(html.escape(label, quote=True))}</a>')
        else:
            out.append(_bold(html.escape(m.group(0), quote=True)))
        pos = m.end()
    out.append(_bold(html.escape(text[pos:], quote=True)))
    return "".join(out).replace("\n", "<br>")


def inline_text(text: str) -> str:
    """纯文本版本：[文字](链接) → 文字 (链接)，**加粗** → 加粗。"""
    text = _LINK_RE.sub(lambda m: f"{m.group(1)} ({m.group(2)})", text)
    return _BOLD_RE.sub(r"\1", text)


def _render_block(block: Block, pal: dict) -> tuple[str, list[str]]:
    """返回 (html, 纯文本行)。"""
    t = block.type
    if t == "divider":
        return f'<hr style="border:0;border-top:1px solid {pal["border"]};margin:16px 0;">', ["", "-" * 30, ""]
    if t == "heading":
        return (f'<h2 style="font-size:17px;margin:20px 0 8px;color:{pal["text"]};">{inline_html(block.text)}</h2>',
                ["", f"## {inline_text(block.text)}", ""])
    if t == "paragraph":
        return f'<p style="margin:0 0 12px;">{inline_html(block.text)}</p>', [inline_text(block.text), ""]
    if t in ("bullets", "numbered"):
        tag = "ul" if t == "bullets" else "ol"
        items = "".join(f'<li style="margin:4px 0;">{inline_html(i)}</li>' for i in block.items)
        lines = [(f"- {inline_text(i)}" if t == "bullets" else f"{n}. {inline_text(i)}")
                 for n, i in enumerate(block.items, 1)]
        return f'<{tag} style="margin:0 0 12px 22px;padding:0;">{items}</{tag}>', lines + [""]
    if t == "quote":
        return (f'<blockquote style="margin:0 0 12px;padding:8px 14px;border-left:4px solid {pal["accent"]};'
                f'color:{pal["muted"]};background:#fafafa;">{inline_html(block.text)}</blockquote>',
                [f"> {ln}" for ln in inline_text(block.text).splitlines()] + [""])
    if t == "callout":
        bg, border = _CALLOUT.get(block.style, _CALLOUT["info"])
        return (f'<div style="margin:0 0 12px;padding:10px 14px;border:1px solid {border};border-radius:6px;'
                f'background:{bg};">{inline_html(block.text)}</div>',
                [f"[{block.style.upper()}] {inline_text(block.text)}", ""])
    if t == "button":
        return (f'<table role="presentation" cellspacing="0" cellpadding="0" style="margin:8px 0 16px;"><tr>'
                f'<td style="background:{pal["accent"]};border-radius:6px;">'
                f'<a href="{html.escape(block.url, quote=True)}" style="display:inline-block;padding:10px 18px;'
                f'color:#ffffff;text-decoration:none;font-weight:600;">{esc(block.text, 200)}</a></td></tr></table>',
                [f"{block.text}: {block.url}", ""])
    # table
    th = f'style="text-align:left;padding:6px 8px;background:{pal["th_bg"]};border:1px solid {pal["border"]};"'
    td = f'style="padding:6px 8px;border:1px solid {pal["border"]};vertical-align:top;"'
    parts = [f'<table role="presentation" cellspacing="0" cellpadding="0" '
             f'style="border-collapse:collapse;width:100%;margin:0 0 14px;font-size:14px;">']
    lines: list[str] = []
    if block.header:
        parts.append("<tr>" + "".join(f"<th {th}>{inline_html(h)}</th>" for h in block.header) + "</tr>")
        lines.append(" | ".join(inline_text(h) for h in block.header))
        lines.append("-" * min(60, max(10, len(lines[-1]))))
    for row in block.rows:
        parts.append("<tr>" + "".join(f"<td {td}>{inline_html(c)}</td>" for c in row) + "</tr>")
        lines.append(" | ".join(inline_text(c) for c in row))
    parts.append("</table>")
    return "".join(parts), lines + [""]


def render_spec(spec: MailSpec, preface: Optional[str] = None) -> tuple[str, str]:
    """MailSpec → (html, text)。preface 是放在顶部的灰色说明行（回复给请求者时用，草稿本体不带）。"""
    pal = _PALETTES.get(spec.template, _PALETTES["card"])
    body_parts: list[str] = []
    text_lines: list[str] = []
    if preface:
        body_parts.append(f'<p style="margin:0 0 14px;padding:8px 12px;font-size:12px;color:{pal["muted"]};'
                          f'background:{pal["page_bg"]};border:1px dashed {pal["border"]};border-radius:6px;">'
                          f'{esc(preface, 500)}</p>')
        text_lines += [preface, "=" * 30, ""]
    body_parts.append(f'<h1 style="font-size:20px;margin:0 0 16px;padding-bottom:10px;'
                      f'border-bottom:3px solid {pal["accent"]};color:{pal["text"]};">{esc(spec.subject, 200)}</h1>')
    text_lines += [spec.subject, "=" * min(60, max(10, len(spec.subject))), ""]
    for block in spec.blocks:
        block_html, block_lines = _render_block(block, pal)
        body_parts.append(block_html)
        text_lines.extend(block_lines)

    html_out = (
        '<!DOCTYPE html><html><body style="margin:0;padding:0;background:' + pal["page_bg"] + ';">'
        '<table role="presentation" width="100%" cellspacing="0" cellpadding="0"><tr>'
        '<td align="center" style="padding:24px 12px;">'
        f'<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="max-width:640px;'
        f'background:{pal["card_bg"]};border:1px solid {pal["border"]};border-radius:8px;"><tr>'
        f'<td style="padding:24px 28px;{_FONT}color:{pal["text"]};font-size:15px;line-height:1.6;">'
        + "".join(body_parts) +
        '</td></tr></table></td></tr></table></body></html>'
    )
    while text_lines and not text_lines[-1]:
        text_lines.pop()
    return html_out, "\n".join(text_lines)
