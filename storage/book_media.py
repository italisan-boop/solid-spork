from __future__ import annotations

import asyncio
import hashlib
import io
import sqlite3
import uuid
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

from config import settings
from db.schema import connect
from runtime.context import maybe_current_tenant_context


MAX_SOURCE_BYTES = 10 * 1024 * 1024
MAX_PIXELS = 16_000_000
THUMBNAIL_SIZE = (400, 600)
DISPLAY_SIZE = (1200, 1800)


class BookMediaError(ValueError):
    pass


def _media_root() -> Path:
    context = maybe_current_tenant_context()
    if context is not None:
        root = context.media_root
    else:
        value = settings.BOOK_MEDIA_ROOT
        if not value:
            raise BookMediaError("BOOK_MEDIA_ROOT is not configured")
        root = Path(value).expanduser()
    if not root.is_absolute():
        raise BookMediaError("BOOK_MEDIA_ROOT must be absolute")
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve()


def _webp_image(source: Image.Image, bounds: tuple[int, int]) -> Image.Image:
    image = ImageOps.exif_transpose(source).convert("RGB")
    image.thumbnail(bounds, Image.Resampling.LANCZOS)
    return image


def _decode_image(data: bytes) -> Image.Image:
    if not data or len(data) > MAX_SOURCE_BYTES:
        raise BookMediaError("image exceeds the 10 MB limit")
    try:
        with Image.open(io.BytesIO(data)) as check:
            check.verify()
        image = Image.open(io.BytesIO(data))
        if image.width * image.height > MAX_PIXELS:
            image.close()
            raise BookMediaError("image exceeds the 16 megapixel limit")
        image.load()
        return image
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise BookMediaError("unsupported or invalid image") from exc


def _save_variant(root: Path, asset_id: str, variant: str, image: Image.Image) -> str:
    filename = f"{asset_id}-{variant}.webp"
    target = root / filename
    image.save(target, format="WEBP", quality=82, method=6)
    return filename


def attach_book_media_sync(
    book_id: int,
    role: str,
    position: int,
    data: bytes,
) -> dict:
    if role not in {"cover", "page"} or position < 0:
        raise BookMediaError("invalid media role")
    source = _decode_image(data)
    try:
        asset_id = uuid.uuid4().hex
        root = _media_root()
        display = _webp_image(source, DISPLAY_SIZE)
        thumbnail = _webp_image(source, THUMBNAIL_SIZE)
        width, height = display.width, display.height
        try:
            display_filename = _save_variant(root, asset_id, "display", display)
            thumbnail_filename = _save_variant(root, asset_id, "thumb", thumbnail)
        finally:
            display.close()
            thumbnail.close()
        database = connect()
        try:
            database.execute("BEGIN IMMEDIATE")
            if not database.execute("SELECT 1 FROM books WHERE id = ?", (book_id,)).fetchone():
                raise BookMediaError("book not found")
            existing = database.execute(
                """
                SELECT asset_id, thumbnail_filename, display_filename
                FROM book_media WHERE book_id = ? AND role = ? AND position = ?
                """,
                (book_id, role, position),
            ).fetchone()
            database.execute(
                """
                INSERT INTO book_media (
                    asset_id, book_id, role, position, thumbnail_filename, display_filename,
                    width, height, content_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(book_id, role, position) DO UPDATE SET
                    asset_id = excluded.asset_id,
                    thumbnail_filename = excluded.thumbnail_filename,
                    display_filename = excluded.display_filename,
                    width = excluded.width,
                    height = excluded.height,
                    content_hash = excluded.content_hash,
                    created_at = CURRENT_TIMESTAMP
                """,
                (
                    asset_id,
                    book_id,
                    role,
                    position,
                    thumbnail_filename,
                    display_filename,
                    width,
                    height,
                    hashlib.sha256(data).hexdigest(),
                ),
            )
            database.commit()
        except Exception:
            database.rollback()
            for filename in (thumbnail_filename, display_filename):
                (root / filename).unlink(missing_ok=True)
            raise
        finally:
            database.close()
        if existing:
            for filename in (existing[1], existing[2]):
                (root / filename).unlink(missing_ok=True)
        return {
            "asset_id": asset_id,
            "role": role,
            "position": position,
            "width": width,
            "height": height,
        }
    finally:
        source.close()


async def attach_telegram_media(
    bot, book_id: int, role: str, position: int, file_id: str
) -> dict:
    file = await bot.get_file(file_id)
    payload = await bot.download_file(file.file_path)
    data = payload.getvalue()
    return await asyncio.to_thread(attach_book_media_sync, book_id, role, position, data)


