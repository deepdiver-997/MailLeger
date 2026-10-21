"""核心编排模块（引擎）：扫描 → 预写任务 → 逐条处理 → 通知入队 → 发送。

事务/崩溃恢复流程（write-ahead，每封邮件）：
1. SEARCH UNSEEN 拿 UID 列表（全程 BODY.PEEK，不改变服务器状态）；
2. UID + 元数据以 pending 状态先写入 scan_tasks（预写日志）；
3. 逐条处理 pending：取正文（缓存进 raw_json）→ 规则预筛选 → 长度检查 →
   配额检查 → 大模型分类 → 得出决策；
4. 决策持久化：高优先级写入 outbox（dedup_key 唯一约束保证幂等），
   随后写 processed_messages 去重表、scan_tasks 置为 processed；
5. 以上全部落库成功后，才把服务器邮件标记已读/移动到 Processed；
6. 启动/每次扫描都会重新处理遗留 pending 任务（at-least-once），
   outbox 的 dedup_key 与 processed_messages 保证通知不重复发送。

agent 账号（role: agent）在步骤 3 分流到 _process_command：鉴权 → 解析 #命令 → 执行 →
回复只发给请求者（outbox 的 to_addr 必须在白名单内，send_outbox 再校验一次，违反即永久失败）。
"""
from __future__ import annotations

import email
import email.policy
import hashlib
import imaplib
import json
import logging
import re
import secrets
import smtplib
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.message import Message
from pathlib import Path
from typing import Optional

from . import __version__
from .classifier import CLASSIFY_SYSTEM_PROMPT, LLMClassifier, LLMError, rule_classify
from .commands import (ParsedCommand, auto_mail_reason, command_catalog, is_authorized,
                       material_text_for_llm, parse_command_mail)
from .composer import AttachmentInfo, Composer, MailSpec
from .config import AccountConfig, AppConfig, normalize_address
from .db import Database, now_str, today_str
from .imap_worker import ImapClient, extract_text_body, imap_utf7_encode
from .mailer import (SmtpSession, build_notification_html, build_summary_html,
                     extract_first_url, truncate)
from .mime_builder import (build_draft_eml, build_reply, render_help, render_notice, render_spec,
                           render_status, reply_subject, validate_reply)

logger = logging.getLogger(__name__)


class DeadLetter(RuntimeError):
    """outbox 条目违反不变量或文件丢失：永久失败，不再重试。"""


# 两步确认发送（#send-as）：确认码 / 有效期 / 错误次数上限
CONFIRM_TTL_MIN = 60
MAX_CONFIRM_ATTEMPTS = 3
_CODE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"   # 去掉易混的 0/O/1/I/L


def _confirm_code() -> str:
    return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(6))


def _code_hash(requester: str, code: str) -> str:
    return hashlib.sha256(f"{requester.lower()}:{code.upper()}".encode()).hexdigest()


@dataclass
class CommandReply:
    """一条命令的回复内容；draft 非空时作为 message/rfc822 附件（draft.eml）一并回给请求者。"""
    subject: str
    html: str
    text: str
    draft: Optional[tuple[str, bytes]] = None


@dataclass
class Decision:
    """一封邮件的处理决策。"""
    priority: str   # "high" | "low"
    reason: str     # 判断原因（已按 reason_max_chars 截断）
    banner: str     # 通知邮件顶部提示语（仅高优先级/人工查看类使用）
    outcome: str    # notified / low / dedup / too_long / quota / no_llm


