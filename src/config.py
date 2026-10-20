"""配置加载模块。

职责：
- 读取 YAML 配置并展开 ${ENV_VAR} 形式的环境变量引用；
- 自动加载 .env 文件（不覆盖已有环境变量）；
- 解析为类型化 dataclass，启动时完整校验（fail-fast）。
"""
from __future__ import annotations

import email.utils
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

ACCOUNT_ROLES = ("monitor", "agent")
AGENT_DEFAULT_COMMANDS = ("help", "compose")


class ConfigError(Exception):
    """配置缺失或非法。"""


def normalize_address(value: str | None) -> str:
    """把 From 头/配置里的地址规范化为小写纯邮箱（去显示名、去空白），用于白名单比对。"""
    addr = email.utils.parseaddr(value or "")[1] or (value or "")
    return addr.strip().lower()


DEFAULT_CONFIG_TEMPLATE = """\
# =====================================================================
# MailAgent 配置文件（mailagent init 自动生成）
# - 密码等敏感信息建议用 ${ENV_VAR} 引用环境变量（配合 .env 文件）；
# - 相对路径 database_path / log_file 相对于运行命令时所在目录解析；
# - accounts 为列表，可添加多个账号，scan/watch 会逐账号独立扫描。
# =====================================================================

database_path: data/mailagent.db
log_file: logs/mailagent.log
log_level: INFO

# watch 模式的轮询间隔（秒）
scan_interval_seconds: 300

# 正文超过该字符数则不调用大模型，直接发送“请人工查看”模板通知
max_body_chars: 2000
# 提取正文时的硬上限字符数（防止超大邮件占用内存）
max_extract_chars: 20000
# 通知邮件中“判断原因”字段最大长度（字符）
reason_max_chars: 100
# outbox 单封邮件最大发送重试次数
max_send_retries: 50
# 每日汇总调用大模型生成摘要的输入上限（字符），超过则只输出分组列表
summary_llm_max_chars: 4000

# 大模型（OpenAI 兼容 API）
llm:
  base_url: https://api.openai.com/v1
  api_key: ${OPENAI_API_KEY}
  model_name: gpt-4o-mini
  max_tokens: 100
  timeout_seconds: 30
  max_retries: 2
  # API 支持 JSON 模式时开启；不支持会自动降级为纯提示词约束
  json_mode: true
  # agent 生成类命令（#compose / #summarize）：输出远比分类长，单独配置模型与预算
  generate_model_name: ""
  generate_max_tokens: 2000
  generate_timeout_seconds: 90

accounts:
  - name: main
    imap_host: imap.example.com
    imap_port: 993
    imap_use_ssl: true
    imap_user: you@example.com
    imap_password: ${IMAP_PASSWORD}

    smtp_host: smtp.example.com
    smtp_port: 465
    smtp_use_ssl: true        # false 时配合 smtp_starttls 使用 STARTTLS
    smtp_starttls: false
    smtp_user: you@example.com
    smtp_password: ${SMTP_PASSWORD}

    # 重要邮件通知收件箱：内部域名专属邮箱（本系统 SMTP 只能对内投递）
    notification_to: you@internal.example.com
    # 通知邮件的发件地址，留空则取 smtp_user
    notification_from: mailagent@internal.example.com

    # 每日汇总时间（本地时间 HH:MM）
    daily_summary_time: "21:00"

    # 每账号每日大模型配额
    daily_max_calls: 500
    daily_max_tokens: 1000000

    # 处理完成后对服务器邮件的动作：mark_seen 或 move（移动到 processed_folder）
    on_processed: mark_seen
    processed_folder: Processed

  # ---------------------------------------------------------------------
  # 命令邮箱（role: agent）：只响应 allowed_senders 发来的 #命令 邮件，
  # 结果只会回给发出命令的人；非白名单来信静默丢弃、不回、不弹。
  # ---------------------------------------------------------------------
  # - name: leger
  #   role: agent
  #   imap_host: imap.example.com
  #   imap_user: leger@example.com
  #   imap_password: ${LEGER_IMAP_PASSWORD}
  #   smtp_host: smtp.example.com
  #   smtp_user: leger@example.com
  #   smtp_password: ${LEGER_SMTP_PASSWORD}
  #   allowed_senders: [you@example.com]
  #   reply_display_name: Leger
  #   default_command: help          # 白名单来信但没带 #命令 时：help | compose
  #   max_command_attempts: 3        # 需要大模型的命令连续失败 N 次后回错误说明并终结
  #   on_processed: move
  #   processed_folder: Done
"""


