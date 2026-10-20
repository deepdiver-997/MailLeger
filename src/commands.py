"""agent 账号（命令邮箱）的命令解析与鉴权。

全部是纯函数：不触网、不触库，便于单测。约定：
- 主题决定命令：允许 Re:/Fwd:/转发: 等前缀，取第一个 "#词"；手机上转发一封邮件
  只需改主题，且不受 HTML 引用正文干扰；
- 正文第一条分隔线（各客户端的转发/引用标记）之上是指令，之下是素材；
  若带 message/rfc822 附件则优先以它为素材，正文整体视为指令；
- 鉴权只看规范化后的 From 地址是否在 allowed_senders 中。这个 From 之所以可信，
  依赖服务端"公网来信不得携带本域发件人"的边界规则——两边缺一不可；
- 循环守卫：自动生成的邮件（Auto-Submitted / X-Leger-Generated / Precedence:
  bulk|list|junk / 邮件列表）与来自本账号自身的邮件一律忽略。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from email.message import Message
from typing import Optional

from .config import AccountConfig, normalize_address


# ----------------------------------------------------------------------
# 命令目录
# ----------------------------------------------------------------------

@dataclass(frozen=True)
class CommandSpec:
    name: str
    syntax: str
    description: str
    enabled: bool = True        # False：识别但回复"尚未启用"（后续版本接入大模型）
    needs_llm: bool = False
    aliases: tuple[str, ...] = ()


COMMANDS: dict[str, CommandSpec] = {
    "help": CommandSpec("help", "#help", "列出支持的命令与语法"),
    "status": CommandSpec("status", "#status", "今日大模型用量/配额、待处理任务与发送队列"),
    "compose": CommandSpec(
        "compose", "#compose [主题]",
        "按正文中的指令生成一封排版好的邮件；分隔线下方可附参考素材",
        enabled=False, needs_llm=True),
    "summarize": CommandSpec(
        "summarize", "#summarize",
        "总结分隔线下方或 .eml 附件中的邮件素材",
        enabled=False, needs_llm=True),
    "rewrite": CommandSpec(
        "rewrite", "#rewrite [要求]",
        "按要求改写转发来的邮件，默认保留原附件",
        enabled=False, needs_llm=True, aliases=("forward",)),
}

_ALIAS_INDEX = {alias: spec.name for spec in COMMANDS.values() for alias in spec.aliases}


def resolve_command_name(raw: str) -> Optional[str]:
    """把用户写的命令词（含别名）映射为规范命令名；未知返回 None。"""
    key = (raw or "").strip().lower()
    if key in COMMANDS:
        return key
    return _ALIAS_INDEX.get(key)


def command_catalog() -> list[CommandSpec]:
    return list(COMMANDS.values())


# ----------------------------------------------------------------------
# 主题解析
# ----------------------------------------------------------------------

# 常见回复/转发前缀与方括号标签（如 [外部]），可重复出现
_SUBJECT_PREFIX_RE = re.compile(
    r"^\s*(?:(?:re|fw|fwd|aw|wg|转发|回复|答复)\s*[:：]\s*|\[[^\]]{1,30}\]\s*)+",
    re.IGNORECASE)
_COMMAND_RE = re.compile(r"^\s*#\s*([A-Za-z][\w-]*)\b[\s:：,，-]*(.*)$", re.DOTALL)


@dataclass
class SubjectParse:
    raw_name: Optional[str]     # 用户写的命令词（小写）；没写命令则 None
    name: Optional[str]         # 规范命令名；未知命令为 None
    args: str                   # #命令 之后的剩余主题文本
    clean_subject: str          # 去掉前缀与 #命令 后的主题，用于回复主题


def parse_subject(subject: str) -> SubjectParse:
    stripped = _SUBJECT_PREFIX_RE.sub("", (subject or "").strip(), count=1).strip()
    m = _COMMAND_RE.match(stripped)
    if not m:
        return SubjectParse(None, None, "", stripped)
    raw = m.group(1).lower()
    args = m.group(2).strip()
    return SubjectParse(raw, resolve_command_name(raw), args, args)


# ----------------------------------------------------------------------
# 正文切分：指令 / 素材
# ----------------------------------------------------------------------

_DELIMITER_PATTERNS = (
    re.compile(r"^-{3,}\s*$"),                                                  # 用户手写分隔线
    re.compile(r"^-{2,}\s*(forwarded message|original message|原始邮件|原邮件|"
               r"转发的邮件|转发邮件)\b.*$", re.IGNORECASE),                       # Gmail / Outlook / QQ / 网易
    re.compile(r"^begin forwarded message:?\s*$", re.IGNORECASE),               # Apple Mail
    re.compile(r"^下面是被转发的邮件[:：]?\s*$"),                                 # Apple Mail（中文）
    re.compile(r"^on .{3,200} wrote:\s*$", re.IGNORECASE),                      # 回复引用（英文）
    re.compile(r"^在.{3,200}写道[:：]\s*$"),                                      # 回复引用（中文）
    re.compile(r"^>"),                                                          # 引用块起点
)
# Outlook 等不带横线的转发：From/发件人 行后紧跟 Sent/Subject 等头字段
_HEADER_BLOCK_FIRST = re.compile(r"^(from|发件人)\s*[:：]\s*\S", re.IGNORECASE)
_HEADER_BLOCK_FOLLOW = re.compile(
    r"^(sent|date|to|subject|cc|发送时间|日期|时间|收件人|主题|抄送)\s*[:：]", re.IGNORECASE)
_SIGNATURE_RE = re.compile(r"^-- ?$")


def _is_delimiter(lines: list[str], i: int) -> bool:
    s = lines[i].strip()
    if not s:
        return False
    if any(p.match(s) for p in _DELIMITER_PATTERNS):
        return True
    if _HEADER_BLOCK_FIRST.match(s):
        return any(_HEADER_BLOCK_FOLLOW.match(ln.strip()) for ln in lines[i + 1:i + 5])
    return False


def split_body(text: str) -> tuple[str, str]:
    """返回 (指令, 素材)。素材保留分隔线及其后的全部内容；指令去掉末尾签名块。"""
    lines = (text or "").splitlines()
    cut = next((i for i in range(len(lines)) if _is_delimiter(lines, i)), None)
    instruction_lines = lines if cut is None else lines[:cut]
    material_lines = [] if cut is None else lines[cut:]
    for j, line in enumerate(instruction_lines):
        if _SIGNATURE_RE.match(line.rstrip()):
            instruction_lines = instruction_lines[:j]
            break
    return "\n".join(instruction_lines).strip(), "\n".join(material_lines).strip()


def extract_rfc822_material(msg: Message) -> Optional[Message]:
    """取第一个 message/rfc822 附件（"作为附件转发"），没有则 None。"""
    for part in msg.walk():
        if part.get_content_type() == "message/rfc822":
            payload = part.get_payload()
            if isinstance(payload, list) and payload:
                return payload[0]
    return None


# ----------------------------------------------------------------------
# 鉴权与循环守卫
# ----------------------------------------------------------------------

_AUTO_PRECEDENCE = {"bulk", "list", "junk", "auto_reply"}


def _hdr(msg: Message, name: str) -> str:
    try:
        return str(msg.get(name) or "").strip()
    except Exception:
        return ""


def auto_mail_reason(msg: Message, account: AccountConfig) -> Optional[str]:
    """判断是否为自动邮件/自环来信；返回忽略原因，正常邮件返回 None。"""
    auto = _hdr(msg, "Auto-Submitted").lower()
    if auto and auto != "no":
        return f"Auto-Submitted: {auto}"
    if _hdr(msg, "X-Leger-Generated"):
        return "X-Leger-Generated（Leger 自己生成的邮件）"
    precedence = _hdr(msg, "Precedence").lower()
    if precedence in _AUTO_PRECEDENCE:
        return f"Precedence: {precedence}"
    if _hdr(msg, "List-Id") or _hdr(msg, "List-Unsubscribe"):
        return "邮件列表来信"
    sender = normalize_address(_hdr(msg, "From"))
    if not sender:
        return "From 为空"
    if sender in account.self_addresses:
        return "来自本账号自身"
    return None


def is_authorized(from_addr: str, account: AccountConfig) -> bool:
    """白名单精确匹配（规范化后）。"""
    addr = normalize_address(from_addr)
    return bool(addr) and addr in account.allowed_senders


def mask_address(addr: str) -> str:
    """s***a@example.com：status 回复里展示白名单用。"""
    local, _, domain = (addr or "").partition("@")
    if len(local) <= 2:
        masked = "***"
    else:
        masked = f"{local[0]}***{local[-1]}"
    return f"{masked}@{domain}" if domain else masked


# ----------------------------------------------------------------------
# 整封命令邮件的解析结果
# ----------------------------------------------------------------------

@dataclass
class ParsedCommand:
    raw_name: Optional[str]              # 用户写的命令词；未写则 None（此时 name 为 default_command）
    name: Optional[str]                  # 规范命令名；未知命令为 None
    args: str
    clean_subject: str
    instruction: str                     # 分隔线之上的指令文本
    material_text: str                   # 分隔线之下的素材文本（rfc822 附件存在时为空）
    material_message: Optional[Message]  # message/rfc822 附件
    used_default: bool                   # 是否因未写命令而使用 default_command

    @property
    def spec(self) -> Optional[CommandSpec]:
        return COMMANDS.get(self.name) if self.name else None


def parse_command_mail(msg: Message, body_text: str, account: AccountConfig) -> ParsedCommand:
    sp = parse_subject(_hdr(msg, "Subject"))
    rfc822 = extract_rfc822_material(msg)
    if rfc822 is not None:
        instruction, material = (body_text or "").strip(), ""
    else:
        instruction, material = split_body(body_text)
    name, used_default = sp.name, False
    if sp.raw_name is None:
        name, used_default = account.default_command, True
    return ParsedCommand(
        raw_name=sp.raw_name, name=name, args=sp.args, clean_subject=sp.clean_subject,
        instruction=instruction, material_text=material, material_message=rfc822,
        used_default=used_default,
    )
