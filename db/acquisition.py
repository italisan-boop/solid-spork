from __future__ import annotations

import aiosqlite

from controlplane.plan_policy import FEATURE_CAMPAIGNS, LIMIT_CAMPAIGNS
from db.connection import connection


async def capture_campaign_first_touch(user_id: int, code: str) -> bool:
    if not code.startswith("c_"):
        return False
    campaign_code = code.removeprefix("c_")
    if not campaign_code or len(campaign_code) > 48:
        return False
    async with connection() as database:
        database.row_factory = aiosqlite.Row
        row = await (
            await database.execute(
                """
                SELECT channel, source, campaign
                FROM acquisition_campaigns
                WHERE code = ? AND is_active = 1
                """,
                (campaign_code,),
            )
        ).fetchone()
        if not row:
            return False
        await database.execute(
            """
            INSERT OR IGNORE INTO user_acquisition
                (user_id, channel, source, campaign)
            VALUES (?, ?, ?, ?)
            """,
            (user_id, row["channel"], row["source"], row["campaign"]),
        )
        await database.commit()
        return True


async def create_campaign(code: str, channel: str, source: str, campaign: str) -> bool:
    from runtime.context import maybe_current_tenant_context
    from runtime.features import require_feature
    from runtime.quota import require_count_quota

    if not code or len(code) > 48 or not channel or len(channel) > 48:
        raise ValueError("invalid campaign")
    if len(source) > 120 or len(campaign) > 120:
        raise ValueError("invalid campaign")
    tenant_context = maybe_current_tenant_context()
    if tenant_context is not None:
        require_feature(FEATURE_CAMPAIGNS, tenant_context)
    async with connection() as database:
        try:
            await database.execute("BEGIN IMMEDIATE")
            existing = await (
                await database.execute(
                    "SELECT 1 FROM acquisition_campaigns WHERE code = ?", (code,)
                )
            ).fetchone()
            if existing is None:
                cursor = await database.execute(
                    "SELECT COUNT(*) FROM acquisition_campaigns"
                )
                campaign_count = (await cursor.fetchone())[0]
                require_count_quota(
                    LIMIT_CAMPAIGNS, campaign_count, context=tenant_context
                )
            cursor = await database.execute(
                """
                INSERT INTO acquisition_campaigns (code, channel, source, campaign)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(code) DO UPDATE SET
                    channel = excluded.channel,
                    source = excluded.source,
                    campaign = excluded.campaign,
                    is_active = 1
                """,
                (code, channel, source, campaign),
            )
            await database.commit()
            return cursor.rowcount == 1
        except Exception:
            await database.rollback()
            raise
