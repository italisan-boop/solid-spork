from __future__ import annotations

import re

from db.schema import connect


_COLOR_PATTERN = re.compile(r"#[0-9a-fA-F]{6}\Z")
_STOREFRONT_FIELDS = {"store_name", "primary_color", "accent_color", "support_contact"}


def get_storefront_settings_sync() -> dict[str, str | None]:
    database = connect()
    try:
        row = database.execute(
            """
            SELECT store_name, primary_color, accent_color, logo_asset_id, support_contact
            FROM storefront_settings WHERE id = 1
            """
        ).fetchone()
        if row is None:
            return {
                "store_name": "",
                "primary_color": "#2f7d4a",
                "accent_color": "#f2b84b",
                "logo_asset_id": None,
                "support_contact": "",
            }
        return {
            "store_name": row[0],
            "primary_color": row[1],
            "accent_color": row[2],
            "logo_asset_id": row[3],
            "support_contact": row[4],
        }
    finally:
        database.close()


def storefront_payload(payload: object) -> dict[str, str]:
    if not isinstance(payload, dict) or set(payload) != _STOREFRONT_FIELDS:
        raise ValueError("invalid storefront configuration")
    name = payload["store_name"]
    primary = payload["primary_color"]
    accent = payload["accent_color"]
    contact = payload["support_contact"]
    if (
        not isinstance(name, str) or not 1 <= len(name.strip()) <= 120
        or not isinstance(primary, str) or not _COLOR_PATTERN.fullmatch(primary)
        or not isinstance(accent, str) or not _COLOR_PATTERN.fullmatch(accent)
        or not isinstance(contact, str) or len(contact.strip()) > 160
    ):
        raise ValueError("invalid storefront configuration")
    return {
        "store_name": name.strip(),
        "primary_color": primary.lower(),
        "accent_color": accent.lower(),
        "support_contact": contact.strip(),
    }


def save_storefront_settings_sync(payload: object) -> dict[str, str | None]:
    values = storefront_payload(payload)
    database = connect()
    try:
        database.execute("BEGIN IMMEDIATE")
        database.execute(
            """
            INSERT INTO storefront_settings (
                id, store_name, primary_color, accent_color, support_contact, updated_at
            ) VALUES (1, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(id) DO UPDATE SET
                store_name = excluded.store_name,
                primary_color = excluded.primary_color,
                accent_color = excluded.accent_color,
                support_contact = excluded.support_contact,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                values["store_name"],
                values["primary_color"],
                values["accent_color"],
                values["support_contact"],
            ),
        )
        database.commit()
    except Exception:
        database.rollback()
        raise
    finally:
        database.close()
    return get_storefront_settings_sync()
