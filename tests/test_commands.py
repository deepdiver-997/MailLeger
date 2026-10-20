"""commands 模块：主题命令解析、正文切分、rfc822 素材、鉴权与循环守卫（纯函数）。"""
import unittest
from email.message import EmailMessage

from src import commands as cmd
from src.config import AccountConfig


def _agent() -> AccountConfig:
    return AccountConfig(
        name="leger", imap_host="imap.example.com", imap_user="leger@example.com",
        smtp_host="smtp.example.com", smtp_user="leger@example.com",
        role="agent", allowed_senders=["sakura@example.com"],
    )


class TestParseSubject(unittest.TestCase):
    def test_plain_command(self) -> None:
        sp = cmd.parse_subject("#help")
        self.assertEqual((sp.raw_name, sp.name, sp.args), ("help", "help", ""))

    def test_reply_forward_prefixes_and_args(self) -> None:
        sp = cmd.parse_subject("Re: Fwd: #compose 下周例会通知")
        self.assertEqual(sp.name, "compose")
        self.assertEqual(sp.args, "下周例会通知")
        self.assertEqual(sp.clean_subject, "下周例会通知")

    def test_chinese_prefix_bracket_tag_and_colon(self) -> None:
        sp = cmd.parse_subject("转发: [外部] #Rewrite: 改成正式语气")
        self.assertEqual(sp.name, "rewrite")
        self.assertEqual(sp.args, "改成正式语气")

    def test_alias_resolves_to_canonical(self) -> None:
        sp = cmd.parse_subject("#forward 请精简")
        self.assertEqual((sp.raw_name, sp.name), ("forward", "rewrite"))

    def test_unknown_command_keeps_raw_name(self) -> None:
        sp = cmd.parse_subject("#foobar")
        self.assertEqual((sp.raw_name, sp.name), ("foobar", None))

    def test_no_command(self) -> None:
        sp = cmd.parse_subject("Re: 普通主题")
        self.assertIsNone(sp.raw_name)
        self.assertIsNone(sp.name)
        self.assertEqual(sp.clean_subject, "普通主题")

    def test_space_after_hash(self) -> None:
        self.assertEqual(cmd.parse_subject("# status").name, "status")

    def test_sticky_command_glued_to_forward_prefix(self) -> None:
        # 手机上把 #summarize 直接贴在预填的 "Fw: …" 前面（线上实测踩到的写法）
        sp = cmd.parse_subject("#summarizeFw: 你最近使用 Apple\xa0帳户下載的項目")
        self.assertEqual((sp.raw_name, sp.name), ("summarize", "summarize"))
        self.assertEqual(sp.clean_subject, "你最近使用 Apple\xa0帳户下載的項目")
        sp = cmd.parse_subject("#composeRe: 关于预算")
        self.assertEqual((sp.name, sp.args), ("compose", "关于预算"))

    def test_fullwidth_hash(self) -> None:
        self.assertEqual(cmd.parse_subject("＃help").name, "help")
        self.assertEqual(cmd.parse_subject("转发：＃summarize 原主题").clean_subject, "原主题")

    def test_known_command_anywhere_in_subject(self) -> None:
        sp = cmd.parse_subject("Fw: 季度报告 #summarize")
        self.assertEqual(sp.name, "summarize")
        self.assertEqual(sp.clean_subject, "季度报告")
        sp = cmd.parse_subject("Fw: 季度报告 #forward 精简一下")
        self.assertEqual((sp.name, sp.clean_subject), ("rewrite", "季度报告 精简一下"))

    def test_unknown_hash_word_inside_subject_is_not_a_command(self) -> None:
        # 转发主题里本来就有的 #foo 不能被当成命令
        sp = cmd.parse_subject("Fw: Issue #foo needs review")
        self.assertIsNone(sp.raw_name)
        self.assertEqual(sp.clean_subject, "Issue #foo needs review")
        # 但起始处的未知命令仍如实报告
        self.assertEqual(cmd.parse_subject("#foobar").raw_name, "foobar")