def load_dotenv(path: str | Path) -> None:
    """极简 .env 解析：每行 KEY=VALUE，支持 # 注释；不覆盖已存在的环境变量。"""
    p = Path(path)
    if not p.is_file():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _expand_env(value: Any) -> Any:
    """递归展开字符串中的 ${VAR}；未定义变量替换为空串并记录警告。"""
    if isinstance(value, str):
        def _repl(m: re.Match[str]) -> str:
            var = m.group(1)
            val = os.environ.get(var)
            if val is None:
                logger.warning("环境变量 %s 未定义，配置中 ${%s} 将替换为空串", var, var)
                return ""
            return val
        return _ENV_PATTERN.sub(_repl, value)
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    return value


def _get_str(d: dict, key: str, default: str = "") -> str:
    v = d.get(key, default)
    return default if v is None else str(v)


def _get_int(d: dict, key: str, default: int) -> int:
    try:
        return int(d.get(key, default))
    except (TypeError, ValueError):
        raise ConfigError(f"字段 {key} 必须是整数，得到: {d.get(key)!r}") from None


def _get_bool(d: dict, key: str, default: bool) -> bool:
    v = d.get(key, default)
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)


@dataclass
class LLMConfig:
    base_url: str = "https://api.openai.com/v1"
    api_key: str = ""
    model_name: str = "gpt-4o-mini"
    max_tokens: int = 100
    timeout_seconds: int = 30
    max_retries: int = 2
    json_mode: bool = True
    # --- agent 生成任务（#compose / #summarize）专用：输出远比分类长 ---
    generate_model_name: str = ""          # 留空同 model_name；可指定更强的模型
    generate_max_tokens: int = 2000
    generate_timeout_seconds: int = 90

    @property
    def generate_model(self) -> str:
        return self.generate_model_name or self.model_name


@dataclass
class AccountConfig:
    name: str
    imap_host: str
    imap_port: int = 993
    imap_use_ssl: bool = True
    imap_user: str = ""
    imap_password: str = ""
    smtp_host: str = ""
    smtp_port: int = 465
    smtp_use_ssl: bool = True
    smtp_starttls: bool = False
    smtp_user: str = ""
    smtp_password: str = ""
    notification_to: str = ""
    notification_from: str = ""
    daily_summary_time: str = "21:00"
    daily_max_calls: int = 500
    daily_max_tokens: int = 1_000_000
    on_processed: str = "mark_seen"       # mark_seen | move
    processed_folder: str = "Processed"
    # --- role: agent（命令邮箱）专用 ---
    role: str = "monitor"                 # monitor | agent
    allowed_senders: list[str] = field(default_factory=list)   # 已规范化为小写纯地址
    reply_display_name: str = "Leger"
    default_command: str = "help"         # 白名单来信但没带 #命令 时的处理：help | compose
    max_command_attempts: int = 3         # 需要大模型的命令连续失败 N 次后回错误说明并终结
    max_material_chars: int = 12000       # 交给大模型的素材上限（字符）
    max_attachment_mb: int = 10           # 单个附件上限
    max_mime_mb: int = 25                 # 生成邮件总体积上限

    @property
    def self_addresses(self) -> set[str]:
        """本账号自己的所有地址（规范化），用于防自环与白名单互斥校验。"""
        return {normalize_address(a) for a in (self.imap_user, self.smtp_user, self.notification_from) if a}

    @property
    def reply_from(self) -> str:
        """agent 回复 / monitor 通知使用的发件地址。"""
        return self.notification_from or self.smtp_user or self.imap_user


