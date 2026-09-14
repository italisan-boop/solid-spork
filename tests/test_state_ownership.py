from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from config.settings import Settings, settings
from handlers.admin_broadcast import BroadcastState, broadcast_start_ui
from handlers.admin_commands import submit_drop_cache_code
from handlers.user import support_claims
from utils.otp_confirm import ACTION_DROP_CACHE


class StateOwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        support_claims.clear()
        settings.support_pending_users.clear()

    def test_settings_exposes_only_active_support_cache(self):
        configured = Settings()
        self.assertTrue(hasattr(configured, "support_pending_users"))
        self.assertFalse(hasattr(configured, "support_claims"))
        self.assertFalse(hasattr(configured, "broadcast_pending_users"))

    async def test_confirmed_drop_cache_clears_live_ticket_claims(self):
        support_claims[101] = 202
        settings.support_pending_users.add(101)
        message = SimpleNamespace(
            from_user=SimpleNamespace(id=202),
            text="123456",
            answer=AsyncMock(),
        )
        state = SimpleNamespace(
            get_data=AsyncMock(return_value={"action": ACTION_DROP_CACHE}),
            clear=AsyncMock(),
        )

        with (
            patch("handlers.admin_commands.is_admin", return_value=True),
            patch("handlers.admin_commands.consume_otp", return_value=True),
        ):
            await submit_drop_cache_code(message, state)

        self.assertEqual({}, support_claims)
        self.assertEqual(set(), settings.support_pending_users)
        state.clear.assert_awaited_once()

    async def test_broadcast_starts_through_fsm_without_pending_set(self):
        message = SimpleNamespace(answer=AsyncMock())
        state = SimpleNamespace(clear=AsyncMock(), set_state=AsyncMock())

        with patch("handlers.admin_broadcast.get_all_users", new_callable=AsyncMock, return_value=[]):
            await broadcast_start_ui(message, state)

        state.clear.assert_awaited_once()
        state.set_state.assert_awaited_once_with(BroadcastState.waiting_for_message)
        self.assertFalse(hasattr(settings, "broadcast_pending_users"))


if __name__ == "__main__":
    unittest.main()
