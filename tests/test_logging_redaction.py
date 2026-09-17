import io
import logging
import unittest

from utils.logger import RedactingFilter


class LoggingRedactionTests(unittest.TestCase):
    def test_redacts_tokens_init_data_cards_and_phone_numbers(self):
        record = logging.LogRecord(
            "test", logging.ERROR, __file__, 1,
            "token 123456:secret-value X-Telegram-Init-Data=raw-data card 2200123412341234 phone +79991234567", (), None,
        )
        self.assertTrue(RedactingFilter().filter(record))
        self.assertNotIn("secret-value", record.msg)
        self.assertNotIn("raw-data", record.msg)
        self.assertNotIn("2200123412341234", record.msg)
        self.assertNotIn("79991234567", record.msg)


if __name__ == "__main__":
    unittest.main()
