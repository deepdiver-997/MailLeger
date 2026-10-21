"""composer：MailSpec 严格校验、模型 JSON 解析、反馈重试；mime_builder：渲染转义、草稿 .eml。

全程不触网：Composer 用假客户端按序返回预置输出。
"""
import copy
import email
import email.policy
import unittest

from src.composer import (REWRITE_SYSTEM_PROMPT, AttachmentInfo, ComposeError, Composer,
                          SpecError, SpecLimits, loads_model_json, parse_mail_spec)
from src.config import LLMConfig
from src.mime_builder import build_draft_eml, inline_html, inline_text, render_spec

VALID = {
    "template": "card",
    "subject": "下周例会通知",
    "to": ["Team <TEAM@Example.com>", "team@example.com"],
    "blocks": [
        {"type": "heading", "text": "时间"},
        {"type": "paragraph", "text": "周一 **10:00**，详见 [日程](https://cal.example.com/x)。"},
        {"type": "bullets", "items": ["带电脑", "准时"]},
        {"type": "numbered", "items": ["签到", "讨论"]},
        {"type": "table", "header": ["议题", "负责人"], "rows": [["进度", "A"], ["风险", "B"]]},
        {"type": "quote", "text": "引用一句"},
        {"type": "callout", "style": "warn", "text": "注意事项"},
        {"type": "button", "text": "打开日程", "url": "https://cal.example.com/x"},
        {"type": "divider"},
    ],
    "attachments": [{"index": 0}, 0],
}


def _spec(**overrides):
    obj = copy.deepcopy(VALID)
    obj.update(overrides)
    return obj


class TestParseMailSpec(unittest.TestCase):
    def test_valid_spec_normalizes(self) -> None:
        spec = parse_mail_spec(VALID, SpecLimits(), available_attachments=1)
        self.assertEqual(spec.template, "card")
        self.assertEqual(spec.to, ["team@example.com"])            # 规范化 + 去重
        self.assertEqual(len(spec.blocks), 9)
        self.assertEqual(spec.attachments, [0])                   # dict/int 两种写法，去重
        self.assertEqual(spec.blocks[6].style, "warn")

    def test_rejections(self) -> None:
        cases = {
            "template": _spec(template="fancy"),
            "subject": _spec(subject=""),
            "block type": _spec(blocks=[{"type": "script", "text": "x"}]),
            "button url": _spec(blocks=[{"type": "button", "text": "x", "url": "javascript:alert(1)"}]),
            "to": _spec(to=["not-an-email"]),
            "attachment index": _spec(attachments=[{"index": 3}]),
            "attachment bool": _spec(attachments=[True]),
            "blocks not list": _spec(blocks="x"),
            "callout style": _spec(blocks=[{"type": "callout", "style": "danger", "text": "x"}]),
            "table rows": _spec(blocks=[{"type": "table", "header": ["a"], "rows": []}]),
            "empty items": _spec(blocks=[{"type": "bullets", "items": []}]),
        }
        for name, obj in cases.items():
            with self.subTest(name), self.assertRaises(SpecError):
                parse_mail_spec(obj, SpecLimits(), available_attachments=1)

    def test_limits_enforced(self) -> None:
        with self.assertRaises(SpecError):
            parse_mail_spec(VALID, SpecLimits(max_blocks=2), available_attachments=1)
        with self.assertRaises(SpecError):
            parse_mail_spec(_spec(blocks=[{"type": "paragraph", "text": "x" * 50}]),
                            SpecLimits(max_text=10), available_attachments=1)

    def test_loads_model_json_tolerates_fences_and_prose(self) -> None:
        self.assertEqual(loads_model_json('```json\n{"a": 1}\n```'), {"a": 1})
        self.assertEqual(loads_model_json('好的，这是结果：{"a": 1} 请查收'), {"a": 1})
        with self.assertRaises(SpecError):
            loads_model_json("完全不是 JSON")


class _FakeClient:
    """按序返回预置输出；记录收到的 payload。"""

    def __init__(self, outputs: list[str]) -> None:
        self.outputs = list(outputs)
        self.payloads: list[dict] = []

    def chat_text(self, payload: dict, timeout=None):
        self.payloads.append(payload)
        return self.outputs.pop(0), 100

    def estimate_tokens(self, text: str, max_tokens=None) -> int:
        return len(text) // 4 + (max_tokens or 0)


