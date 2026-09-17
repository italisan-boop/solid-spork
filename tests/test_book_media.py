import io
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from PIL import Image

import db.connection as db_connection
import db.schema as schema
from config import settings
from storage.book_media import (
    attach_book_media_sync,
    clear_book_media_sync,
    delete_book_media_sync,
    list_book_media_sync,
    media_variants_for_book_sync,
    next_page_position_sync,
    resolve_media_variant_sync,
)


def image_bytes(color: str) -> bytes:
    image = Image.new("RGB", (80, 120), color)
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


class BookMediaTests(unittest.TestCase):
    def setUp(self):
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary_directory.name)
        self.database_path = self.root / "media.sqlite"
        self.media_root = self.root / "media"
        self._patches = ExitStack()
        self._patches.enter_context(patch.object(schema, "DB_PATH", self.database_path))
        self._patches.enter_context(patch.object(db_connection, "DB_PATH", self.database_path))
        self._patches.enter_context(patch.object(settings, "BOOK_MEDIA_ROOT", str(self.media_root)))
        schema.initialize_database(self.database_path, seed_catalog=False)
        connection = schema.connect(self.database_path)
        try:
            connection.execute("INSERT INTO books (id, title, price) VALUES (1, 'Книга', 100)")
            connection.commit()
        finally:
            connection.close()

    def tearDown(self):
        self._patches.close()
        self._temporary_directory.cleanup()

    def test_native_page_media_is_ordered_reindexed_and_removed(self):
        cover = attach_book_media_sync(1, "cover", 0, image_bytes("red"))
        first = attach_book_media_sync(1, "page", 0, image_bytes("green"))
        second = attach_book_media_sync(1, "page", 1, image_bytes("blue"))
        self.assertEqual(2, next_page_position_sync(1))
        self.assertEqual(["cover", "page", "page"], [item["role"] for item in list_book_media_sync(1)])
        self.assertTrue(resolve_media_variant_sync(cover["asset_id"], "display")[0].is_file())

        self.assertTrue(delete_book_media_sync(1, first["asset_id"]))
        pages = [item for item in list_book_media_sync(1) if item["role"] == "page"]
        self.assertEqual([(second["asset_id"], 0)], [(item["asset_id"], item["position"]) for item in pages])
        self.assertEqual(1, next_page_position_sync(1))
        self.assertIsNone(resolve_media_variant_sync(first["asset_id"], "display"))

        self.assertEqual(1, clear_book_media_sync(1, "page"))
        self.assertEqual([], media_variants_for_book_sync(1)["pages"])
        self.assertEqual(1, clear_book_media_sync(1, "cover"))
        self.assertIsNone(media_variants_for_book_sync(1)["cover"])


if __name__ == "__main__":
    unittest.main()
