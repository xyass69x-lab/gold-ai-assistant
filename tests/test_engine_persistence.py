import tempfile
import unittest
from pathlib import Path

from app import main


class EnginePersistenceTests(unittest.TestCase):
    def test_no_signal_does_not_consume_candle(self):
        original_db_path = main.DB_PATH
        try:
            with tempfile.TemporaryDirectory() as tmp:
                main.DB_PATH = Path(tmp) / "signals.db"
                main.init_db()
                saved = main.persist_engine_result(
                    "2026-11-24T10:00:00+00:00",
                    None,
                    None,
                    "test",
                )
                self.assertFalse(saved)
                with main._db() as conn:
                    count = conn.execute(
                        "SELECT COUNT(*) FROM processed_candles"
                    ).fetchone()[0]
                self.assertEqual(count, 0)
        finally:
            main.DB_PATH = original_db_path


if __name__ == "__main__":
    unittest.main()