class TestComposer(unittest.TestCase):
    def _composer(self, outputs: list[str]):
        llm = LLMConfig(api_key="k", model_name="small", generate_model_name="big",
                        generate_max_tokens=1234, generate_timeout_seconds=77)
        client = _FakeClient(outputs)
        return Composer(llm, client), client

    def test_prepare_rewrite_uses_rewrite_prompt_and_language_hint(self) -> None:
        composer, _ = self._composer([])
        req = composer.prepare("rewrite", "翻译成英文并更正式", "", "原文素材",
                               [AttachmentInfo(0, "a.txt", "text/plain", 5)], language_hint="简体中文")
        self.assertEqual(req.messages[0]["content"], REWRITE_SYSTEM_PROMPT)
        user = req.messages[1]["content"]
        self.assertIn("改写要求：\n翻译成英文并更正式", user)
        self.assertIn("输出语言：简体中文", user)
        self.assertIn("[0] a.txt (text/plain", user)
        self.assertIn("<<<MATERIAL\n原文素材\nMATERIAL>>>", user)

    def test_prepare_and_estimate(self) -> None:
        composer, _ = self._composer([])
        req = composer.prepare("compose", "写周报", "周报", "素材正文",
                               [AttachmentInfo(0, "a.pdf", "application/pdf", 2048)])
        self.assertEqual(req.messages[0]["role"], "system")
        user = req.messages[1]["content"]
        self.assertIn("指令：\n写周报", user)
        self.assertIn("主题参数：周报", user)
        self.assertIn("[0] a.pdf (application/pdf, 2 KB)", user)
        self.assertIn("<<<MATERIAL\n素材正文\nMATERIAL>>>", user)
        self.assertEqual(req.available_attachments, 1)
        self.assertGreater(composer.estimate_tokens(req), 1234)

    def test_run_uses_generate_settings_and_returns_spec(self) -> None:
        import json
        composer, client = self._composer([json.dumps(VALID)])
        req = composer.prepare("compose", "写", "", "", [AttachmentInfo(0, "a", "text/plain", 1)])
        spec, tokens = composer.run(req)
        self.assertEqual(spec.subject, "下周例会通知")
        self.assertEqual(tokens, 100)
        payload = client.payloads[0]
        self.assertEqual(payload["model"], "big")
        self.assertEqual(payload["max_tokens"], 1234)
        self.assertEqual(payload["response_format"], {"type": "json_object"})

    def test_run_retries_once_with_feedback(self) -> None:
        import json
        composer, client = self._composer(["这不是 JSON", json.dumps(VALID)])
        req = composer.prepare("compose", "写", "", "", [AttachmentInfo(0, "a", "text/plain", 1)])
        spec, tokens = composer.run(req)
        self.assertEqual(spec.subject, "下周例会通知")
        self.assertEqual(tokens, 200)                              # 两次调用合计
        retry_messages = client.payloads[1]["messages"]
        self.assertEqual(retry_messages[-2]["role"], "assistant")
        self.assertIn("上一次输出不合法", retry_messages[-1]["content"])

    def test_run_gives_up_after_second_failure(self) -> None:
        composer, _ = self._composer(["x", '{"subject": ""}'])
        req = composer.prepare("compose", "写", "", "", [])
        with self.assertRaises(ComposeError):
            composer.run(req)

    def test_summarize_forces_report_without_recipients(self) -> None:
        import json
        composer, _ = self._composer([json.dumps(_spec(template="card", to=["x@y.zz"]))])
        req = composer.prepare("summarize", "", "", "素材", [AttachmentInfo(0, "a", "text/plain", 1)])
        spec, _ = composer.run(req)
        self.assertEqual((spec.template, spec.to, spec.attachments), ("report", [], []))


class TestRendering(unittest.TestCase):
    def test_inline_markup_is_escaped_and_safe(self) -> None:
        out = inline_html('<script>x</script> **粗** [点我](https://a.b/c?d=1&e=2) [坏](javascript:alert(1))')
        self.assertIn("&lt;script&gt;", out)
        self.assertIn("<strong>粗</strong>", out)
        self.assertIn('<a href="https://a.b/c?d=1&amp;e=2"', out)
        self.assertNotIn("javascript:alert(1)\"", out)              # 非法链接不成为 href
        self.assertIn("[坏](javascript:alert(1))", out.replace("&#x27;", "'").replace("&quot;", '"')
                      or out)
        self.assertEqual(inline_text("**粗** [点我](https://a.b)"), "粗 点我 (https://a.b)")

    def test_render_spec_html_and_text(self) -> None:
        spec = parse_mail_spec(_spec(subject='<b>注入</b>'), SpecLimits(), available_attachments=1)
        html_out, text_out = render_spec(spec, preface="说明行")
        self.assertIn("&lt;b&gt;注入&lt;/b&gt;", html_out)
        self.assertNotIn("<b>注入</b>", html_out)
        self.assertIn("说明行", html_out)
        self.assertIn('href="https://cal.example.com/x"', html_out)
        self.assertIn("<th", html_out)
        self.assertIn("负责人", html_out)
        self.assertIn("## 时间", text_out)
        self.assertIn("- 带电脑", text_out)
        self.assertIn("1. 签到", text_out)
        self.assertIn("进度 | A", text_out)
        self.assertIn("打开日程: https://cal.example.com/x", text_out)
        self.assertTrue(text_out.startswith("说明行"))

    def test_build_draft_eml_structure(self) -> None:
        raw = build_draft_eml(
            from_addr="sakura@example.com", to_addrs=["a@x.y", "b@x.y"], subject="主题",
            html_body="<p>hi</p>", text_body="hi",
            attachments=[("r.txt", "text/plain", b"data"), ("fwd.eml", "message/rfc822", b"raw")],
        )
        msg = email.message_from_bytes(raw, policy=email.policy.default)
        self.assertEqual(str(msg["From"]), "sakura@example.com")
        self.assertEqual(str(msg["To"]), "a@x.y, b@x.y")
        self.assertEqual(str(msg["X-Leger-Draft"]), "1")
        self.assertEqual(msg.get_content_type(), "multipart/mixed")
        atts = list(msg.iter_attachments())
        self.assertEqual([a.get_filename() for a in atts], ["r.txt", "fwd.eml"])
        self.assertEqual(atts[0].get_content(), "data")
        self.assertEqual(atts[1].get_content_type(), "application/octet-stream")   # message/* 退化
        self.assertEqual(msg.get_body(("html",)).get_content().strip(), "<p>hi</p>")


if __name__ == "__main__":
    unittest.main()