@dataclass
class AppConfig:
    config_path: Path
    database_path: Path
    log_file: Path
    log_level: str = "INFO"
    scan_interval_seconds: int = 300
    max_body_chars: int = 2000
    max_extract_chars: int = 20000
    reason_max_chars: int = 100
    max_send_retries: int = 50
    summary_llm_max_chars: int = 4000
    llm: LLMConfig = field(default_factory=LLMConfig)
    accounts: list[AccountConfig] = field(default_factory=list)

    @property
    def raw_mail_dir(self) -> Path:
        """agent 账号原始 MIME 的落盘目录（与数据库同级，崩溃恢复时不必重拉）。"""
        return self.database_path.parent / "raw"

    @property
    def outbox_dir(self) -> Path:
        """agent 回复生成的 .eml 文件目录，outbox.raw_path 指向这里。"""
        return self.database_path.parent / "outbox"


def _parse_str_list(raw: dict, key: str) -> list[str]:
    """允许 YAML 列表或逗号分隔字符串；返回规范化、去重后的地址列表。"""
    v = raw.get(key)
    if v is None:
        return []
    items = v if isinstance(v, list) else str(v).split(",")
    out: list[str] = []
    for item in items:
        addr = normalize_address(str(item))
        if addr and addr not in out:
            out.append(addr)
    return out


def _parse_account(raw: dict, index: int) -> AccountConfig:
    cfg = AccountConfig(
        name=_get_str(raw, "name"),
        imap_host=_get_str(raw, "imap_host"),
        imap_port=_get_int(raw, "imap_port", 993),
        imap_use_ssl=_get_bool(raw, "imap_use_ssl", True),
        imap_user=_get_str(raw, "imap_user"),
        imap_password=_get_str(raw, "imap_password"),
        smtp_host=_get_str(raw, "smtp_host"),
        smtp_port=_get_int(raw, "smtp_port", 465),
        smtp_use_ssl=_get_bool(raw, "smtp_use_ssl", True),
        smtp_starttls=_get_bool(raw, "smtp_starttls", False),
        smtp_user=_get_str(raw, "smtp_user"),
        smtp_password=_get_str(raw, "smtp_password"),
        notification_to=_get_str(raw, "notification_to"),
        notification_from=_get_str(raw, "notification_from"),
        daily_summary_time=_get_str(raw, "daily_summary_time", "21:00"),
        daily_max_calls=_get_int(raw, "daily_max_calls", 500),
        daily_max_tokens=_get_int(raw, "daily_max_tokens", 1_000_000),
        on_processed=_get_str(raw, "on_processed", "mark_seen"),
        processed_folder=_get_str(raw, "processed_folder", "Processed"),
        role=_get_str(raw, "role", "monitor").strip().lower(),
        allowed_senders=_parse_str_list(raw, "allowed_senders"),
        reply_display_name=_get_str(raw, "reply_display_name", "Leger"),
        default_command=_get_str(raw, "default_command", "help").strip().lower().lstrip("#"),
        max_command_attempts=_get_int(raw, "max_command_attempts", 3),
        max_material_chars=_get_int(raw, "max_material_chars", 12000),
        max_attachment_mb=_get_int(raw, "max_attachment_mb", 10),
        max_mime_mb=_get_int(raw, "max_mime_mb", 25),
    )
    if cfg.role not in ACCOUNT_ROLES:
        raise ConfigError(f"accounts[{index}].role 只能是 {' 或 '.join(ACCOUNT_ROLES)}，得到: {cfg.role!r}")
    required = ["name", "imap_host", "imap_user"]
    if cfg.role == "monitor":
        required.append("notification_to")   # agent 的回复只发给请求者，不需要通知邮箱
    missing = [f for f in required if not getattr(cfg, f)]
    if missing:
        raise ConfigError(f"accounts[{index}] 缺少必填字段: {', '.join(missing)}")
    if cfg.role == "agent":
        if not cfg.allowed_senders:
            raise ConfigError(f"accounts[{index}] 是 agent 角色，必须配置非空的 allowed_senders 白名单")
        if not cfg.smtp_host:
            raise ConfigError(f"accounts[{index}] 是 agent 角色，必须配置 smtp_host 才能回复命令")
        overlap = cfg.self_addresses & set(cfg.allowed_senders)
        if overlap:
            raise ConfigError(
                f"accounts[{index}].allowed_senders 不能包含本账号自己的地址（防止自环）: {', '.join(sorted(overlap))}")
        if cfg.default_command not in AGENT_DEFAULT_COMMANDS:
            raise ConfigError(
                f"accounts[{index}].default_command 只能是 {' 或 '.join(AGENT_DEFAULT_COMMANDS)}，得到: {cfg.default_command!r}")
        if cfg.max_command_attempts < 1:
            raise ConfigError(f"accounts[{index}].max_command_attempts 必须 ≥ 1")
    if cfg.on_processed not in ("mark_seen", "move"):
        raise ConfigError(f"accounts[{index}].on_processed 只能是 mark_seen 或 move")
    try:
        datetime.strptime(cfg.daily_summary_time, "%H:%M")
    except ValueError:
        raise ConfigError(
            f"accounts[{index}].daily_summary_time 格式应为 HH:MM，得到: {cfg.daily_summary_time!r}"
        ) from None
    return cfg


