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
"""
from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime

from .classifier import CLASSIFY_SYSTEM_PROMPT, LLMClassifier, LLMError, rule_classify
from .config import AccountConfig, AppConfig
from .db import Database, now_str, today_str
from .imap_worker import ImapClient, extract_text_body
from .mailer import (SmtpSession, build_notification_html, build_summary_html,
                     extract_first_url, truncate)

logger = logging.getLogger(__name__)


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

    # ------------------------------------------------------------------
    # 扫描入口
    # ------------------------------------------------------------------
    def run_account_scan(self, account: AccountConfig) -> dict[str, int]:
        """对单个账号执行一次“扫描 + 处理 pending + 发送到期通知”。"""
        account_id = self.db.upsert_account(account)
        stats: dict[str, int] = {"new_tasks": 0, "notified": 0, "low": 0, "dedup": 0,
                                 "too_long": 0, "quota": 0, "no_llm": 0, "retry_later": 0}
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
                    try:
                        smtp.send_html(row["to_addr"], row["subject"], row["body"])
                        self.db.mark_outbox_sent(row["id"])
                        sent += 1
                        logger.info("邮件已发送：to=%s subject=%s", row["to_addr"], row["subject"][:80])
                    except Exception as e:
                        self.db.mark_outbox_failed(row["id"])
                        logger.error("发送失败（将下次重试）：id=%s，%s", row["id"], e)
        except Exception as e:
            for row in rows:
                self.db.mark_outbox_failed(row["id"])
            logger.error("SMTP 连接失败，%d 封待发邮件延后重试：%s", len(rows), e)
        return sent

    # ------------------------------------------------------------------
    # 每日汇总
    # ------------------------------------------------------------------
    def generate_summary(self, account: AccountConfig, force: bool = False) -> bool:
        """生成“每日低优先级汇总”并入队。force=True 忽略时间立即生成。

        返回是否新生成（dedup_key = summary:{account_id}:{日期}，天然防重复）。
        """
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