class Engine:
    def __init__(self, cfg: AppConfig, db: Database) -> None:
        self.cfg = cfg
        self.db = db
        self.classifier = LLMClassifier(cfg.llm)
        self.composer = Composer(cfg.llm, self.classifier)

    # ------------------------------------------------------------------
    # 扫描入口
    # ------------------------------------------------------------------
    def run_account_scan(self, account: AccountConfig) -> dict[str, int]:
        """对单个账号执行一次“扫描 + 处理 pending + 发送到期通知”。"""
        account_id = self.db.upsert_account(account)
        stats: dict[str, int] = {"new_tasks": 0, "notified": 0, "low": 0, "dedup": 0,
                                 "too_long": 0, "quota": 0, "no_llm": 0, "retry_later": 0,
                                 "command": 0, "unauthorized": 0, "ignored": 0}
        try:
            with ImapClient(account) as imap:
                uids = imap.search_unseen()
                logger.info("账号 %s：UNSEEN 邮件 %d 封", account.name, len(uids))

                # 步骤 2：预写 pending 任务（唯一键 account_id+uid 幂等）
                for meta in imap.fetch_headers(uids):
                    raw_json = json.dumps({
                        "uid": meta.uid,
                        "message_id": meta.message_id,
                        "from": meta.from_display,
                        "from_addr": meta.from_addr,
                        "subject": meta.subject,
                        "date": meta.date,
                        "body_text": None,
                    }, ensure_ascii=False)
                    if self.db.insert_scan_task(
                        account_id=account_id,
                        email_uid=meta.uid,
                        message_id=meta.message_id,
                        from_addr=meta.from_addr,
                        subject=meta.subject,
                        email_date=meta.date,
                        raw_json=raw_json,
                    ):
                        stats["new_tasks"] += 1

                # 步骤 3：处理全部 pending（含上次运行遗留的，即崩溃恢复）
                for task in self.db.get_pending_tasks(account_id):
                    try:
                        ok, outcome = self._process_task(account, account_id, task, imap)
                    except Exception:
                        logger.exception("处理任务 #%s 时发生未预期异常，保持 pending", task["id"])
                        ok, outcome = False, ""
                    if ok:
                        if outcome in stats:
                            stats[outcome] += 1
                    else:
                        stats["retry_later"] += 1
        except Exception as e:
            logger.error("账号 %s IMAP 扫描失败：%s", account.name, e)
        finally:
            self.send_outbox(account)
        return stats

    # ------------------------------------------------------------------
    # 单封邮件处理
    # ------------------------------------------------------------------
    def _process_task(self, account: AccountConfig, account_id: int,
                      task: sqlite3.Row, imap: ImapClient) -> tuple[bool, str]:
        """处理单封邮件。返回 (是否完成, 结果类别)；大模型失败返回 False 保持 pending。"""
        message_id = (task["message_id"] or "").strip() or f"no-message-id:{account_id}:{task['email_uid']}"

        # Message-ID 去重：此前已处理过的邮件直接终结，不重复通知
        if self.db.is_message_processed(account_id, message_id):
            self.db.finish_task(task["id"], task["priority"] or "low", "去重：该邮件此前已处理")
            self._mark_processed_on_server(imap, account, task["email_uid"])
            return True, "dedup"

        if account.role == "agent":
            return self._process_command(account, account_id, task, imap, message_id)

        try:
            body = self._load_body(task, imap)
        except Exception as e:
            logger.warning("拉取邮件正文失败（保持 pending 待重试）：uid=%s，%s", task["email_uid"], e)
            return False, ""

        decision = self._decide(account, account_id, task, body)
        if decision is None:  # 大模型调用失败：任务保持 pending，等待下次重试
            return False, ""

        # 步骤 4：先写 outbox（幂等），再写去重表与完成状态
        if decision.priority == "high":
            self._enqueue_notification(account, account_id, task, decision, message_id, body)

        self.db.mark_message_processed(account_id, message_id)
        self.db.finish_task(task["id"], decision.priority, decision.reason)

        # 步骤 5：本地状态全部落库后，才改服务器状态
        self._mark_processed_on_server(imap, account, task["email_uid"])
        return True, decision.outcome

    def _load_body(self, task: sqlite3.Row, imap: ImapClient) -> str:
        """优先读取 raw_json 缓存的正文；否则 FETCH 正文并缓存（截断到硬上限）。"""
        try:
            raw = json.loads(task["raw_json"] or "{}")
        except json.JSONDecodeError:
            raw = {}
        body = raw.get("body_text")
        if body is None:
            msg = imap.fetch_message(task["email_uid"])
            body = extract_text_body(msg, self.cfg.max_extract_chars)
            raw["body_text"] = body
            self.db.update_task_body(task["id"], json.dumps(raw, ensure_ascii=False))
        return body or ""

    def _decide(self, account: AccountConfig, account_id: int,
                task: sqlite3.Row, body: str) -> Decision | None:
        """决策链：规则 → 长度 → 配额 → 大模型。大模型失败返回 None（保持 pending）。"""
        from_addr = task["from_addr"] or ""
        subject = task["subject"] or ""
        reason_max = self.cfg.reason_max_chars

        # 1) 规则预筛选：命中则不消耗模型调用
        rule = rule_classify(from_addr, subject)
        if rule is not None:
            return Decision("high", truncate(rule.reason, reason_max), "⚠️ 重要邮件（规则命中）", "notified")

        # 2) 正文超过阈值：不调用模型，发固定模板提醒人工查看
        if len(body) > self.cfg.max_body_chars:
            return Decision(
                "high",
                f"邮件过长（超过{self.cfg.max_body_chars}字符），已超过AI分析限额，请人工查看",
                "⚠️ 邮件过长，未经 AI 分析", "too_long")

        estimated = self.classifier.estimate_tokens(
            CLASSIFY_SYSTEM_PROMPT + self.classifier.build_user_prompt(
                from_addr, subject, task["email_date"] or "", body[: self.cfg.max_body_chars]))

        # 3) 未配置 API Key：退化为“人工查看”模板，任务不丢失
        if not self.cfg.llm.api_key:
            return Decision("high", "未配置大模型 API Key，未做AI分类，请人工查看",
                            "⚠️ 未配置大模型，邮件未经分类", "no_llm")

        # 4) 配额检查（按当天累计“预留”值）
        quota_ok, quota_msg = self._quota_ok(account, account_id, estimated)
        if not quota_ok:
            logger.info("账号 %s 触发配额上限（%s），本邮件跳过模型调用", account.name, quota_msg)
            return Decision("high", f"API 每日配额已达上限（{quota_msg}），未做AI分类，请人工查看",
                            "⚠️ API 配额已满，邮件未经分类", "quota")

        # 5) 调用前写预估预留，调用后回填实际消耗
        usage_id = self.db.record_api_usage(account_id, task["id"], estimated, self.cfg.llm.model_name)
        try:
            result, actual = self.classifier.classify(
                from_addr, subject, task["email_date"] or "", body[: self.cfg.max_body_chars])
        except LLMError as e:
            logger.warning("大模型分类失败（任务保持 pending 待重试）：%s", e)
            return None
        self.db.update_api_usage_actual(usage_id, actual)

        reason = truncate(result.reason, reason_max) or "无"
        if result.priority == "high":
            return Decision("high", reason, "⚠️ 重要邮件（AI 判定）", "notified")
        return Decision("low", reason, "", "low")

    def _enqueue_notification(self, account: AccountConfig, account_id: int,
                              task: sqlite3.Row, decision: Decision,
                              message_id: str, body: str) -> None:
        """生成模板通知写入 outbox；dedup_key 幂等，重复处理不会重复发送。"""
        try:
            raw = json.loads(task["raw_json"] or "{}")
        except json.JSONDecodeError:
            raw = {}
        from_display = raw.get("from") or task["from_addr"] or "(未知发件人)"
        html_body = build_notification_html(
            banner=decision.banner,
            from_display=from_display,
            subject=task["subject"] or "(无主题)",
            date_str=task["email_date"] or "",
            link=extract_first_url(body),
            reason=decision.reason,
        )
        subject_out = f"【重要】{(task['subject'] or '(无主题)')[:200]}"
        self.db.enqueue_outbox(
            account_id=account_id,
            task_id=task["id"],
            to_addr=account.notification_to,
            subject=subject_out,
            body=html_body,
            scheduled_time=now_str(),
            dedup_key=f"notify:{account_id}:{message_id}",
        )
        logger.info("重要邮件已入通知队列：task=%s subject=%s", task["id"], task["subject"])

    def _mark_processed_on_server(self, imap: ImapClient, account: AccountConfig, uid: str) -> None:
        """本地决策落库后才调用：标记已读或移动。失败仅告警，不影响本地状态。"""
        try:
            if account.on_processed == "move":
                imap.move_to_folder(uid, account.processed_folder)
            else:
                imap.mark_seen(uid)
        except Exception as e:
            logger.warning("更新服务器邮件状态失败（本地已安全落库）：%s", e)
            try:
                imap.mark_seen(uid)  # move 失败时至少标已读，避免僵尸未读
            except Exception:
                pass

    # ------------------------------------------------------------------
    # agent 账号：命令处理
    # ------------------------------------------------------------------
    def _process_command(self, account: AccountConfig, account_id: int, task: sqlite3.Row,
                         imap: ImapClient, message_id: str) -> tuple[bool, str]:
        """鉴权 → 解析 → 执行 → 回复入队。回复的收件人永远是请求者本人。

        失败语义与 monitor 一致：拉取失败保持 pending；执行失败计 attempts，
        达到 max_command_attempts 后改回"处理失败"说明并终结，避免烧光配额。
        """
        try:
            raw = self._load_raw(account, task, imap)
        except Exception as e:
            logger.warning("拉取命令邮件失败（保持 pending 待重试）：uid=%s，%s", task["email_uid"], e)
            return False, ""
        msg = email.message_from_bytes(raw, policy=email.policy.default)

        reason = auto_mail_reason(msg, account)
        if reason is not None:
            self._finish_silently(account, account_id, task, imap, message_id, "ignored", f"忽略自动邮件：{reason}")
            return True, "ignored"

        from_addr = normalize_address(str(msg.get("From") or ""))
        if not is_authorized(from_addr, account):
            # 不回、不弹：避免 backscatter 与暴露命令面，只留日志
            logger.warning("账号 %s 收到非白名单来信，静默丢弃：from=%s subject=%s",
                           account.name, from_addr, (task["subject"] or "")[:80])
            self._finish_silently(account, account_id, task, imap, message_id, "unauthorized",
                                  f"非白名单发件人：{from_addr}")
            return True, "unauthorized"

        parsed = parse_command_mail(msg, extract_text_body(msg, self.cfg.max_extract_chars), account)
        label = f"#{parsed.name or parsed.raw_name or '?'}"
        try:
            reply = self._run_command(account, account_id, task, parsed, from_addr)
            self._enqueue_command_reply(account, account_id, task, msg, parsed, from_addr, reply, message_id)
        except Exception as e:
            attempts = self.db.increment_task_attempts(task["id"])
            if attempts < account.max_command_attempts:
                logger.warning("命令 %s 处理失败（第 %d 次，保持 pending）：%s", label, attempts, e)
                return False, ""
            logger.exception("命令 %s 连续失败 %d 次，回复错误说明并终结", label, attempts)
            html_body, text_body = render_notice("处理失败", [
                f"命令 {label} 连续 {attempts} 次处理失败，已停止重试。",
                f"最后一次错误：{str(e)[:300]}",
                "请稍后重新发送命令；若持续失败请查看 Leger 日志。",
            ])
            reply = CommandReply(reply_subject(parsed.clean_subject, parsed.raw_name), html_body, text_body)
            self._enqueue_command_reply(account, account_id, task, msg, parsed, from_addr, reply, message_id)

        self.db.mark_message_processed(account_id, message_id)
        self.db.finish_task(task["id"], "command", label)
        self._mark_processed_on_server(imap, account, task["email_uid"])
        self._drop_raw_cache(account, task)
        return True, "command"

    def _run_command(self, account: AccountConfig, account_id: int, task: sqlite3.Row,
                     parsed: ParsedCommand, from_addr: str) -> CommandReply:
        """执行命令。需要模型的命令抛出的 LLMError/ComposeError 由调用方按 attempts 处理。"""
        subject = reply_subject(parsed.clean_subject, parsed.raw_name)
        spec = parsed.spec
        if spec is None:
            html_body, text_body = render_help(
                account, command_catalog(), banner=f"未知命令 #{parsed.raw_name}，以下是可用命令。")
            return CommandReply(subject, html_body, text_body)
        if not spec.enabled:
            enabled = "、".join(f"#{c.name}" for c in command_catalog() if c.enabled)
            html_body, text_body = render_notice("命令尚未启用", [
                f"#{spec.name} 已识别，但当前版本还没有接入这项能力。",
                f"目前可用：{enabled}。",
            ])
            return CommandReply(subject, html_body, text_body)
        if spec.name == "help":
            banner = "主题里没有 #命令，已按默认命令处理。" if parsed.used_default else None
            html_body, text_body = render_help(account, command_catalog(), banner=banner)
            return CommandReply(subject, html_body, text_body)
        if spec.name == "status":
            html_body, text_body = render_status(account, self._collect_status(account, account_id))
            return CommandReply(subject, html_body, text_body)
        if spec.name == "compose":
            return self._cmd_compose(account, account_id, task, parsed, from_addr)
        if spec.name == "summarize":
            return self._cmd_summarize(account, account_id, task, parsed)
        if spec.name == "rewrite":
            return self._cmd_rewrite(account, account_id, task, parsed, from_addr)
        if spec.name == "confirm":
            return self._cmd_confirm(account, account_id, task, parsed, from_addr)
        raise RuntimeError(f"命令 #{spec.name} 已启用但没有处理器")

    # ---- 需要模型的命令 -------------------------------------------------
    def _cmd_compose(self, account: AccountConfig, account_id: int, task: sqlite3.Row,
                     parsed: ParsedCommand, from_addr: str) -> CommandReply:
        if not parsed.instruction.strip() and not parsed.args.strip():
            html_body, text_body = render_notice("#compose 需要指令", [
                "请在正文（分隔线之上）写明要写什么邮件，或至少在主题里给出 #compose 后的主题参数。",
                "分隔线下方或 .eml 附件可作为参考素材。",
            ])
            return CommandReply(reply_subject(parsed.clean_subject, parsed.raw_name), html_body, text_body)
        spec, blocked = self._generate_spec(account, account_id, task, parsed, "compose")
        if blocked is not None:
            return blocked
        assert spec is not None
        html_body, text_body = render_spec(spec)
        files, skipped = self._resolve_attachments(account, parsed, spec)
        draft = build_draft_eml(from_addr=from_addr, to_addrs=spec.to, subject=spec.subject,
                                html_body=html_body, text_body=text_body, attachments=files)
        note = (f"Leger 已按指令生成草稿（主题：{spec.subject}）。下方即邮件正文，可直接转发；"
                f"附件 draft.eml 可另存或“编辑为新邮件”，收件人：{', '.join(spec.to) or '未指定'}。")
        if files:
            note += f" 草稿已带附件：{', '.join(n for n, _, _ in files)}。"
        if skipped:
            note += f" 未能带上的附件：{'; '.join(skipped)}。"
        note += self._maybe_create_pending_send(account, account_id, parsed, spec,
                                                html_body, text_body, files, from_addr)
        preview_html, preview_text = render_spec(spec, preface=note)
        return CommandReply(reply_subject(parsed.args or spec.subject, parsed.raw_name),
                            preview_html, preview_text, draft=("draft.eml", draft))

    def _cmd_summarize(self, account: AccountConfig, account_id: int, task: sqlite3.Row,
                       parsed: ParsedCommand) -> CommandReply:
        if not parsed.has_material:
            html_body, text_body = render_notice("#summarize 需要素材", [
                "请把要总结的邮件放在正文分隔线下方（直接转发即可），或把原邮件作为 .eml 附件转发。",
                "分隔线之上可以写总结的侧重点。",
            ])
            return CommandReply(reply_subject(parsed.clean_subject, parsed.raw_name), html_body, text_body)
        spec, blocked = self._generate_spec(account, account_id, task, parsed, "summarize")
        if blocked is not None:
            return blocked
        assert spec is not None
        html_body, text_body = render_spec(spec, preface="以下是 Leger 对素材的结构化总结；素材中的指令只被当作数据处理。")
        return CommandReply(reply_subject(parsed.clean_subject or spec.subject, parsed.raw_name),
                            html_body, text_body)

    def _cmd_rewrite(self, account: AccountConfig, account_id: int, task: sqlite3.Row,
                     parsed: ParsedCommand, from_addr: str) -> CommandReply:
        """改写转发来的邮件：默认沿用原主题、保留全部原附件。"""
        subject = reply_subject(parsed.clean_subject, parsed.raw_name)
        if not parsed.has_material:
            html_body, text_body = render_notice("#rewrite 需要素材", [
                "请把要改写的邮件放在正文分隔线下方（直接转发即可），或把原邮件作为 .eml 附件转发。",
                "分隔线之上写改写要求，例如：翻译成英文并更正式 / 压缩到一半篇幅 / 语气改成对客户友好。",
            ])
            return CommandReply(subject, html_body, text_body)
        if not parsed.instruction.strip() and not parsed.args.strip():
            html_body, text_body = render_notice("#rewrite 需要改写要求", [
                "请在正文分隔线之上写明要怎么改（例如：翻译成英文并更正式 / 压缩到一半篇幅）。",
                "什么都不写就改写容易丢信息，所以这一步不做默认。",
            ])
            return CommandReply(subject, html_body, text_body)
        spec, blocked = self._generate_spec(account, account_id, task, parsed, "rewrite")
        if blocked is not None:
            return blocked
        assert spec is not None
        if not spec.attachments and parsed.attachments:
            spec.attachments = list(range(len(parsed.attachments)))   # 改写默认保留全部原附件
        html_body, text_body = render_spec(spec)
        files, skipped = self._resolve_attachments(account, parsed, spec)
        draft = build_draft_eml(from_addr=from_addr, to_addrs=spec.to, subject=spec.subject,
                                html_body=html_body, text_body=text_body, attachments=files)
        note = (f"Leger 已按要求改写（主题：{spec.subject}）。下方即改写后的邮件正文，可直接转发；"
                f"附件 draft.eml 可另存，收件人：{', '.join(spec.to) or '未指定'}。")
        if files:
            note += f" 已保留附件：{', '.join(n for n, _, _ in files)}。"
        if skipped:
            note += f" 未能带上的附件：{'; '.join(skipped)}。"
        note += self._maybe_create_pending_send(account, account_id, parsed, spec,
                                                html_body, text_body, files, from_addr)
        preview_html, preview_text = render_spec(spec, preface=note)
        return CommandReply(reply_subject(parsed.args or spec.subject, parsed.raw_name),
                            preview_html, preview_text, draft=("draft.eml", draft))

    # ---- 两步确认发送（#send-as / #confirm）---------------------------
    def _resolve_send_account(self, account: AccountConfig) -> Optional[AccountConfig]:
        """send_as 指向的账号配置——Leger 必须持有其 SMTP 凭据才发得出去。"""
        if not account.send_as:
            return None
        for a in self.cfg.accounts:
            if normalize_address(a.imap_user) == account.send_as or (
                    a.smtp_user and normalize_address(a.smtp_user) == account.send_as):
                return a
        return None

    def _maybe_create_pending_send(self, account: AccountConfig, account_id: int,
                                   parsed: ParsedCommand, spec, html_body: str, text_body: str,
                                   files: list, from_addr: str) -> str:
        """compose/rewrite 成功且具备发送条件时，生成两步确认任务，返回追加到预览里的提示。

        不适用（未配置 send_as / 规格无收件人）返回空串——那类结果仍只是草稿。
        """
        send_acc = self._resolve_send_account(account)
        if send_acc is None or not send_acc.smtp_host or not spec.to:
            return ""
        code = _confirm_code()
        now = datetime.now()
        final_raw = build_draft_eml(
            from_addr=send_acc.reply_from, to_addrs=spec.to, subject=spec.subject,
            html_body=html_body, text_body=text_body, attachments=files,
            draft_header=False)   # 对外成品：干净 MIME，无草稿标记
        message_id = str(email.message_from_bytes(final_raw, policy=email.policy.default)
                         .get("Message-ID") or "")
        pending_dir = self.cfg.outbox_dir / account.name / "pending"
        pending_dir.mkdir(parents=True, exist_ok=True)
        raw_path = pending_dir / f"pending-{secrets.token_hex(6)}.eml"
        raw_path.write_bytes(final_raw)
        self.db.create_pending_send(
            account_id=account_id, requester=from_addr, send_as=send_acc.reply_from,
            to_addrs=", ".join(spec.to), subject=spec.subject,
            code_hash=_code_hash(from_addr, code), raw_path=str(raw_path),
            message_id=message_id, created_at=now.strftime("%Y-%m-%d %H:%M:%S"),
            expires_at=(now + timedelta(minutes=CONFIRM_TTL_MIN)).strftime("%Y-%m-%d %H:%M:%S"))
        return (f"确认发送：回复 #confirm {code}，将以 {send_acc.reply_from} 名义真实发送给"
                f" {', '.join(spec.to)}（{CONFIRM_TTL_MIN} 分钟内有效）。")

    def _cmd_confirm(self, account: AccountConfig, account_id: int, task: sqlite3.Row,
                     parsed: ParsedCommand, from_addr: str) -> CommandReply:
        subject = "Re: 发送确认"
        code = parsed.args.strip().upper()
        if not re.fullmatch(r"[2-9A-Z]{4,10}", code or ""):
            html_body, text_body = render_notice("用法：#confirm 确认码", [
                "确认码在生成预览的回信里，例如：#confirm 7K2M4X。"])
            return CommandReply(subject, html_body, text_body)

        row = self.db.find_pending_send(account_id, from_addr, _code_hash(from_addr, code))
        if row is None:
            # 错误码/过期：对最近一个待发任务计错误次数，超限作废（防爆破）
            latest = self.db.latest_pending_send(account_id, from_addr)
            if latest is not None:
                attempts = self.db.increment_pending_attempts(latest["id"])
                if attempts >= MAX_CONFIRM_ATTEMPTS:
                    self.db.dead_pending_send(latest["id"])
                    Path(latest["raw_path"]).unlink(missing_ok=True)
                    html_body, text_body = render_notice("发送任务已作废", [
                        f"确认码连续错误 {attempts} 次，该任务已作废。",
                        "请重新用 #compose / #rewrite 生成并确认。"])
                    return CommandReply(subject, html_body, text_body)
            html_body, text_body = render_notice("没有可确认的发送任务", [
                "确认码不正确、已超过 60 分钟、已发送或已作废。",
                "请回到原预览回信核对确认码。"])
            return CommandReply(subject, html_body, text_body)

        send_acc = self._resolve_send_account(account)
        raw_path = Path(row["raw_path"])
        if send_acc is None or not raw_path.is_file():
            self.db.dead_pending_send(row["id"])
            raw_path.unlink(missing_ok=True)
            html_body, text_body = render_notice("发送任务已失效", [
                "发送账号配置或待发文件不存在，请重新生成。"])
            return CommandReply(subject, html_body, text_body)

        raw = raw_path.read_bytes()
        try:
            with SmtpSession(send_acc) as smtp:
                smtp.send_raw(send_acc.reply_from, row["to_addrs"], raw)
        except smtplib.SMTPRecipientsRefused as e:
            # 收件人被服务器拒收（如外部投递受限）：确定性失败，作废并说明
            self.db.dead_pending_send(row["id"])
            raw_path.unlink(missing_ok=True)
            html_body, text_body = render_notice("发送被服务器拒绝", [
                f"收件人 {row['to_addrs']} 被服务器拒收（外部投递受限时只能发本域）。",
                str(e)[:300],
            ])
            return CommandReply(subject, html_body, text_body)
        except (smtplib.SMTPException, OSError) as e:
            logger.warning("确认发送临时失败（保留待发任务，可再次确认）：%s", e)
            html_body, text_body = render_notice("发送暂时失败", [
                "服务器暂时不可用，待发任务仍保留，请稍后重新回复确认码。", str(e)[:300]])
            return CommandReply(subject, html_body, text_body)

        self.db.mark_pending_sent(row["id"])
        raw_path.unlink(missing_ok=True)
        self._append_sent_copy(send_acc, raw)
        html_body, text_body = render_notice("已发送", [
            f"已以 {send_acc.reply_from} 名义发送给 {row['to_addrs']}。",
            f"主题：{row['subject']}",
        ])
        return CommandReply(subject, html_body, text_body)

    def _append_sent_copy(self, send_acc: AccountConfig, raw: bytes) -> None:
        """已发送副本 APPEND 到发送账号的 Sent 文件夹；尽力而为，失败仅告警。

        在后台线程执行：线上服务端存在"APPEND 成功后同会话命令被吞"的已知问题
        （登录后只 APPEND 时，QUIT 响应也会被吞），同步等待会平白拖住确认回执。
        """
        if not send_acc.sent_folder:
            return

        def _run() -> None:
            try:
                if send_acc.imap_use_ssl:
                    conn = imaplib.IMAP4_SSL(send_acc.imap_host, send_acc.imap_port, timeout=15)
                else:
                    conn = imaplib.IMAP4(send_acc.imap_host, send_acc.imap_port, timeout=15)
                try:
                    conn.login(send_acc.imap_user, send_acc.imap_password)
                    quoted = '"' + imap_utf7_encode(send_acc.sent_folder).replace('"', '\\"') + '"'
                    conn.create(quoted)      # 已存在时服务器返回 NO，忽略
                    conn.append(quoted, "(\\Seen)", None, raw)
                finally:
                    try:
                        conn.logout()
                    except Exception:
                        pass
            except Exception as e:
                logger.warning("已发送副本 APPEND 失败（不影响发送结果）：%s", e)

        threading.Thread(target=_run, name="leger-sent-copy", daemon=True).start()

    def _generate_spec(self, account: AccountConfig, account_id: int, task: sqlite3.Row,
                       parsed: ParsedCommand, kind: str) -> tuple[Optional[MailSpec], Optional[CommandReply]]:
        """配额/密钥门禁 → 预留记账 → 生成 → 回填。门禁未过返回说明回复（确定性结果，不计 attempts）。"""
        subject = reply_subject(parsed.clean_subject, parsed.raw_name)
        if not self.cfg.llm.api_key:
            html_body, text_body = render_notice("未配置大模型", ["llm.api_key 为空，无法执行需要生成的命令。"])
            return None, CommandReply(subject, html_body, text_body)
        material = material_text_for_llm(parsed, account.max_material_chars)
        infos = [AttachmentInfo(a.index, a.filename, a.content_type, a.size) for a in parsed.attachments]
        req = self.composer.prepare(kind, parsed.instruction, parsed.args, material, infos,
                                    language_hint=account.reply_language)
        estimated = self.composer.estimate_tokens(req)
        ok, quota_msg = self._quota_ok(account, account_id, estimated)
        if not ok:
            html_body, text_body = render_notice("今日模型配额已满", [quota_msg, "请明天再试，或调高该账号的 daily_max_calls / daily_max_tokens。"])
            return None, CommandReply(subject, html_body, text_body)
        usage_id = self.db.record_api_usage(account_id, task["id"], estimated, self.cfg.llm.generate_model)
        spec, actual = self.composer.run(req)      # LLMError / ComposeError 向上抛 → attempts
        self.db.update_api_usage_actual(usage_id, actual)
        return spec, None

    def _resolve_attachments(self, account: AccountConfig, parsed: ParsedCommand,
                             spec: MailSpec) -> tuple[list[tuple[str, str, bytes]], list[str]]:
        """按规格索引取附件内容；超过单附件上限的跳过并说明。"""
        limit = account.max_attachment_mb * 1024 * 1024
        files: list[tuple[str, str, bytes]] = []
        skipped: list[str] = []
        for idx in spec.attachments:
            ref = parsed.attachments[idx]
            data = ref.read()
            if len(data) > limit:
                skipped.append(f"{ref.filename}（{len(data) // 1024} KB，超过 {account.max_attachment_mb} MB 上限）")
                continue
            files.append((ref.filename, ref.content_type, data))
        return files, skipped

    def _collect_status(self, account: AccountConfig, account_id: int) -> dict:
        calls, tokens = self.db.today_usage(account_id)
        status = {
            "calls_today": calls,
            "tokens_today": tokens,
            # 当前这条命令任务此刻仍是 pending，读到回复时它已完成，故不计入
            "pending_tasks": max(0, self.db.count_pending_tasks(account_id) - 1),
            "enabled_commands": [f"#{c.name}" for c in command_catalog() if c.enabled],
            "version": __version__,
        }
        status.update(self.db.outbox_counts(account_id, self.cfg.max_send_retries))
        return status

    def _enqueue_command_reply(self, account: AccountConfig, account_id: int, task: sqlite3.Row,
                               msg: Message, parsed: ParsedCommand, to_addr: str,
                               reply: CommandReply, message_id: str) -> None:
        """生成完整 .eml → 校验不变量 → 落盘 → 入 outbox（dedup_key 幂等）。"""
        raw_reply = build_reply(
            account=account, to_addr=to_addr, subject=reply.subject,
            html_body=reply.html, text_body=reply.text,
            in_reply_to=str(msg.get("Message-ID") or "").strip(),
            references=str(msg.get("References") or "").strip(),
            command=parsed.name or parsed.raw_name or "",
            rfc822_attachments=[reply.draft] if reply.draft else None,
        )
        validate_reply(raw_reply, to_addr, account.max_mime_mb * 1024 * 1024)
        out_dir = self.cfg.outbox_dir / account.name
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"task-{task['id']}.eml"
        path.write_bytes(raw_reply)
        self.db.enqueue_outbox(
            account_id=account_id, task_id=task["id"], to_addr=to_addr,
            subject=reply.subject, body="", scheduled_time=now_str(),
            dedup_key=f"cmd:{account_id}:{message_id}", raw_path=str(path),
        )
        logger.info("命令回复已入队：task=%s to=%s subject=%s", task["id"], to_addr, reply.subject[:80])

    def _finish_silently(self, account: AccountConfig, account_id: int, task: sqlite3.Row,
                         imap: ImapClient, message_id: str, outcome: str, reason: str) -> None:
        """不回复地终结任务（非白名单 / 自动邮件）。"""
        self.db.mark_message_processed(account_id, message_id)
        self.db.finish_task(task["id"], outcome, reason)
        self._mark_processed_on_server(imap, account, task["email_uid"])
        self._drop_raw_cache(account, task)

    def _raw_cache_path(self, account: AccountConfig, task: sqlite3.Row) -> Path:
        return self.cfg.raw_mail_dir / account.name / f"{task['email_uid']}.eml"

    def _load_raw(self, account: AccountConfig, task: sqlite3.Row, imap: ImapClient) -> bytes:
        """原始 MIME 落盘缓存：崩溃恢复时不重拉；写入用临时文件 + 原子替换。"""
        path = self._raw_cache_path(account, task)
        if path.is_file():
            return path.read_bytes()
        raw = imap.fetch_raw(task["email_uid"])
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".part")
        tmp.write_bytes(raw)
        tmp.replace(path)
        return raw

    def _drop_raw_cache(self, account: AccountConfig, task: sqlite3.Row) -> None:
        try:
            self._raw_cache_path(account, task).unlink()
        except OSError:
            pass

    # ------------------------------------------------------------------
    # 发送
    # ------------------------------------------------------------------
    def send_outbox(self, account: AccountConfig) -> int:
        """发送该账号所有到期（且未超重试上限）的 outbox 邮件，返回成功条数。"""
        account_id = self.db.upsert_account(account)
        rows = self.db.due_outbox(account_id, self.cfg.max_send_retries)
        if not rows:
            return 0
        sent = 0
        try:
            with SmtpSession(account) as smtp:
                for row in rows:
                    raw_path = row["raw_path"] if "raw_path" in row.keys() else None
                    try:
                        if raw_path:
                            self._send_raw_row(smtp, account, row, raw_path)
                        else:
                            smtp.send_html(row["to_addr"], row["subject"], row["body"])
                        self.db.mark_outbox_sent(row["id"])
                        sent += 1
                        logger.info("邮件已发送：to=%s subject=%s", row["to_addr"], row["subject"][:80])
                        if raw_path:
                            Path(raw_path).unlink(missing_ok=True)   # 已落库 sent 之后才删文件
                    except DeadLetter as e:
                        self.db.mark_outbox_dead(row["id"], self.cfg.max_send_retries)
                        logger.error("outbox #%s 永久失败（不再重试）：%s", row["id"], e)
                    except Exception as e:
                        self.db.mark_outbox_failed(row["id"])
                        logger.error("发送失败（将下次重试）：id=%s，%s", row["id"], e)
        except Exception as e:
            for row in rows:
                self.db.mark_outbox_failed(row["id"])
            logger.error("SMTP 连接失败，%d 封待发邮件延后重试：%s", len(rows), e)
        return sent

    def _send_raw_row(self, smtp: SmtpSession, account: AccountConfig,
                      row: sqlite3.Row, raw_path: str) -> None:
        """发送预生成 .eml。agent 账号在这里再校验一次"只发给白名单"的不变量。"""
        to_addr = normalize_address(row["to_addr"])
        if account.role == "agent" and to_addr not in account.allowed_senders:
            raise DeadLetter(f"收件人 {to_addr} 不在白名单内，拒绝发送")
        path = Path(raw_path)
        if not path.is_file():
            raise DeadLetter(f"预生成邮件文件不存在：{raw_path}")
        raw = path.read_bytes()
        if account.role == "agent":
            try:
                validate_reply(raw, to_addr, account.max_mime_mb * 1024 * 1024)
            except ValueError as e:
                raise DeadLetter(str(e)) from e
        smtp.send_raw(account.reply_from, to_addr, raw)

    # ------------------------------------------------------------------
    # 每日汇总
    # ------------------------------------------------------------------
    def generate_summary(self, account: AccountConfig, force: bool = False) -> bool:
        """生成“每日低优先级汇总”并入队。force=True 忽略时间立即生成。

        返回是否新生成（dedup_key = summary:{account_id}:{日期}，天然防重复）。
        """
        if account.role == "agent":
            return False   # 命令邮箱没有"低优先级邮件"概念，也没有通知收件箱
        account_id = self.db.upsert_account(account)
        today = today_str()
        dedup_key = f"summary:{account_id}:{today}"
        if self.db.summary_enqueued(dedup_key):
            return False
        if not force:
            target = datetime.strptime(account.daily_summary_time, "%H:%M").time()
            if datetime.now().time() < target:
                return False
        rows = self.db.today_low_priority(account_id)
        if not rows and not force:
            return False

        groups = self._group_by_sender(rows)
        digest = self._llm_digest(account, account_id, groups)
        html_body = build_summary_html(account.name, today, groups, digest)
        subject = f"【MailAgent】每日邮件汇总 {today}（低优先级 {len(rows)} 封）"
        self.db.enqueue_outbox(
            account_id=account_id, task_id=None, to_addr=account.notification_to,
            subject=subject[:200], body=html_body,
            scheduled_time=now_str(), dedup_key=dedup_key,
        )
        logger.info("每日汇总已入队：%s（%d 封低优先级）", dedup_key, len(rows))
        return True

    @staticmethod
    def _group_by_sender(rows: list[sqlite3.Row]) -> list[tuple[str, list[tuple[str, str, str]]]]:
        grouped: dict[str, list[tuple[str, str, str]]] = {}
        for r in rows:
            sender = r["from_addr"] or "(未知发件人)"
            t = (r["updated_at"] or "")[11:16]
            grouped.setdefault(sender, []).append((t, r["subject"] or "(无主题)", r["reason"] or ""))
        return sorted(grouped.items(), key=lambda kv: kv[0].lower())

    def _llm_digest(self, account: AccountConfig, account_id: int,
                    groups: list[tuple[str, list[tuple[str, str, str]]]]) -> str | None:
        """可选：用大模型为汇总生成一段摘要；输入过长或配额不足则退回分组列表。"""
        if not self.cfg.llm.api_key or not groups:
            return None
        lines: list[str] = []
        for sender, items in groups:
            lines.append(f"发件人 {sender}:")
            lines.extend(f"- [{t}] {s}" + (f"（{r}）" if r else "") for t, s, r in items)
        text = "\n".join(lines)
        if len(text) > self.cfg.summary_llm_max_chars:
            logger.info("汇总内容过长（%d 字符），跳过大模型摘要，直接分组展示", len(text))
            return None
        estimated = self.classifier.estimate_tokens(text, max_tokens=300)
        quota_ok, _ = self._quota_ok(account, account_id, estimated)
        if not quota_ok:
            return None
        usage_id = self.db.record_api_usage(account_id, None, estimated, self.cfg.llm.model_name)
        try:
            digest, actual = self.classifier.summarize(text)
        except LLMError as e:
            logger.warning("汇总摘要生成失败，退回分组列表展示：%s", e)
            return None
        self.db.update_api_usage_actual(usage_id, actual)
        return truncate(digest, 500)

    def _quota_ok(self, account: AccountConfig, account_id: int, estimated_tokens: int) -> tuple[bool, str]:
        """检查“当天累计预留配额 + 本次预估”是否超限。"""
        calls, tokens = self.db.today_usage(account_id)
        if calls + 1 > account.daily_max_calls:
            return False, f"今日调用次数已达上限 {account.daily_max_calls}"
        if tokens + estimated_tokens > account.daily_max_tokens:
            return False, (f"今日预留token {tokens} + 本次预估 {estimated_tokens} "
                           f"将超过上限 {account.daily_max_tokens}")
        return True, ""
