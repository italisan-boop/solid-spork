from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from config import settings
from handlers.admin_commands import cb_reset_referrals, cmd_drop_cache, submit_drop_cache_code
from handlers.user import support_claims
from states import CriticalActionState
from utils.otp_confirm import ACTION_DROP_CACHE, ACTION_RESET_REFERRALS


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
            patch("handlers.admin_commands.db.clear_support_history", new_callable=AsyncMock) as clear_history,
            patch("handlers.admin_commands.db.clear_support_active_users", new_callable=AsyncMock) as clear_active,
        ):
            await submit_drop_cache_code(confirmation_message, confirmation_state)

        consume_otp.assert_called_once_with(admin_id, ACTION_DROP_CACHE, "654321")
        clear_history.assert_awaited_once()
        clear_active.assert_awaited_once()
        self.assertEqual({}, support_claims)
        self.assertEqual(set(), settings.support_pending_users)
        confirmation_state.clear.assert_awaited_once()
        self.assertIn("Кэш и история поддержки сброшены", confirmation_message.answer.await_args.args[0])

    async def test_confirmed_referral_reset_deletes_only_referral_links(self):
        admin_id = 123
        callback = SimpleNamespace(
            from_user=SimpleNamespace(id=admin_id),
            message=SimpleNamespace(answer=AsyncMock()),
            answer=AsyncMock(),
        )
        state = SimpleNamespace(set_state=AsyncMock(), update_data=AsyncMock())
        with (
            patch("handlers.admin_commands.is_admin", return_value=True),
            patch("handlers.admin_commands.issue_otp", return_value="123456") as issue_otp,
        ):
            await cb_reset_referrals(callback, state)

        issue_otp.assert_called_once_with(admin_id, ACTION_RESET_REFERRALS)
        state.set_state.assert_awaited_once_with(CriticalActionState.waiting_for_code)
        state.update_data.assert_awaited_once_with(action=ACTION_RESET_REFERRALS)
        self.assertIn("Бонусы, пользователи, заказы и кампании сохранятся", callback.message.answer.await_args.args[0])

        confirmation_message = SimpleNamespace(
            from_user=SimpleNamespace(id=admin_id), text="123456", answer=AsyncMock()
        )
        confirmation_state = SimpleNamespace(
            get_data=AsyncMock(return_value={"action": ACTION_RESET_REFERRALS}), clear=AsyncMock()
        )
        with (
            patch("handlers.admin_commands.is_admin", return_value=True),
            patch("handlers.admin_commands.consume_otp", return_value=True) as consume_otp,
            patch("handlers.admin_commands.db.clear_referrals", new_callable=AsyncMock, return_value=4) as clear_referrals,
            patch("handlers.admin_commands.db.clear_support_history", new_callable=AsyncMock) as clear_history,
        ):
            await submit_drop_cache_code(confirmation_message, confirmation_state)

        consume_otp.assert_called_once_with(admin_id, ACTION_RESET_REFERRALS, "123456")
        clear_referrals.assert_awaited_once()
        clear_history.assert_not_awaited()
        confirmation_state.clear.assert_awaited_once()
        self.assertIn("Реферальные связи сброшены: 4", confirmation_message.answer.await_args.args[0])


if __name__ == "__main__":
    unittest.main()
