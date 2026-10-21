"""imap_worker：IMAP modified UTF-7 文件夹名编码（RFC 3501 §5.1.3）。

期望值与 scut.email 线上 LIST 输出逐字核对过（2026-10-21）。
"""
import unittest

from src.imap_worker import imap_utf7_encode


class TestImapUtf7(unittest.TestCase):
    def test_server_verified_folder_names(self) -> None:
        self.assertEqual(imap_utf7_encode("发件箱"), "&U9FO9nux-")
        self.assertEqual(imap_utf7_encode("收件箱"), "&ZTZO9nux-")
        self.assertEqual(imap_utf7_encode("草稿箱"), "&g0l6P3ux-")

    def test_ascii_passthrough_and_ampersand(self) -> None:
        self.assertEqual(imap_utf7_encode("Sent"), "Sent")
        self.assertEqual(imap_utf7_encode("Done 2026"), "Done 2026")
        self.assertEqual(imap_utf7_encode("A&B"), "A&-B")

    def test_mixed_ascii_and_cjk_single_base64_run(self) -> None:
        # 中文段整体进一个 base64 run，前后 ASCII 原样
        out = imap_utf7_encode("Re发件箱Box")
        self.assertTrue(out.startswith("Re"))
        self.assertTrue(out.endswith("Box"))
        self.assertIn("&", out)

    def test_empty(self) -> None:
        self.assertEqual(imap_utf7_encode(""), "")


if __name__ == "__main__":
    unittest.main()
