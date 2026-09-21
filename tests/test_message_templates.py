import asyncio
from contextlib import ExitStack
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from content_defaults import TEMPLATES
import db.connection as db_connection
import db.message_templates as message_templates
import db.schema as schema
from db.message_templates import TemplateValidationError, validate_template_value
from utils import sanitize_telegram_html


class TemplateValidationTests(unittest.TestCase):
    def test_accepts_default_telegram_html(self):
        validate_template_value(
            "support.reply.header",
            "💬 <b>Ответ от поддержки «Семена Знаний»:</b>",
        )

    def test_branding_defaults_are_valid_telegram_html(self):
        branding = [template for template in TEMPLATES if template.group == "branding"]
        self.assertEqual(["branding.greeting", "branding.about"], [template.key for template in branding])
        for template in branding:
            validate_template_value(template.key, template.default)

    def test_rejects_orphaned_closing_tag(self):
        with self.assertRaises(TemplateValidationError):
            validate_template_value("support.quick.greeting", "Здравствуйте </b>")

    def test_rejects_unclosed_tag(self):
        with self.assertRaises(TemplateValidationError):
            validate_template_value("support.quick.greeting", "<b>Здравствуйте")


class TelegramHtmlSanitizerTests(unittest.TestCase):
    def test_removes_orphaned_closing_tag(self):
        self.assertEqual(sanitize_telegram_html("Текст </b>"), "Текст ")

    def test_removes_unclosed_opening_tag(self):
        self.assertEqual(sanitize_telegram_html("<b>Текст"), "Текст")

    def test_preserves_balanced_nested_tags(self):
        value = "<b>Жирный <i>и курсив</i></b>"
        self.assertEqual(sanitize_telegram_html(value), value)


class MessageTemplateStorageTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self._temporary_directory.name) / "templates.sqlite"
        self._patches = ExitStack()
        self._patches.enter_context(patch.object(schema, "DB_PATH", self.database_path))
        self._patches.enter_context(patch.object(db_connection, "DB_PATH", self.database_path))
        schema.initialize_database(self.database_path)

    def tearDown(self):
        self._patches.close()
        self._temporary_directory.cleanup()

    async def test_seeding_preserves_override_and_adds_missing_defaults(self):
        await message_templates.set_message_template("support.quick.greeting", "Добрый день!")
        schema.initialize_database(self.database_path)
        self.assertEqual(
            await message_templates.get_message_template("support.quick.greeting"),
            "Добрый день!",
        )
        self.assertEqual(
            await message_templates.get_message_template("branding.greeting"),
            next(template.default for template in TEMPLATES if template.key == "branding.greeting"),
        )


if __name__ == "__main__":
    unittest.main()