def _resolve_path(value: str) -> Path:
    p = Path(value).expanduser()
    return p if p.is_absolute() else Path.cwd() / p


def load_config(path: str | Path) -> AppConfig:
    """加载并校验配置文件。相对路径相对于当前工作目录解析。"""
    cfg_path = Path(path).expanduser()
    if not cfg_path.is_file():
        raise ConfigError(f"配置文件不存在: {cfg_path}（可先运行 `python -m src.cli init` 生成）")

    # 依次尝试常见位置的 .env（工作目录 / 配置文件目录 / 其上级目录）
    for cand in (Path.cwd() / ".env", cfg_path.parent / ".env", cfg_path.parent.parent / ".env"):
        load_dotenv(cand)

    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ConfigError("配置文件顶层必须是键值映射")
    raw = _expand_env(raw)

    llm_raw = raw.get("llm") or {}
    llm = LLMConfig(
        base_url=_get_str(llm_raw, "base_url", "https://api.openai.com/v1"),
        api_key=_get_str(llm_raw, "api_key"),
        model_name=_get_str(llm_raw, "model_name", "gpt-4o-mini"),
        max_tokens=_get_int(llm_raw, "max_tokens", 100),
        timeout_seconds=_get_int(llm_raw, "timeout_seconds", 30),
        max_retries=_get_int(llm_raw, "max_retries", 2),
        json_mode=_get_bool(llm_raw, "json_mode", True),
        generate_model_name=_get_str(llm_raw, "generate_model_name", ""),
        generate_max_tokens=_get_int(llm_raw, "generate_max_tokens", 2000),
        generate_timeout_seconds=_get_int(llm_raw, "generate_timeout_seconds", 90),
    )

    accounts_raw = raw.get("accounts")
    if not isinstance(accounts_raw, list) or not accounts_raw:
        raise ConfigError("配置中至少需要一个 accounts 条目")
    accounts = [_parse_account(a, i) for i, a in enumerate(accounts_raw)]

    cfg = AppConfig(
        config_path=cfg_path,
        database_path=_resolve_path(_get_str(raw, "database_path", "data/mailagent.db")),
        log_file=_resolve_path(_get_str(raw, "log_file", "logs/mailagent.log")),
        log_level=_get_str(raw, "log_level", "INFO").upper(),
        scan_interval_seconds=_get_int(raw, "scan_interval_seconds", 300),
        max_body_chars=_get_int(raw, "max_body_chars", 2000),
        max_extract_chars=_get_int(raw, "max_extract_chars", 20000),
        reason_max_chars=_get_int(raw, "reason_max_chars", 100),
        max_send_retries=_get_int(raw, "max_send_retries", 50),
        summary_llm_max_chars=_get_int(raw, "summary_llm_max_chars", 4000),
        llm=llm,
        accounts=accounts,
    )
    if not llm.api_key:
        logger.warning("llm.api_key 为空：规则预筛选仍可用，未命中规则的邮件将收到“未分类”模板通知")
    for acc in accounts:
        if not acc.imap_password:
            logger.warning("账号 %s 的 imap_password 为空，IMAP 登录可能失败", acc.name)
    return cfg


def init_config_file(path: str | Path) -> bool:
    """在 path 生成默认配置文件；已存在则不覆盖。返回是否新建。"""
    p = Path(path).expanduser()
    if p.is_file():
        return False
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(DEFAULT_CONFIG_TEMPLATE, encoding="utf-8")
    return True
