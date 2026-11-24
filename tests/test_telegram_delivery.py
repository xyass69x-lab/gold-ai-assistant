import unittest
from unittest.mock import Mock, patch

from app.main import _telegram_message_chunks, telegram_send_to_chat


class TelegramDeliveryTests(unittest.TestCase):
    def test_long_messages_are_split_below_telegram_limit(self):
        chunks = _telegram_message_chunks(("A" * 3900) + "\n" + ("B" * 3900))
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= 4000 for chunk in chunks))
        self.assertEqual("".join(chunks).replace("\n", ""), ("A" * 3900) + ("B" * 3900))

    @patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "test-token"})
    @patch("app.main.requests.post")
    def test_send_failure_returns_telegram_description(self, post):
        response = Mock()
        response.ok = False
        response.status_code = 400
        response.text = '{"ok":false}'
        response.json.return_value = {
            "ok": False,
            "error_code": 400,
            "description": "Bad Request: chat not found",
        }
        post.return_value = response

        sent, note = telegram_send_to_chat("123", "hello")

        self.assertFalse(sent)
        self.assertIn("chat not found", note)


if __name__ == "__main__":
    unittest.main()
