from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from config import settings
from handlers.admin_commands import cmd_drop_cache, submit_drop_cache_code
from handlers.user import support_claims
from states import CriticalActionState
from utils.otp_confirm import ACTION_DROP_CACHE


class DropCacheSmokeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        support_claims.clear()
        settings.support_pending_users.clear()

    async def test_confirmed_drop_cache_clears_live_support_state(self):
        admin_id = 123
        prompt_message = SimpleNamespace(
            from_user=SimpleNamespace(id=admin_id),
            answer=AsyncMock(),
        )
        prompt_state = SimpleNamespace(
            set_state=AsyncMock(),
            update_data=AsyncMock(),
        )

        with (
            patch("handlers.admin_commands.is_admin", return_value=True),
            patch("handlers.admin_commands.issue_otp", return_value="654321") as issue_otp,
        ):
            await cmd_drop_cache(prompt_message, prompt_state)

        issue_otp.assert_called_once_with(admin_id, ACTION_DROP_CACHE)
        prompt_state.set_state.assert_awaited_once_with(CriticalActionState.waiting_for_code)
        prompt_state.update_data.assert_awaited_once_with(action=ACTION_DROP_CACHE)
        self.assertIn("654321", prompt_message.answer.await_args.args[0])

        support_claims[44] = admin_id
        settings.support_pending_users.add(44)
        confirmation_message = SimpleNamespace(
            from_user=SimpleNamespace(id=admin_id),
            text="654321",
            answer=AsyncMock(),
        )
        confirmation_state = SimpleNamespace(
            get_data=AsyncMock(return_value={"action": ACTION_DROP_CACHE}),
            clear=AsyncMock(),
        )

        with (
            patch("handlers.admin_commands.is_admin", return_value=True),
            patch("handlers.admin_commands.consume_otp", return_value=True) as consume_otp,
        ):
            await submit_drop_cache_code(confirmation_message, confirmation_state)

        consume_otp.assert_called_once_with(admin_id, ACTION_DROP_CACHE, "654321")
        self.assertEqual({}, support_claims)
        self.assertEqual(set(), settings.support_pending_users)
        confirmation_state.clear.assert_awaited_once()
        self.assertIn("Кэш поддержки сброшен", confirmation_message.answer.await_args.args[0])


if __name__ == "__main__":
    unittest.main()