def media_variants_for_book_sync(book_id: int) -> dict:
    database = connect()
    database.row_factory = sqlite3.Row
    try:
        rows = database.execute(
            """
            SELECT asset_id, role, position, width, height
            FROM book_media WHERE book_id = ?
            ORDER BY CASE role WHEN 'cover' THEN 0 ELSE 1 END, position
            """,
            (book_id,),
        ).fetchall()
        cover = None
        pages = []
        for row in rows:
            item = {
                "id": row["asset_id"],
                "thumbnail_url": f"/media/books/{row['asset_id']}/thumbnail",
                "display_url": f"/media/books/{row['asset_id']}/display",
                "width": row["width"],
                "height": row["height"],
            }
            if row["role"] == "cover":
                cover = item
            else:
                pages.append(item)
        return {"cover": cover, "pages": pages}
    finally:
        database.close()


def list_book_media_sync(book_id: int) -> list[dict]:
    database = connect()
    database.row_factory = sqlite3.Row
    try:
        rows = database.execute(
            """
            SELECT asset_id, role, position, width, height
            FROM book_media
            WHERE book_id = ?
            ORDER BY CASE role WHEN 'cover' THEN 0 ELSE 1 END, position
            """,
            (book_id,),
        ).fetchall()
        return [
            {
                "asset_id": row["asset_id"],
                "role": row["role"],
                "position": row["position"],
                "thumbnail_url": f"/media/books/{row['asset_id']}/thumbnail",
                "display_url": f"/media/books/{row['asset_id']}/display",
                "width": row["width"],
                "height": row["height"],
            }
            for row in rows
        ]
    finally:
        database.close()


def next_page_position_sync(book_id: int) -> int:
    database = connect()
    try:
        row = database.execute(
            "SELECT COALESCE(MAX(position) + 1, 0) FROM book_media WHERE book_id = ? AND role = 'page'",
            (book_id,),
        ).fetchone()
        return row[0]
    finally:
        database.close()


def _remove_variants(root: Path, rows: list[tuple[str, str]]) -> None:
    for thumbnail_filename, display_filename in rows:
        for filename in (thumbnail_filename, display_filename):
            try:
                path = (root / filename).resolve()
                if path.parent == root:
                    path.unlink(missing_ok=True)
            except OSError:
                pass


def delete_book_media_sync(book_id: int, asset_id: str) -> bool:
    if len(asset_id) != 32 or any(char not in "0123456789abcdef" for char in asset_id):
        raise BookMediaError("invalid media asset")
    database = connect()
    root = _media_root()
    files: list[tuple[str, str]] = []
    try:
        database.execute("BEGIN IMMEDIATE")
        row = database.execute(
            """
            SELECT role, position, thumbnail_filename, display_filename
            FROM book_media WHERE book_id = ? AND asset_id = ?
            """,
            (book_id, asset_id),
        ).fetchone()
        if row is None:
            database.commit()
            return False
        role, position, thumbnail_filename, display_filename = row
        database.execute("DELETE FROM book_media WHERE asset_id = ?", (asset_id,))
        if role == "page":
            database.execute(
                "UPDATE book_media SET position = position + 1000000 WHERE book_id = ? AND role = 'page' AND position > ?",
                (book_id, position),
            )
            database.execute(
                "UPDATE book_media SET position = position - 1000001 WHERE book_id = ? AND role = 'page' AND position >= 1000000",
                (book_id,),
            )
        database.commit()
        files.append((thumbnail_filename, display_filename))
        return True
    except Exception:
        database.rollback()
        raise
    finally:
        database.close()
        _remove_variants(root, files)


def clear_book_media_sync(book_id: int, role: str | None = None) -> int:
    if role is not None and role not in {"cover", "page"}:
        raise BookMediaError("invalid media role")
    database = connect()
    root = _media_root()
    files: list[tuple[str, str]] = []
    try:
        database.execute("BEGIN IMMEDIATE")
        where = "WHERE book_id = ?" if role is None else "WHERE book_id = ? AND role = ?"
        params = (book_id,) if role is None else (book_id, role)
        rows = database.execute(
            f"SELECT thumbnail_filename, display_filename FROM book_media {where}", params
        ).fetchall()
        database.execute(f"DELETE FROM book_media {where}", params)
        database.commit()
        files = [(row[0], row[1]) for row in rows]
        return len(files)
    except Exception:
        database.rollback()
        raise
    finally:
        database.close()
        _remove_variants(root, files)


def resolve_media_variant_sync(asset_id: str, variant: str) -> tuple[Path, str] | None:
    if variant not in {"thumbnail", "display"} or len(asset_id) != 32:
        return None
    database = connect()
    try:
        row = database.execute(
            """
            SELECT thumbnail_filename, display_filename, mime_type
            FROM book_media WHERE asset_id = ?
            """,
            (asset_id,),
        ).fetchone()
        if not row:
            return None
        filename = row[0] if variant == "thumbnail" else row[1]
        root = _media_root()
        path = (root / filename).resolve()
        if path.parent != root or not path.is_file():
            return None
        return path, row[2]
    finally:
        database.close()
