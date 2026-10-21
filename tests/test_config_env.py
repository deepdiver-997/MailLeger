"""配置模块：${ENV} 展开、.env 不覆盖既有环境变量、fail-fast 校验。"""
import os
import tempfile
import unittest

from src import config as cfgmod
from src.config import ConfigError


class TestConfigEnv(unittest.TestCase):
    def test_env_expansion_and_missing_placeholder_to_empty(self) -> None:
        os.environ["MAILAGENT_TEST_VAR"] = "resolved"
        out = cfgmod._expand_env({
            "a": "${MAILAGENT_TEST_VAR}",
            "b": "${MAILAGENT_UNSET_VAR}",
            "c": ["x${MAILAGENT_TEST_VAR}"],
        })
        self.assertEqual(out["a"], "resolved")
        self.assertEqual(out["b"], "")  # 未定义 → 空串 + 警告，不炸
        self.assertEqual(out["c"], ["xresolved"])

    def test_dotenv_does_not_override_existing_env(self) -> None:
        os.environ["MAILAGENT_OVERRIDE"] = "keep"
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, ".env")
            with open(p, "w", encoding="utf-8") as f:
                f.write("MAILAGENT_OVERRIDE=overwrite\nMAILAGENT_OTHER=new\n")
            cfgmod.load_dotenv(p)
        self.assertEqual(os.environ["MAILAGENT_OVERRIDE"], "keep")
        self.assertEqual(os.environ.get("MAILAGENT_OTHER"), "new")
        os.environ.pop("MAILAGENT_OTHER", None)

    def test_parse_account_missing_required_raises(self) -> None:
        with self.assertRaises(ConfigError):
            cfgmod._parse_account({"imap_host": "imap.example.com"}, 0)


_AGENT_BASE = {
    "name": "leger", "role": "agent",
    "imap_host": "imap.example.com", "imap_user": "leger@example.com",
    "smtp_host": "smtp.example.com", "smtp_user": "leger@example.com",
    "allowed_senders": ["Sakura <Sakura@Example.com>", "sakura@example.com", "other@example.com"],
}


class TestAgentAccountConfig(unittest.TestCase):
    def test_agent_normalizes_and_dedupes_whitelist_and_needs_no_notification_to(self) -> None:
        acc = cfgmod._parse_account(dict(_AGENT_BASE), 0)
        self.assertEqual(acc.role, "agent")
        self.assertEqual(acc.allowed_senders, ["sakura@example.com", "other@example.com"])
        self.assertEqual(acc.default_command, "help")
        self.assertEqual(acc.reply_from, "leger@example.com")

    def test_whitelist_accepts_comma_string(self) -> None:
        raw = dict(_AGENT_BASE, allowed_senders="a@x.y, B@X.Y")
        self.assertEqual(cfgmod._parse_account(raw, 0).allowed_senders, ["a@x.y", "b@x.y"])

    def test_agent_requires_whitelist(self) -> None:
        with self.assertRaises(ConfigError):
            cfgmod._parse_account(dict(_AGENT_BASE, allowed_senders=[]), 0)

    def test_agent_requires_smtp_host(self) -> None:
        with self.assertRaises(ConfigError):
            cfgmod._parse_account(dict(_AGENT_BASE, smtp_host=""), 0)

    def test_agent_rejects_self_in_whitelist(self) -> None:
        with self.assertRaises(ConfigError):
            cfgmod._parse_account(dict(_AGENT_BASE, allowed_senders=["LEGER@example.com"]), 0)

    def test_invalid_role_and_default_command(self) -> None:
        with self.assertRaises(ConfigError):
            cfgmod._parse_account(dict(_AGENT_BASE, role="robot"), 0)
        with self.assertRaises(ConfigError):
            cfgmod._parse_account(dict(_AGENT_BASE, default_command="status"), 0)
        self.assertEqual(cfgmod._parse_account(dict(_AGENT_BASE, default_command="#Compose"), 0).default_command,
                         "compose")

    def test_monitor_still_requires_notification_to(self) -> None:
        raw = {"name": "m", "imap_host": "h", "imap_user": "u@x.y"}
        with self.assertRaises(ConfigError):
            cfgmod._parse_account(raw, 0)
        acc = cfgmod._parse_account(dict(raw, notification_to="to@x.y"), 0)
        self.assertEqual(acc.role, "monitor")
        self.assertEqual(acc.allowed_senders, [])

    def test_reply_language_optional(self) -> None:
        self.assertEqual(cfgmod._parse_account(dict(_AGENT_BASE), 0).reply_language, "")
        self.assertEqual(cfgmod._parse_account(dict(_AGENT_BASE, reply_language="简体中文"), 0).reply_language,
                         "简体中文")

    def test_normalize_address(self) -> None:
        self.assertEqual(cfgmod.normalize_address('"Sakura" <SAKURA@Example.com>'), "sakura@example.com")
        self.assertEqual(cfgmod.normalize_address("  plain@x.y "), "plain@x.y")
        self.assertEqual(cfgmod.normalize_address(None), "")


if __name__ == "__main__":
    unittest.main()
