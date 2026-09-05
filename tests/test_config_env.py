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


if __name__ == "__main__":
    unittest.main()