class TestSplitBody(unittest.TestCase):
    def _check(self, text: str, instruction: str, material_prefix: str) -> None:
        ins, mat = cmd.split_body(text)
        self.assertEqual(ins, instruction)
        self.assertTrue(mat.startswith(material_prefix), mat)

    def test_gmail_forward(self) -> None:
        self._check("请帮我总结\n\n---------- Forwarded message ---------\nFrom: a@b.c\nSubject: x\n\nbody",
                    "请帮我总结", "---------- Forwarded message")

    def test_apple_mail_forward(self) -> None:
        self._check("改写一下\n\nBegin forwarded message:\n\nFrom: a@b.c", "改写一下", "Begin forwarded")

    def test_outlook_original_message(self) -> None:
        self._check("精简\n-----Original Message-----\nFrom: a", "精简", "-----Original")

    def test_qq_chinese_delimiter(self) -> None:
        self._check("总结\n------------------ 原始邮件 ------------------\n发件人: a", "总结", "------------------ 原始邮件")

    def test_header_block_without_dashes(self) -> None:
        self._check("改成正式语气\n\nFrom: Alice <a@b.c>\nSent: Monday\nTo: me\nSubject: hi\n\nbody",
                    "改成正式语气", "From: Alice")

    def test_reply_quote_english_and_chinese(self) -> None:
        self._check("回复要点\nOn Mon, Oct 20, 2026 at 9:00 AM Bob <b@c.d> wrote:\n> hi", "回复要点", "On Mon")
        self._check("回复要点\n在 2026年10月20日，Bob 写道：\n> hi", "回复要点", "在 2026")

    def test_quote_block_start(self) -> None:
        self._check("要点\n> 引用的内容", "要点", "> 引用")

    def test_signature_stripped_from_instruction(self) -> None:
        ins, mat = cmd.split_body("指令\n-- \n我的签名")
        self.assertEqual((ins, mat), ("指令", ""))

    def test_no_delimiter_all_instruction(self) -> None:
        self.assertEqual(cmd.split_body("只有指令\n第二行"), ("只有指令\n第二行", ""))

    def test_manual_dashes(self) -> None:
        self._check("指令\n---\n素材", "指令", "---")


class TestRfc822AndParseMail(unittest.TestCase):
    def test_rfc822_attachment_preferred_as_material(self) -> None:
        inner = EmailMessage()
        inner["Subject"] = "原邮件"
        inner["From"] = "x@y.z"
        inner.set_content("原正文")
        outer = EmailMessage()
        outer["Subject"] = "#rewrite 更正式"
        outer["From"] = "Sakura <sakura@example.com>"
        outer.set_content("请改写附件里的邮件\n---\n这行不该被当成素材分隔")
        outer.add_attachment(inner)

        parsed = cmd.parse_command_mail(outer, outer.get_body(("plain",)).get_content(), _agent())
        self.assertEqual(parsed.name, "rewrite")
        self.assertIsNotNone(parsed.material_message)
        self.assertEqual(str(parsed.material_message["Subject"]), "原邮件")
        self.assertEqual(parsed.material_text, "")
        self.assertIn("请改写附件里的邮件", parsed.instruction)

    def test_default_command_when_subject_has_no_hash(self) -> None:
        msg = EmailMessage()
        msg["Subject"] = "随便聊聊"
        msg["From"] = "sakura@example.com"
        msg.set_content("hi")
        parsed = cmd.parse_command_mail(msg, "hi", _agent())
        self.assertTrue(parsed.used_default)
        self.assertEqual(parsed.name, "help")
        self.assertIsNone(parsed.raw_name)


class TestAuthAndLoopGuard(unittest.TestCase):
    def _msg(self, **headers: str) -> EmailMessage:
        m = EmailMessage()
        m["From"] = headers.pop("From", "Sakura <SAKURA@Example.com>")
        for k, v in headers.items():
            m[k] = v
        m.set_content("x")
        return m

    def test_authorized_is_case_and_display_name_insensitive(self) -> None:
        acc = _agent()
        self.assertTrue(cmd.is_authorized("Sakura <SAKURA@Example.com>", acc))
        self.assertFalse(cmd.is_authorized("mallory@example.com", acc))
        self.assertFalse(cmd.is_authorized("", acc))

    def test_normal_mail_not_flagged(self) -> None:
        self.assertIsNone(cmd.auto_mail_reason(self._msg(), _agent()))
        self.assertIsNone(cmd.auto_mail_reason(self._msg(**{"Auto-Submitted": "no"}), _agent()))

    def test_auto_submitted_flagged(self) -> None:
        self.assertIn("Auto-Submitted", cmd.auto_mail_reason(self._msg(**{"Auto-Submitted": "auto-replied"}), _agent()))

    def test_leger_generated_flagged(self) -> None:
        self.assertIn("X-Leger-Generated", cmd.auto_mail_reason(self._msg(**{"X-Leger-Generated": "1"}), _agent()))

    def test_bulk_precedence_and_list_flagged(self) -> None:
        self.assertIn("Precedence", cmd.auto_mail_reason(self._msg(Precedence="bulk"), _agent()))
        self.assertIn("邮件列表", cmd.auto_mail_reason(self._msg(**{"List-Id": "<dev.lists.example.com>"}), _agent()))

    def test_self_sender_flagged(self) -> None:
        self.assertIn("自身", cmd.auto_mail_reason(self._msg(From="Leger <leger@example.com>"), _agent()))

    def test_empty_from_flagged(self) -> None:
        m = EmailMessage()
        m.set_content("x")
        self.assertIn("From 为空", cmd.auto_mail_reason(m, _agent()))

    def test_mask_address(self) -> None:
        self.assertEqual(cmd.mask_address("sakura@example.com"), "s***a@example.com")
        self.assertEqual(cmd.mask_address("ab@x.y"), "***@x.y")


if __name__ == "__main__":
    unittest.main()
