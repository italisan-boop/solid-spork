from aiogram import Router, F
from aiogram.types import CallbackQuery, Message
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.utils.keyboard import InlineKeyboardBuilder
import logging
from config.settings import settings
from db.users import get_all_users
from utils import sanitize_telegram_html

logger = logging.getLogger(__name__)
router = Router()


class BroadcastState(StatesGroup):
    waiting_for_message = State()
    waiting_for_confirmation = State()


@router.callback_query(F.data == "admin_broadcast")
async def start_broadcast(callback: CallbackQuery, state: FSMContext):
    """Начало процесса рассылки"""
    await state.clear()
    
    # Получаем количество пользователей
    users = await get_all_users()
    user_count = len(users) if users else 0
    
    builder = InlineKeyboardBuilder()
    builder.button(text="❌ Отмена", callback_data="admin_menu")
    builder.adjust(1)
    
    await callback.bot.edit_message_text(
        chat_id=callback.from_user.id,
        message_id=callback.message.message_id,
        text=(
            f"📢 <b>Рассылка сообщений пользователям</b>\n\n"
            f"Всего пользователей: {user_count}\n\n"
            "Отправьте текст сообщения для рассылки.\n\n"
            "Поддерживается Telegram-HTML:\n"
            "<code>&lt;b&gt;</code> <code>&lt;i&gt;</code> <code>&lt;u&gt;</code> "
            "<code>&lt;s&gt;</code> <code>&lt;code&gt;</code> <code>&lt;pre&gt;</code> "
            "<code>&lt;a href=\"...\"&gt;</code>\n"
            "Остальные теги будут автоматически удалены."
        ),
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )
    await state.set_state(BroadcastState.waiting_for_message)


@router.message(BroadcastState.waiting_for_message)
async def process_message(message: Message, state: FSMContext):
    """Обработка текста рассылки"""
    if message.text and message.text.strip():
        raw_text = message.text.strip()
        # Telegram HTML парсер принимает только ограниченный набор тегов;
        # прогоняем через санитайзер, чтобы <!doctype>, <script> и прочее
        # не валили рассылку с Bad Request: can't parse entities.
        text = sanitize_telegram_html(raw_text).strip()
        if not text:
            await message.answer(
                "❌ После очистки HTML текст пустой. Попробуйте еще раз:"
            )
            return
        await state.update_data(message_text=text)

        # Получаем количество пользователей
        users = await get_all_users()
        user_count = len(users) if users else 0

        builder = InlineKeyboardBuilder()
        builder.button(text="✅ Подтвердить рассылку", callback_data="admin_broadcast_confirm")
        builder.button(text="✏️ Изменить текст", callback_data="admin_broadcast_edit")
        builder.button(text="❌ Отмена", callback_data="admin_menu")
        builder.adjust(1)

        preview_text = text[:500] + "..." if len(text) > 500 else text

        # Показываем именно то, что увидят получатели — без дополнительной
        # обёртки <i>, чтобы пользователь сразу видел итоговый вид.
        await message.answer(
            f"📢 <b>Предпросмотр рассылки</b>\n\n"
            f"Получателей: {user_count}\n\n"
            f"{preview_text}\n\n"
            f"Подтвердите отправку или измените текст:",
            reply_markup=builder.as_markup(),
            parse_mode="HTML"
        )
        await state.set_state(BroadcastState.waiting_for_confirmation)
    else:
        await message.answer("❌ Сообщение не может быть пустым. Попробуйте еще раз:")


@router.callback_query(F.data == "admin_broadcast_edit")
async def edit_broadcast_message(callback: CallbackQuery, state: FSMContext):
    """Редактирование текста рассылки"""
    builder = InlineKeyboardBuilder()
    builder.button(text="❌ Отмена", callback_data="admin_menu")
    builder.adjust(1)
    
    await callback.bot.edit_message_text(
        chat_id=callback.from_user.id,
        message_id=callback.message.message_id,
        text=(
            "📢 <b>Рассылка сообщений пользователям</b>\n\n"
            "Отправьте новый текст сообщения:"
        ),
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )
    await state.set_state(BroadcastState.waiting_for_message)


@router.callback_query(F.data == "admin_broadcast_confirm")
async def confirm_broadcast(callback: CallbackQuery, state: FSMContext):
    """Подтверждение и отправка рассылки"""
    data = await state.get_data()
    text = data.get('message_text')
    
    if not text:
        await callback.bot.edit_message_text(
            chat_id=callback.from_user.id,
            message_id=callback.message.message_id,
            text="❌ Ошибка: текст сообщения не найден."
        )
        await state.clear()
        return
    
    # Получаем всех пользователей
    users = await get_all_users()
    
    if not users:
        await callback.bot.edit_message_text(
            chat_id=callback.from_user.id,
            message_id=callback.message.message_id,
            text="❌ Нет пользователей для рассылки."
        )
        await state.clear()
        return
    
    user_count = len(users)
    success_count = 0
    fail_count = 0
    
    # Обновляем сообщение о начале рассылки
    await callback.bot.edit_message_text(
        chat_id=callback.from_user.id,
        message_id=callback.message.message_id,
        text=(
            f"📢 <b>Запуск рассылки...</b>\n\n"
            f"Всего получателей: {user_count}\n"
            f"Отправлено: 0/{user_count}"
        ),
        parse_mode="HTML"
    )
    
    # Отправляем сообщения всем пользователям
    for i, user in enumerate(users):
        try:
            user_id = user['user_id']
            await callback.bot.send_message(
                chat_id=user_id,
                text=text,
                parse_mode="HTML"
            )
            success_count += 1
        except Exception as e:
            logger.error(f"Не удалось отправить сообщение пользователю {user['user_id']}: {e}")
            fail_count += 1
        
        # Обновляем прогресс каждые 10 сообщений
        if (i + 1) % 10 == 0 or i == user_count - 1:
            try:
                await callback.bot.edit_message_text(
                    chat_id=callback.from_user.id,
                    message_id=callback.message.message_id,
                    text=(
                        f"📢 <b>Рассылка в процессе...</b>\n\n"
                        f"Всего получателей: {user_count}\n"
                        f"Отправлено: {success_count + fail_count}/{user_count}\n"
                        f"Успешно: {success_count}\n"
                        f"Ошибок: {fail_count}"
                    ),
                    parse_mode="HTML"
                )
            except Exception:
                pass
    
    # Финальное сообщение
    builder = InlineKeyboardBuilder()
    builder.button(text="🔙 В меню админа", callback_data="admin_menu")
    builder.adjust(1)
    
    await callback.bot.edit_message_text(
        chat_id=callback.from_user.id,
        message_id=callback.message.message_id,
        text=(
            f"✅ <b>Рассылка завершена!</b>\n\n"
            f"Всего получателей: {user_count}\n"
            f"Успешно отправлено: {success_count}\n"
            f"Ошибок: {fail_count}\n\n"
            f"Сообщение: {text[:100]}..."
        ),
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )
    
    logger.info(f"Рассылка выполнена админом {callback.from_user.id}: {success_count} успешно, {fail_count} ошибок")
    await state.clear()


@router.callback_query(F.data == "admin_broadcast_cancel")
async def cancel_broadcast(callback: CallbackQuery, state: FSMContext):
    """Отмена рассылки"""
    await state.clear()
    
    builder = InlineKeyboardBuilder()
    builder.button(text="🔙 В меню админа", callback_data="admin_menu")
    builder.adjust(1)
    
    await callback.bot.edit_message_text(
        chat_id=callback.from_user.id,
        message_id=callback.message.message_id,
        text="❌ <b>Рассылка отменена</b>",
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )
