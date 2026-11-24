import os
import unittest
from unittest.mock import patch

from app import main


class SafeTestModeTests(unittest.TestCase):
    def test_safe_mode_blocks_webhook_registration(self):
        with patch.dict(os.environ, {"SAFE_TEST_MODE": "true", "TELEGRAM_BOT_TOKEN": "dummy-token"}):
            with patch("app.main.requests.post") as post:
                ok, note = main.configure_telegram_webhook()
        self.assertFalse(ok)
        self.assertIn("SAFE_TEST_MODE", note)
        post.assert_not_called()

    def test_safe_mode_blocks_outbound_telegram_messages(self):
        with patch.dict(os.environ, {"SAFE_TEST_MODE": "true", "TELEGRAM_BOT_TOKEN": "dummy-token"}):
            with patch("app.main.requests.post") as post:
                sent, note = main.telegram_send("test")
                sent_direct, direct_note = main.telegram_send_to_chat("123", "test")
        self.assertFalse(sent)
        self.assertFalse(sent_direct)
        self.assertIn("disabled", note)
        self.assertIn("disabled", direct_note)
        post.assert_not_called()

    def test_safe_mode_is_opt_in(self):
        with patch.dict(os.environ, {"SAFE_TEST_MODE": "false"}):
            self.assertFalse(main.safe_test_mode())
        with patch.dict(os.environ, {"SAFE_TEST_MODE": "true"}):
            self.assertTrue(main.safe_test_mode())


if __name__ == "__main__":
    unittest.main()
