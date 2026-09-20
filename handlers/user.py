import json
import asyncio
import time
import html
from collections import deque
from aiogram import Bot, Router, F
from aiogram.types import Message, WebAppInfo, CallbackQuery, LabeledPrice
from aiogram.filters import CommandStart, Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.context import FSMContext
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

import db
from db.orders import format_order_receipt_html
from content_defaults import QUICK_TEMPLATE_KEYS
from authz import has_permission_sync, recipient_ids_for_event_sync
from config import settings
from states import AddBookState, DeliverySettingsState, EditBookState, CategoryState, PromoCodeState, ReferralState, PaymentSettingsState
from utils import format_local_time, parseBookImages, setup_logger
from utils.otp_confirm import revoke_otp

router = Router()
logger = setup_logger(__name__)

# Карта закреплённых тикетов поддержки: user_id -> admin_id.
# Если пользователь в диалоге и е��о тикет закреплён — сообщения летят
# только эт��му админу, чтобы коллеги не отвечали параллельно.
# Живёт в памяти процесса (рядом с support_pending_users), при рестарте
# обнуляется — это сознательно: никто не окажется «вечно залоченным».
support_claims: dict[int, int] = {}

# Краткий runtime-кэш истории поддержки: user_id → последние сообщения.
# Постоянный источник истории — таблица support_messages; кэш нужен только
# для текущего процесса и live-уведомлений.
HISTORY_LIMIT = 50
support_history: dict[int, list[dict]] = {}

# === РЕЙТ-ЛИМИТ НА СООБЩЕНИЯ ПОДДЕРЖКИ ===
# Защита от спама в обе стороны:
#  - админ → пользователь: пауза REPLY_MIN_GAP сек между двумя
#    ответами одному пользователю + не больше REPLY_BURST_LIMIT ответов в
#    скользящем окне REPLY_WINDOW на одного админа;
#  - пользователь → поддержка: те же правила на одного пользователя.
# Всё живёт в памяти процесса (как support_claims) — при рестарте
# обнуляется, это нормально.
REPLY_MIN_GAP = 3.0        # сек между сообщениями одному получателю
REPLY_BURST_LIMIT = 8      # макс. сообщений в скользящем окне
REPLY_WINDOW = 60.0        # длина окна, сек
reply_last_user_ts: dict[tuple[int, int], float] = {}
reply_admin_log: dict[int, deque] = {}
user_msg_last_ts: dict[int, float] = {}
user_msg_log: dict[int, deque] = {}


def _rate_limited(gap_log: dict, burst_log: dict, gap_key, burst_key,
                  gap_s: float = REPLY_MIN_GAP,
                  burst: int = REPLY_BURST_LIMIT,
                  window: float = REPLY_WINDOW):
    """Проверка рейт-лимита на отправку сообщения. Возвращает текст ошибки
    (лимит превышен) или None, если можно отправлять. Успех фиксируется
    в словарях gap_log/burst_log — их ключи должны быть раздельными.
    """
    now = time.monotonic()
    last = gap_log.get(gap_key, 0.0)
    if now - last < gap_s:
        wait = int(gap_s - (now - last)) + 1
        return f"⏳ Слишком часто: подождите {wait} сек."
    log = burst_log.setdefault(burst_key, deque())
    while log and now - log[0] > window:
        log.popleft()
    if len(log) >= burst:
        return f"⏳ Превышен лимит: не больше {burst} сообщений за минуту."
    log.append(now)
    gap_log[gap_key] = now
    return None

# === ЭСКАЛАЦИЯ НЕОТВЕЧЕННЫХ ТИКЕТОВ ===
# Последнее сообщение пользователя запоминаем вместе с меткой времени. Если
# админ не ответил за ESCALATION_TIMEOUT секунд, фоновый поллер
# support_escalation_loop() дублирует текст всем админам, чтобы тикет не
# завис на одном закреплённом админе. Как только админ отвечает — трекинг
# сбрасывается. Всё живёт в памяти процесса (рядом с support_claims) и
# обнуляется при рестарте — это нормально: эскалация важна для «здесь и сейчас».
ESCALATION_TIMEOUT = 60
ESCALATION_POLL_INTERVAL = 15
support_last_msg_ts: dict[int, float] = {}
support_last_msg_text: dict[int, str] = {}
support_last_msg_name: dict[int, str] = {}
support_escalated: dict[int, bool] = {}

# ID сообщений, которые бот разослал админам по тикету пользователя:
#  - support_forward_msgs: обычные пересылки сообщения из universal_text_handler;
#  - support_escalation_msgs: дубли со значком «Эскалация» от поллера.
# Когда кто-то берёт тикет в работу кнопкой или отвечает —
# все эти сообщения редактируются в «тикет уже в работе / на него ответили»,
# чтобы остальные админы не путались и не отвечали параллельно.
# user_id -> список кортежей (chat_id, message_id).
support_forward_msgs: dict[int, list[tuple[int, int]]] = {}
support_escalation_msgs: dict[int, list[tuple[int, int]]] = {}

# Обратная карта для нативного «Ответить» в Telegram: (chat_id, message_id) -> user_id.
# Заполняется для каждого сообщения, которое бот разослал админам по тикету
# (пересылки, эскалации) и для каждого ответа админа. По этой карте бот по
# reply_to_message находит, к пользователю какого тикета относится ответ.
support_msg_owner: dict[tuple[int, int], int] = {}

# Максимум отслеживаемых уведомлений на один тикет — режем, чтобы
# при долгом молчащемся диалоге список не разрастался.
MAX_TRACKED_ADMIN_MSGS = 40


def _track_support_message(user_id: int, name: str, text: str) -> None:
    """Запомнить последнее сообщение пользователя для эскалации."""
    support_last_msg_ts[user_id] = time.time()
    support_last_msg_text[user_id] = text
    support_last_msg_name[user_id] = name
    support_escalated[user_id] = False


def _clear_support_tracking(user_id: int) -> None:
    """Сбросить трекинг эскалации (админ ответил / юзер вышел из поддержки)."""
    support_last_msg_ts.pop(user_id, None)
    support_last_msg_text.pop(user_id, None)
    support_last_msg_name.pop(user_id, None)
    support_escalated.pop(user_id, None)
    support_forward_msgs.pop(user_id, None)
    support_escalation_msgs.pop(user_id, None)


def _track_admin_msg(user_id: int, chat_id: int, message_id: int,
                     bucket: dict[int, list[tuple[int, int]]]) -> None:
    """Запомнить ID разосланного админу уведомления, чтобы потом его переписать."""
    msgs = bucket.setdefault(user_id, [])
    msgs.append((chat_id, message_id))
    if len(msgs) > MAX_TRACKED_ADMIN_MSGS:
        bucket[user_id] = msgs[-MAX_TRACKED_ADMIN_MSGS:]
    support_msg_owner[(chat_id, message_id)] = user_id


async def _rewrite_ticket_notices(user_id: int, bot: Bot, status_text: str) -> None:
    """Переписать все уведомления админов по тикету (пересылки + эскалации).

    Вызывается, когда тикет берут в работу кнопкой или на него уже
    ответили: вместо «ждите / возьмите в работу» админы видят актуальный статус,
    чтобы не отвечали параллельно.
    """
    buckets = (support_forward_msgs.pop(user_id, []),
               support_escalation_msgs.pop(user_id, []))
    for chat_id, message_id in sum(buckets, []):
        try:
            await bot.edit_message_text(
                text=status_text,
                chat_id=chat_id,
                message_id=message_id,
                reply_markup=_ticket_action_markup(user_id),
                parse_mode="HTML",
            )
        except Exception as e:
            logger.warning(
                f"Не удалось обновить уведомление по тикету {user_id} "
                f"({chat_id}/{message_id}): {e}"
            )


async def _append_history(user_id: int, role: str, name: str, text: str) -> None:
    """Сохранить сообщение поддержки в БД и кратком runtime-кэше."""
    await db.append_support_message(user_id, role, name, text)
    support_history.setdefault(user_id, []).append({
        "role": role,
        "name": name,
        "text": text,
        "ts": time.time(),
    })
    if len(support_history[user_id]) > HISTORY_LIMIT:
        support_history[user_id] = support_history[user_id][-HISTORY_LIMIT:]

def _ticket_action_markup(user_id: int):
    """Инлайн-кнопки для уведомления о тикете."""
    builder = InlineKeyboardBuilder()
    builder.button(text="⚡ Ответить", callback_data=f"support_reply:{user_id}")
    builder.button(text="📜 История", callback_data=f"support_history:{user_id}")
    if support_claims.get(user_id):
        builder.button(text="🔓 Отпустить", callback_data=f"support_release:{user_id}")
    else:
        builder.button(text="🔒 Взять в работу", callback_data=f"support_claim:{user_id}")
    builder.button(text="✅ Закрыть тикет", callback_data=f"support_close:{user_id}")
    builder.button(text="◀️ К поддержке", callback_data="admin_support_menu")
    builder.adjust(2, 2, 1)
    return builder.as_markup()


def _support_exit_markup():
    builder = InlineKeyboardBuilder()
    builder.button(text="◀️ Выйти из поддержки", callback_data="support_exit")
    return builder.as_markup()


def is_admin(user_id: int) -> bool:
    return has_permission_sync(user_id, "support.respond")


async def set_support_mode(user_id: int, active: bool) -> None:
    """Включить/выключить режим диалога с поддержкой.

    Пишет и в in-memory кэш (быстрая проверка на каждом сообщении),
    и в БД (переживает рестарт бота).
    """
    if active:
        settings.support_pending_users.add(user_id)
    else:
        settings.support_pending_users.discard(user_id)
        # Пользователь вышел из диалога — отпускаем тикет и сбрасываем
        # трекинг эскалации, чтобы поллер не долбил по вышедшему юзеру.
        support_claims.pop(user_id, None)
        _clear_support_tracking(user_id)
    try:
        await db.set_support_active(user_id, active)
    except Exception as e:
        logger.warning(f"Не удалось обновить is_support_active для {user_id}: {e}")


async def _is_in_support(user_id: int) -> bool:
    """Проверка, находится ли пользователь в режиме диалога с поддержкой.

    Сверяется и с in-memory кэшем, и с БД — на случай перезапуска процесса,
    когда кэш пуст, а флаг в БД ещё активен.
    """
    in_support = user_id in settings.support_pending_users
    if not in_support:
        try:
            if await db.is_support_active(user_id):
                settings.support_pending_users.add(user_id)
                in_support = True
        except Exception as e:
            logger.warning(f"is_support_active({user_id}) упал: {e}")
    return in_support




@router.message(CommandStart(deep_link=True))
async def cmd_start_with_ref(message: Message, state: FSMContext):
    """Обработка /start с реферальным кодом"""
    # Сохраняем пользователя в БД
    await db.add_user(message.from_user.id, message.from_user.username or message.from_user.first_name)
    # /start означает начало новой сессии — выходим из активного диалога с поддержкой
    await set_support_mode(message.from_user.id, False)

    ref_code = message.text.split()[1] if len(message.text.split()) > 1 else ""
    await db.capture_campaign_first_touch(message.from_user.id, ref_code)
    referrer_id = await db.parse_referral_code(ref_code)

    if referrer_id and referrer_id != message.from_user.id:
        already_referred = await db.check_referral_exists(message.from_user.id)

        if not already_referred:
            await state.update_data(referrer_id=referrer_id)
            await state.set_state(ReferralState.waiting_for_confirmation)

            builder = InlineKeyboardBuilder()
            builder.button(text="✅ Да, получить скидку 10%", callback_data="ref_accept")
            builder.button(text="❌ Нет, спасибо", callback_data="ref_decline")
            builder.adjust(1)

            await message.answer(
                f"🎁 <b>Ваш друг приглашает вас в «Семена Знаний»!</b>\n\n"
                f"Примите подарок — <b>скидку 10%</b> на первый заказ?\n\n"
                f"А ваш друг получит <b>скидку 15%</b> на следующий заказ 🎉",
                reply_markup=builder.as_markup(),
                parse_mode="HTML"
            )
            return

    await _send_start_menu(message)


@router.message(CommandStart())
async def cmd_start(message: Message):
    """Обычный старт без параметров"""
    # Сохраняем пользователя в БД
    await db.add_user(message.from_user.id, message.from_user.username or message.from_user.first_name)
    # /start означает начало новой сессии — выходим из активного диалога с поддержкой
    await set_support_mode(message.from_user.id, False)
    await _send_start_menu(message)


async def _send_start_menu(message: Message):
    """Отправка стартового меню"""
    builder = InlineKeyboardBuilder()
    builder.button(text="🌱 Открыть магазин", web_app=WebAppInfo(url=settings.WEBAPP_URL))
    builder.button(text="📜 Мои заказы", callback_data="my_orders")
    builder.button(text="👥 Пригласить друга", callback_data="invite_friend")
    builder.button(text="🆘 Поддержка", callback_data="support")
    builder.button(text="ℹ️ О магазине", callback_data="about")
    builder.adjust(1)
    await message.answer(
        "🌿 Добро пожаловать в <b>Семена Знаний</b>!\n\n"
        "Книжный магазин, где цена указана за одну страницу.\n"
        "Нажмите кнопку ниже, чтобы открыть каталог 👇",
        reply_markup=builder.as_markup(), parse_mode="HTML"
    )


@router.message(Command("orders"))
async def cmd_orders(message: Message):
    await show_user_orders(message, message.from_user.id)


@router.message(F.web_app_data)
async def handle_webapp_data(message: Message, bot: Bot):
    try:
        data = json.loads(message.web_app_data.data)
        if data.get('action') == 'checkout':
            await process_checkout(message, data, bot)
    except Exception as e:
        logger.warning(f"Ошибка web_app_data: {e}")
        await message.answer("❌ Ошибка при обработке данных")


@router.callback_query(F.data == "my_orders")
async def my_orders_callback(callback: CallbackQuery):
    # Навигация по каталогу выводит из режима диалога с поддержкой
    await set_support_mode(callback.from_user.id, False)
    await show_user_orders(callback, callback.from_user.id)


@router.callback_query(F.data == "about")
async def about_callback(callback: CallbackQuery):
    # Навигация по разделам выводит из режима диалога с поддержкой
    await set_support_mode(callback.from_user.id, False)
    await callback.message.answer(
        "📚 <b>Семена Знаний</b> — это:\n"
        "• Ботанические атласы и травники\n"
        "• Книги о садоводстве и флористике\n"
        "• Альбомы с акварельной иллюстрацией\n"
        "• Художественная литература о природе\n\n"
        "📍 Ждём вас в Mini App!",
        parse_mode="HTML"
    )
    await callback.answer()


def _support_intro_text(order_id: int | None = None) -> str:
    order_note = (
        f"Ваше обращение по заказу #{order_id} передано администратору.\n\n"
        if order_id is not None
        else ""
    )
    return (
        "🆘 <b>Служба поддержки</b>\n\n"
        f"{order_note}"
        "Напишите ваш вопрос, и администратор ответит!\n\n"
        "💬 Вы можете отправлять несколько сообщений подряд — все они уйдут "
        "в поддержку, отвечать на них можно прямо здесь."
    )


@router.callback_query(F.data == "support")
async def support_callback(callback: CallbackQuery, state: FSMContext):
    """Обработка кнопки поддержки"""
    # Сбрасываем любое накопившееся FSM-состояние, чтобы следующее текстовое
    # сообщение гарантированно ушло в поддержку, а не было проглочено
    # обработчиком какого-нибудь waiting_for_* из админки.
    await state.clear()
    await set_support_mode(callback.from_user.id, True)
    await callback.message.answer(
        _support_intro_text(),
        reply_markup=_support_exit_markup(),
        parse_mode="HTML"
    )
    await callback.answer()


async def order_support_notify_loop(bot: Bot):
    """Deliver durable Mini App order-support requests into the normal ticket flow."""
    from db.order_support_requests import (
        claim_order_support_requests,
        fail_order_support_request,
        mark_order_support_request_sent,
        release_order_support_request,
    )

    logger.info("Order support request worker started")
    while True:
        try:
            requests = await claim_order_support_requests()
            for support_request in requests:
                request_id = support_request["id"]
                try:
                    receipt = json.loads(support_request["receipt_json"])
                    if not isinstance(receipt, dict) or not isinstance(receipt.get("items"), list):
                        raise ValueError("invalid receipt")
                    user_id = support_request["user_id"]
                    item_lines = []
                    for item in receipt["items"]:
                        if not isinstance(item, dict):
                            raise ValueError("invalid receipt item")
                        title = html.escape(str(item["title"]), quote=False)
                        quantity = int(item.get("quantity", 1))
                        line_total = int(item["line_total"])
                        if quantity < 1:
                            raise ValueError("invalid receipt quantity")
                        quantity_suffix = f" ×{quantity}" if quantity > 1 else ""
                        item_lines.append(f"• {title}{quantity_suffix} — {line_total} ₽")
                    item_text = "\n".join(item_lines)
                    receipt_text = (
                        f"🆘 <b>Обращение по заказу #{receipt['id']}</b>\n\n"
                        f"Статус: {html.escape(str(receipt['status']), quote=False)}\n"
                        f"Оплата: {html.escape(str(receipt['payment_method']), quote=False)}\n\n"
                        f"📦 <b>Состав:</b>\n{item_text}\n\n"
                        f"Товары: {int(receipt['items_subtotal'])} ₽\n"
                    )
                    if receipt.get("delivery_price"):
                        receipt_text += f"Доставка: {int(receipt['delivery_price'])} ₽\n"
                    if receipt.get("promo_code_snapshot"):
                        receipt_text += (
                            f"Промокод: <code>{html.escape(str(receipt['promo_code_snapshot']))}</code>\n"
                        )
                    if receipt.get("promo_discount"):
                        receipt_text += f"Скидка промокода: −{int(receipt['promo_discount'])} ₽\n"
                    if receipt.get("bonus_discount"):
                        receipt_text += f"Скидка бонуса: −{int(receipt['bonus_discount'])} ₽\n"
                    if receipt.get("total_discount"):
                        receipt_text += f"Общая скидка: −{int(receipt['total_discount'])} ₽\n"
                    receipt_text += f"Итого: <b>{int(receipt['total'])} ₽</b>"
                    claimed_by = support_claims.get(user_id)
                    support_recipients = recipient_ids_for_event_sync("support")
                    recipients = [claimed_by] if claimed_by in support_recipients else support_recipients
                    delivered = False
                    for admin_id in recipients:
                        try:
                            sent = await bot.send_message(
                                admin_id,
                                receipt_text,
                                reply_markup=_ticket_action_markup(user_id),
                                parse_mode="HTML",
                            )
                            _track_admin_msg(user_id, admin_id, sent.message_id, support_forward_msgs)
                            delivered = True
                        except Exception:
                            logger.warning("Order support ticket delivery failed")
                    if not delivered:
                        await release_order_support_request(request_id, "delivery_failed")
                        continue
                    await set_support_mode(user_id, True)
                    try:
                        await bot.send_message(
                            user_id,
                            _support_intro_text(receipt["id"]),
                            reply_markup=_support_exit_markup(),
                            parse_mode="HTML",
                        )
                    except TelegramForbiddenError:
                        logger.warning("Order support customer notice was rejected")
                    except Exception:
                        logger.warning("Order support customer notice failed")
                    _track_support_message(
                        user_id,
                        name="Заказ",
                        text=f"Обращение по заказу #{receipt['id']}",
                    )
                    await mark_order_support_request_sent(request_id)
                except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                    await fail_order_support_request(request_id, "invalid_receipt")
                except Exception:
                    logger.exception("Order support request delivery failed")
                    await release_order_support_request(request_id, "delivery_error")
        except Exception:
            logger.exception("Order support request worker failed")
        await asyncio.sleep(15)


@router.callback_query(F.data == "support_exit")
async def support_exit_callback(callback: CallbackQuery, state: FSMContext):
    was_active = await _is_in_support(callback.from_user.id)
    await state.clear()
    if was_active:
        await set_support_mode(callback.from_user.id, False)
    await callback.answer()
    try:
        await callback.message.delete()
    except TelegramBadRequest:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass
    if was_active:
        await _send_start_menu(callback.message)


@router.callback_query(F.data == "main_menu")
async def back_to_menu(callback: CallbackQuery):
    # Возврат в главное меню выводит из режима диалога с поддержкой
    await set_support_mode(callback.from_user.id, False)
    builder = InlineKeyboardBuilder()
    builder.button(text="🌱 Открыть магазин", web_app=WebAppInfo(url=settings.WEBAPP_URL))
    builder.button(text="📜 Мои заказы", callback_data="my_orders")
    builder.button(text="🆘 Поддержка", callback_data="support")
    builder.button(text="ℹ️ О магазине", callback_data="about")
    builder.adjust(1)
    await callback.message.edit_text(
        "🌿 <b>Семена Знаний</b>\n\nНажмите кнопку ниже, чтобы открыть каталог 👇",
        reply_markup=builder.as_markup(), parse_mode="HTML"
    )
    await callback.answer()


@router.message(F.text & ~F.text.startswith("/"))
async def universal_text_handler(message: Message, state: FSMContext, bot: Bot):
    """Единый обработчик всех текстовых сообщений с проверкой FSM"""
    user_id = message.from_user.id
    current_state = await state.get_state()

    # === ПОДДЕРЖКА (проверяем ПЕРВОЙ, чтобы любое FSM-состояние не
    # перехватило сообщение раньше, чем мы перешлём его админу) ===
    # Сверяемся и с кэшем, и с БД — на случай если процесс перезапускался
    # между сообщениями и кэш пуст, а в БД флаг ещё активен.
    in_support = user_id in settings.support_pending_users
    if not in_support:
        try:
            if await db.is_support_active(user_id):
                settings.support_pending_users.add(user_id)
                in_support = True
        except Exception as e:
            logger.warning(f"is_support_active({user_id}) упал: {e}")

    if in_support:
        # Рейт-лимит на сообщения пользователя: спам не уходит админам,
        # не пишется в историю и не триггерит эскалацию.
        err = _rate_limited(
            user_msg_last_ts, user_msg_log, user_id, user_id,
        )
        if err:
            await message.answer(err)
            return
        # Сохраняем историю до рассылки, чтобы доставленный в поддержку текст
        # не потерялся при перезапуске процесса.
        try:
            await _append_history(
                user_id,
                role="user",
                name=message.from_user.full_name or str(user_id),
                text=message.text,
            )
        except Exception:
            logger.exception("Не удалось сохранить сообщение поддержки для user_id=%s", user_id)
            await message.answer("❌ Не удалось передать сообщение в поддержку. Попробуйте ещё раз.")
            return
        # Запоминаем сообщение для эскалации: если админ не ответит в течение
        # ESCALATION_TIMEOUT — поллер продублирует текст всем админам.
        _track_support_message(
            user_id,
            name=message.from_user.full_name or str(user_id),
            text=message.text,
        )
        claimed_by = support_claims.get(user_id)
        # Если тикет закреплён за конкретным админом — шлём только ему,
        # чтобы второй админ не отвечал параллельно.
        support_recipients = recipient_ids_for_event_sync("support")
        recipients = (
            [claimed_by] if claimed_by in support_recipients
            else support_recipients
        )
        claim_note = (
            "\n🔒 <i>Тикет закреплён за вами.</i>"
            if claimed_by
            else "\nТикет свободен — возьмите его в работу кнопкой ниже."
        )
        safe_name = html.escape(message.from_user.full_name or str(user_id), quote=False)
        safe_text = html.escape(message.text, quote=False)
        header = (
            f"🆘 <b>Сообщение от пользователя</b>\n\n"
            f"👤 {safe_name} (ID: {message.from_user.id})\n"
            f"💬 Текст: {safe_text}\n\n"
            f"Как ответить:\n"
            f"• просто нажмите «Ответить» на это сообщение\n"
            f"• или используйте кнопки ниже"
            f"{claim_note}"
        )
        for admin_id in recipients:
            try:
                sent = await bot.send_message(
                    admin_id,
                    header,
                    reply_markup=_ticket_action_markup(user_id),
                    parse_mode="HTML",
                )
                _track_admin_msg(user_id, admin_id, sent.message_id,
                                 support_forward_msgs)
            except Exception as e:
                logger.warning(f"Не удалось отправить сообщение админу {admin_id}: {e}")
        # Не убираем пользователя из support_pending_users — пусть продолжает
        # диалог, не нажимая каждый раз кнопку «Поддержка».
        await message.answer(
            "✅ <b>Сообщение отправлено в поддержку!</b>\n\n"
            "Можете продолжать писать здесь — администратор ответит в этом же чате.",
            reply_markup=_support_exit_markup(),
            parse_mode="HTML",
        )
        return

    admin_only_states = {
        AddBookState.waiting_for_title.state,
        AddBookState.waiting_for_author.state,
        AddBookState.waiting_for_description.state,
        AddBookState.waiting_for_price.state,
        AddBookState.waiting_for_category.state,
        AddBookState.waiting_for_emoji.state,
        AddBookState.waiting_for_inner_images.state,
        AddBookState.waiting_for_cover_photo.state,
        AddBookState.waiting_for_page_photos.state,
        AddBookState.confirming.state,
        EditBookState.waiting_for_field.state,
        EditBookState.waiting_for_value.state,
        EditBookState.waiting_for_description.state,
        EditBookState.waiting_for_image_url.state,
        EditBookState.waiting_for_image_photo.state,
        EditBookState.waiting_for_new_category.state,
        EditBookState.waiting_for_new_title.state,
        EditBookState.waiting_for_new_author.state,
        EditBookState.waiting_for_new_price.state,
        EditBookState.waiting_for_new_description.state,
        EditBookState.waiting_for_new_cover.state,
        EditBookState.waiting_for_new_page.state,
        EditBookState.waiting_for_new_category_admin.state,
        CategoryState.waiting_for_name.state,
        CategoryState.waiting_for_emoji.state,
        CategoryState.editing_name.state,
        CategoryState.editing_emoji.state,
        PromoCodeState.waiting_for_code.state,
        PromoCodeState.waiting_for_discount.state,
        PromoCodeState.waiting_for_min_order.state,
        PromoCodeState.waiting_for_max_uses.state,
        PromoCodeState.waiting_for_expires.state,
        PromoCodeState.editing_discount.state,
        PromoCodeState.editing_min_order.state,
        PromoCodeState.editing_max_uses.state,
        PromoCodeState.editing_expires.state,
        DeliverySettingsState.waiting_for_price.state,
        DeliverySettingsState.waiting_for_self_pickup_location.state,
        DeliverySettingsState.waiting_for_self_pickup_schedule.state,
        DeliverySettingsState.waiting_for_self_pickup_instructions.state,
    }
    if current_state in admin_only_states and not is_admin(user_id):
        await state.clear()
        await message.answer("❌ Нет прав администратора.")
        return

    # 🔧 ВАЖНО: пропускаем сообщения, если пользователь в состоянии настройки оплаты
    payment_states = [
        PaymentSettingsState.waiting_for_card.state,
        PaymentSettingsState.waiting_for_sbp_phone.state,
        PaymentSettingsState.waiting_for_sbp_bank.state,
        PaymentSettingsState.waiting_for_recipient.state,
        PaymentSettingsState.waiting_for_instructions.state,
        PaymentSettingsState.waiting_for_stars_rate.state,
        DeliverySettingsState.waiting_for_price.state,
        DeliverySettingsState.waiting_for_self_pickup_location.state,
        DeliverySettingsState.waiting_for_self_pickup_schedule.state,
        DeliverySettingsState.waiting_for_self_pickup_instructions.state,
    ]

    if current_state in payment_states:
        return  # Пропускаем — пусть обрабатывает payments.py

    # 🔧 ВАЖНО: пропускаем сообщения, если пользователь в состоянии редактирования книги
    edit_states = [
        EditBookState.waiting_for_new_title.state,
        EditBookState.waiting_for_new_author.state,
        EditBookState.waiting_for_new_description.state,
        EditBookState.waiting_for_new_price.state,
    ]

    if current_state in edit_states:
        return  # Пропускаем — пусть обрабатывает admin_books.py

    # === FSM: ДОБАВЛЕНИЕ КНИГ ===
    if current_state == AddBookState.waiting_for_title.state:
        await state.update_data(title=message.text)
        await message.answer("💰 Теперь отправьте <b>цену</b> (только цифры):", parse_mode="HTML")
        await state.set_state(AddBookState.waiting_for_price)
        return

    if current_state == AddBookState.waiting_for_price.state:
        try:
            price = int(message.text)
            if price <= 0:
                raise ValueError
            await state.update_data(price=price)

            categories = await db.get_all_categories()
            if categories:
                builder = InlineKeyboardBuilder()
                for cat in categories:
                    builder.button(
                        text=f"{cat['emoji'] or ''} {cat['name']}".strip(),
                        callback_data=f"book_cat_{cat['id']}"
                    )
                builder.button(text="✏️ Ввести свою", callback_data="book_cat_custom")
                builder.adjust(2)

                await message.answer(
                    "📂 <b>Выберите категорию</b> из списка:\n\n"
                    "Или нажмите 'Ввести свою' для ручной настройки",
                    reply_markup=builder.as_markup(),
                    parse_mode="HTML"
                )
            else:
                await message.answer("📂 Отправьте <b>категорию</b>:", parse_mode="HTML")
                await state.set_state(AddBookState.waiting_for_category)
        except ValueError:
            await message.answer("❌ Некорректная цена. Отправьте целое число больше 0.")
        return

    if current_state == AddBookState.waiting_for_category.state:
        await state.update_data(category=message.text)
        await message.answer("🎨 Отправьте эмодзи или URL обложки (или 'нет'):")
        await state.set_state(AddBookState.waiting_for_emoji)
        return

    if current_state == AddBookState.waiting_for_emoji.state:
        emoji = message.text.strip()
        if emoji.lower() == 'нет':
            emoji = ''
        await state.update_data(emoji=emoji)

        await state.set_state(AddBookState.waiting_for_description)

        builder = InlineKeyboardBuilder()
        builder.button(text="⏭️ Пропустить", callback_data="skip_description")

        await message.answer(
            f"🎨 Обложка: {emoji or '📚'}\n\n"
            f"Теперь отправьте <b>описание книги</b>:\n\n"
            f"Можно несколько абзацев. Это поможет покупателям понять, о чём книга.\n\n"
            f"Или нажмите кнопку ниже, чтобы пропустить",
            reply_markup=builder.as_markup(),
            parse_mode="HTML"
        )
        return

    if current_state == AddBookState.waiting_for_description.state:
        description = message.text.strip()
        data = await state.get_data()

        book_id = await db.add_book(
            data['title'],
            data['price'],
            data['category'],
            data.get('emoji', ''),
            description,
            '[]',
            data.get('category_id')
        )

        # Сохраняем ID книги в состоянии для добавления внутренних изображений
        await state.update_data(book_id=book_id)
        
        await message.answer(
            f"✅ <b>Книга добавлена!</b>\n\n"
            f"🆔 ID: {book_id}\n"
            f"📖 {data['title']}\n"
            f"💰 {data['price']} ₽\n"
            f"📂 {data['category']}\n"
            f"🎨 {data.get('emoji', '') or '📚'}\n"
            f"📝 {description[:100]}{'...' if len(description) > 100 else ''}\n\n"
            f"🎉 Теперь она доступна в Mini App!\n\n"
            f"📸 Хотите добавить <b>внутренние фото</b> (страницы, фрагменты)?\n"
            f"Отправьте фото сейчас или нажмите 'Пропустить'",
            parse_mode="HTML"
        )
        await state.set_state(AddBookState.waiting_for_inner_images)
        return
    
    if current_state == AddBookState.waiting_for_inner_images.state:
        # Обработка фото или пропуска
        if message.text and message.text.lower() in ['пропустить', 'skip', 'нет']:
            await message.answer("✅ Внутренние фото не добавлены. Книга готова!")
            await state.clear()
            return
        
        if message.photo:
            # Получаем фото наилучшего качества
            photo = message.photo[-1]
            file_id = photo.file_id
            
            # Получаем URL файла
            file = await bot.get_file(file_id)
            file_url = f"https://api.telegram.org/file/bot{bot.token}/{file.file_path}"
            
            # Получаем текущие данные
            data = await state.get_data()
            book_id = data.get('book_id')
            
            if book_id:
                # Получаем текущие изображения
                book = await db.get_book(book_id)
                import json
                current_images = json.loads(book.get('images', '[]')) if book.get('images') else []
                
                # Добавляем новое изображение
                current_images.append(file_url)
                
                # Обновляем книгу
                await db.update_book(book_id, images=json.dumps(current_images))
                
                await message.answer(
                    f"✅ Фото добавлено!\n\n"
                    f"Отправьте ещё фото или нажмите 'Пропустить' для завершения",
                    parse_mode="HTML"
                )
                return
        
        await message.answer(
            "📸 Отправьте <b>фото</b> страницы книги или нажмите 'Пропустить'\n\n"
            "Это поможет покупателям лучше рассмотреть товар.",
            parse_mode="HTML"
        )
        return

    # === FSM: РЕДАКТИРОВАНИЕ КНИГ ===
    if current_state == EditBookState.waiting_for_value.state:
        data = await state.get_data()
        book_id = data.get('edit_book_id')
        field = data.get('edit_field')

        if not book_id or not field:
            await message.answer("❌ Ошибка")
            await state.clear()
            return

        new_value = message.text.strip()

        if field == 'price':
            try:
                new_value = int(new_value)
                if new_value <= 0:
                    raise ValueError
            except ValueError:
                await message.answer("❌ Некорректная цена.")
                return

        # 🔧 ИСПРАВЛЕНИЕ 1: При смене обложки (emoji) мы НЕ трогаем поле images!
        if field == 'emoji':
            await db.update_book_full(book_id, emoji=new_value)
        else:
            await db.update_book_full(book_id, **{field: new_value})

        book = await db.get_book(book_id)
        emoji_display = "[Картинка]" if book['emoji'].startswith('http') else book['emoji']

        builder = InlineKeyboardBuilder()
        builder.button(text="📚 В меню каталога", callback_data="admin_catalog")

        await message.answer(
            f"✅ <b>Книга обновлена!</b>\n\n"
            f"📖 {book['title']}\n"
            f"💰 {book['price']} ₽\n"
            f"📂 {book['category']}\n"
            f"🎨 {emoji_display}\n\n"
            f"✨ Изменения сразу появятся в Mini App!",
            reply_markup=builder.as_markup(),
            parse_mode="HTML"
        )
        await state.clear()
        return

    if current_state == EditBookState.waiting_for_description.state:
        data = await state.get_data()
        book_id = data.get('edit_book_id')

        if not book_id:
            await message.answer("❌ Ошибка")
            await state.clear()
            return

        new_description = message.text.strip()
        await db.update_book_full(book_id, description=new_description)

        builder = InlineKeyboardBuilder()
        builder.button(text="📚 В меню каталога", callback_data="admin_catalog")

        await message.answer(
            f"✅ <b>Описание обновлено!</b>\n\n"
            f"📖 {new_description[:100]}{'...' if len(new_description) > 100 else ''}\n\n"
            f"✨ Изменения сразу появятся в Mini App!",
            reply_markup=builder.as_markup(),
            parse_mode="HTML"
        )
        await state.clear()
        return

    if current_state == EditBookState.waiting_for_image_url.state:
        data = await state.get_data()
        book_id = data.get('edit_book_id')

        if not book_id:
            await message.answer("❌ Ошибка")
            await state.clear()
            return

        url = message.text.strip()
        book = await db.get_book(book_id)
        current_images = parseBookImages(book.get('images') or '[]')
        current_images.append(url)

        await db.update_book_full(book_id, images=json.dumps(current_images))

        await message.answer(
            f"✅ <b>Изображение добавлено в галерею!</b>\n\n"
            f"Всего фото: {len(current_images)}\n\n"
            f"Отправьте ещё URL или нажмите /cancel",
            parse_mode="HTML",
        )
        return

    # === FSM: КАТЕГОРИИ ===
    if current_state == CategoryState.waiting_for_name.state:
        name = message.text.strip()
        existing = await db.get_all_categories()
        if any(c['name'].lower() == name.lower() for c in existing):
            await message.answer("❌ Категория с таким названием уже существует. Введите другое название:")
            return

        await state.update_data(category_name=name)
        await state.set_state(CategoryState.waiting_for_emoji)

        builder = InlineKeyboardBuilder()
        builder.button(text="◀️ Назад", callback_data="admin_categories")

        await message.answer(
            f"📝 Название: <b>{name}</b>\n\n"
            f"Теперь отправьте <b>эмодзи</b> для категории (например: 📚, 🎨):\n\n"
            "Или нажмите кнопку ниже для отмены",
            reply_markup=builder.as_markup(),
            parse_mode="HTML"
        )
        return

    if current_state == CategoryState.waiting_for_emoji.state:
        emoji = message.text.strip()
        data = await state.get_data()
        name = data.get('category_name')

        cat_id = await db.add_category(name, emoji)

        builder = InlineKeyboardBuilder()
        builder.button(text="📂 К категориям", callback_data="admin_categories")

        await message.answer(
            f"✅ <b>Категория добавлена!</b>\n\n"
            f"🆔 ID: {cat_id}\n"
            f"📂 Название: {name}\n"
            f"🎨 Эмодзи: {emoji}",
            reply_markup=builder.as_markup(),
            parse_mode="HTML"
        )
        await state.clear()
        return

    if current_state == CategoryState.editing_name.state:
        data = await state.get_data()
        cat_id = data.get('edit_category_id')
        new_name = message.text.strip()

        if not cat_id:
            await message.answer("❌ Ошибка")
            await state.clear()
            return

        await db.update_category(cat_id, name=new_name)

        builder = InlineKeyboardBuilder()
        builder.button(text="📂 К категориям", callback_data="admin_categories")

        await message.answer(
            f"✅ <b>Название обновлено!</b>\n\n"
            f"Новое название: {new_name}",
            reply_markup=builder.as_markup(),
            parse_mode="HTML"
        )
        await state.clear()
        return

    if current_state == CategoryState.editing_emoji.state:
        data = await state.get_data()
        cat_id = data.get('edit_category_id')
        new_emoji = message.text.strip()

        if not cat_id:
            await message.answer("❌ Ошибка")
            await state.clear()
            return

        await db.update_category(cat_id, emoji=new_emoji)

        builder = InlineKeyboardBuilder()
        builder.button(text="📂 К категориям", callback_data="admin_categories")

        await message.answer(
            f"✅ <b>Эмодзи обновлён!</b>\n\n"
            f"Новый эмодзи: {new_emoji}",
            reply_markup=builder.as_markup(),
            parse_mode="HTML"
        )
        await state.clear()
        return

    # === FSM: ПРОМОКОДЫ ===
    if current_state == PromoCodeState.waiting_for_code.state:
        code = message.text.strip().upper()
        existing = await db.get_all_promo_codes()
        if any(p['code'] == code for p in existing):
            await message.answer("❌ Промокод с таким кодом уже существует. Введите другой:")
            return

        await state.update_data(promo_code=code)
        await state.set_state(PromoCodeState.waiting_for_discount)

        builder = InlineKeyboardBuilder()
        builder.button(text="◀️ Назад", callback_data="admin_promo")

        await message.answer(
            f"🎟️ Код: <b>{code}</b>\n\n"
            f"Теперь введите <b>скидку</b>:\n"
            f"Формат: <b>10%</b> (процент) или <b>500</b> (фиксированная сумма в ₽)\n\n"
            f"Или нажмите кнопку ниже для отмены",
            reply_markup=builder.as_markup(),
            parse_mode="HTML"
        )
        return

    if current_state == PromoCodeState.waiting_for_discount.state:
        discount_text = message.text.strip()
        discount_percent = 0
        discount_fixed = 0

        if discount_text.endswith('%'):
            try:
                discount_percent = int(discount_text[:-1])
                if discount_percent <= 0 or discount_percent > 100:
                    raise ValueError
            except ValueError:
                await message.answer("❌ Неверный процент. Введите число от 1 до 100 с символом % (например: 10%):")
                return
        else:
            try:
                discount_fixed = int(discount_text)
                if discount_fixed <= 0:
                    raise ValueError
            except ValueError:
                await message.answer("❌ Неверная сумма. Введите целое число больше 0 (например: 500) или процент с % (например: 10%):")
                return

        await state.update_data(discount_percent=discount_percent, discount_fixed=discount_fixed)
        await state.set_state(PromoCodeState.waiting_for_min_order)

        builder = InlineKeyboardBuilder()
        builder.button(text="◀️ Назад", callback_data="admin_promo")

        await message.answer(
            f"💰 Скидка: {discount_text}\n\n"
            f"Теперь введите <b>минимальную сумму заказа</b> в ₽ (или 0 для без ограничений):\n\n"
            f"Или нажмите кнопку ниже для отмены",
            reply_markup=builder.as_markup(),
            parse_mode="HTML"
        )
        return

    if current_state == PromoCodeState.waiting_for_min_order.state:
        try:
            min_order = int(message.text.strip())
            if min_order < 0:
                raise ValueError
            await state.update_data(min_order=min_order)
            await state.set_state(PromoCodeState.waiting_for_max_uses)

            builder = InlineKeyboardBuilder()
            builder.button(text="◀️ Назад", callback_data="admin_promo")

            await message.answer(
                f"📊 Мин. сумма: {min_order} ₽\n\n"
                f"Теперь введите <b>максимальное количество использований</b> (или 0 для без лимита):\n\n"
                f"Или нажмите кнопку ниже для отмены",
                reply_markup=builder.as_markup(),
                parse_mode="HTML"
            )
        except ValueError:
            await message.answer("❌ Введите целое число >= 0")
        return

    if current_state == PromoCodeState.waiting_for_max_uses.state:
        try:
            max_uses = int(message.text.strip())
            if max_uses < 0:
                raise ValueError
            await state.update_data(max_uses=max_uses)
            await state.set_state(PromoCodeState.waiting_for_expires)

            builder = InlineKeyboardBuilder()
            builder.button(text="◀️ Назад", callback_data="admin_promo")

            await message.answer(
                f"🔢 Лимит: {max_uses if max_uses > 0 else '∞'}\n\n"
                f"Теперь введите <b>дату окончания</b> в формате ГГГГ-ММ-ДД (например: 2026-12-31):\n\n"
                f"Или отправьте 'бессрочно' для промокода без срока\n\n"
                f"Или нажмите кнопку ниже для отмены",
                reply_markup=builder.as_markup(),
                parse_mode="HTML"
            )
        except ValueError:
            await message.answer("❌ Введите целое число >= 0")
        return

    if current_state == PromoCodeState.waiting_for_expires.state:
        expires_text = message.text.strip()
        expires_at = None

        if expires_text.lower() != 'бессрочно':
            try:
                from datetime import datetime
                expires_at = datetime.strptime(expires_text, '%Y-%m-%d').isoformat()
            except ValueError:
                await message.answer("❌ Неверный формат даты. Используйте ГГГГ-ММ-ДД (например: 2026-12-31) или отправьте 'бессрочно':")
                return

        data = await state.get_data()
        promo_id = await db.add_promo_code(
            data['promo_code'],
            data.get('discount_percent', 0),
            data.get('discount_fixed', 0),
            data.get('min_order', 0),
            data.get('max_uses', 0),
            expires_at
        )

        discount_text = f"{data.get('discount_percent', 0)}%" if data.get('discount_percent', 0) > 0 else f"{data.get('discount_fixed', 0)}₽"

        builder = InlineKeyboardBuilder()
        builder.button(text="🎟️ К промокодам", callback_data="admin_promo")

        await message.answer(
            f"✅ <b>Промокод создан!</b>\n\n"
            f"🎟️ Код: <b>{data['promo_code']}</b>\n"
            f"💰 Скидка: {discount_text}\n"
            f"📊 Мин. сумма: {data.get('min_order', 0)} ₽\n"
            f"🔢 Лимит: {data.get('max_uses', 0) if data.get('max_uses', 0) > 0 else '∞'}\n"
            f"📅 Действует до: {expires_text}\n\n"
            f"ID: {promo_id}",
            reply_markup=builder.as_markup(),
            parse_mode="HTML"
        )
        await state.clear()
        return

    # Обработка редактирования промокода
    if current_state == PromoCodeState.editing_discount.state:
        data = await state.get_data()
        promo_id = data.get('edit_promo_id')

        if not promo_id:
            await message.answer("❌ Ошибка")
            await state.clear()
            return

        discount_text = message.text.strip()
        discount_percent = 0
        discount_fixed = 0

        if discount_text.endswith('%'):
            try:
                discount_percent = int(discount_text[:-1])
                if discount_percent <= 0 or discount_percent > 100:
                    raise ValueError
            except ValueError:
                await message.answer("❌ Неверный процент.")
                return
        else:
            try:
                discount_fixed = int(discount_text)
                if discount_fixed <= 0:
                    raise ValueError
            except ValueError:
                await message.answer("❌ Неверная сумма.")
                return

        await db.update_promo_code(promo_id, discount_percent=discount_percent, discount_fixed=discount_fixed)

        builder = InlineKeyboardBuilder()
        builder.button(text="🎟️ К промокодам", callback_data="admin_promo")

        await message.answer(
            f"✅ <b>Скидка обновлена!</b>\n\n"
            f"Новая скидка: {discount_text}",
            reply_markup=builder.as_markup(),
            parse_mode="HTML"
        )
        await state.clear()
        return

    if current_state == PromoCodeState.editing_min_order.state:
        data = await state.get_data()
        promo_id = data.get('edit_promo_id')

        if not promo_id:
            await message.answer("❌ Ошибка")
            await state.clear()
            return

        try:
            min_order = int(message.text.strip())
            if min_order < 0:
                raise ValueError
        except ValueError:
            await message.answer("❌ Введите целое число >= 0")
            return

        await db.update_promo_code(promo_id, min_order=min_order)

        builder = InlineKeyboardBuilder()
        builder.button(text="🎟️ К промокодам", callback_data="admin_promo")

        await message.answer(
            f"✅ <b>Минимальная сумма обновлена!</b>\n\n"
            f"Новая мин. сумма: {min_order} ₽",
            reply_markup=builder.as_markup(),
            parse_mode="HTML"
        )
        await state.clear()
        return

    if current_state == PromoCodeState.editing_max_uses.state:
        data = await state.get_data()
        promo_id = data.get('edit_promo_id')

        if not promo_id:
            await message.answer("❌ Ошибка")
            await state.clear()
            return

        try:
            max_uses = int(message.text.strip())
            if max_uses < 0:
                raise ValueError
        except ValueError:
            await message.answer("❌ Введите целое число >= 0")
            return

        await db.update_promo_code(promo_id, max_uses=max_uses)

        builder = InlineKeyboardBuilder()
        builder.button(text="🎟️ К промокодам", callback_data="admin_promo")

        await message.answer(
            f"✅ <b>Лимит обновлён!</b>\n\n"
            f"Новый лимит: {max_uses if max_uses > 0 else '∞'}",
            reply_markup=builder.as_markup(),
            parse_mode="HTML"
        )
        await state.clear()
        return

    if current_state == PromoCodeState.editing_expires.state:
        data = await state.get_data()
        promo_id = data.get('edit_promo_id')

        if not promo_id:
            await message.answer("❌ Ошибка")
            await state.clear()
            return

        expires_text = message.text.strip()
        expires_at = None

        if expires_text.lower() != 'бессрочно':
            try:
                from datetime import datetime
                expires_at = datetime.strptime(expires_text, '%Y-%m-%d').isoformat()
            except ValueError:
                await message.answer("❌ Неверный формат даты.")
                return

        await db.update_promo_code(promo_id, expires_at=expires_at)

        builder = InlineKeyboardBuilder()
        builder.button(text="🎟️ К промокодам", callback_data="admin_promo")

        await message.answer(
            f"✅ <b>Срок действия обновлён!</b>\n\n"
            f"Новый срок: {expires_text}",
            reply_markup=builder.as_markup(),
            parse_mode="HTML"
        )
        await state.clear()
        return

    # === FSM: НОВАЯ КАТЕГОРИЯ ПРИ РЕДАКТИРОВАНИИ ===
    if current_state == EditBookState.waiting_for_new_category.state:
        data = await state.get_data()
        book_id = data.get('edit_book_id')
        new_category_name = message.text.strip()

        if not book_id:
            await message.answer("❌ Ошибка")
            await state.clear()
            return

        categories = await db.get_all_categories()
        existing_cat = next((c for c in categories if c['name'].lower() == new_category_name.lower()), None)

        if existing_cat:
            await db.update_book_full(book_id, category=existing_cat['name'], category_id=existing_cat['id'])
            builder = InlineKeyboardBuilder()
            builder.button(text="📚 В меню каталога", callback_data="admin_catalog")
            builder.button(text="✏️ Продолжить редактирование", callback_data=f"catalog_edit_{book_id}")
            builder.adjust(1)
            await message.answer(
                f"✅ <b>Категория найдена и применена!</b>\n\n"
                f"📂 {existing_cat['emoji'] or ''} {existing_cat['name']}",
                reply_markup=builder.as_markup(),
                parse_mode="HTML"
            )
        else:
            new_cat_id = await db.add_category(new_category_name, "")
            await db.update_book_full(book_id, category=new_category_name, category_id=new_cat_id)
            builder = InlineKeyboardBuilder()
            builder.button(text="📚 В меню каталога", callback_data="admin_catalog")
            builder.button(text="✏️ Продолжить редактирование", callback_data=f"catalog_edit_{book_id}")
            builder.adjust(1)
            await message.answer(
                f"✅ <b>Новая категория создана и применена!</b>\n\n"
                f"📂 {new_category_name}\n\n"
                f"💡 Вы можете добавить эмодзи для неё через '📂 Управление категориями'",
                reply_markup=builder.as_markup(),
                parse_mode="HTML"
            )
        await state.clear()
        return

    # Поддержка обрабатывается в начале функции, чтобы ни одно FSM-состояние
    # не перехватывало сообщение раньше.


@router.message(F.photo)
async def handle_photo(message: Message, state: FSMContext):
    """Обработка загруженных фото"""
    current_state = await state.get_state()

    if current_state == EditBookState.waiting_for_image_photo.state and not is_admin(message.from_user.id):
        await state.clear()
        await message.answer("❌ Нет прав администратора.")
        return

    if current_state == EditBookState.waiting_for_image_photo.state:
        data = await state.get_data()
        book_id = data.get('edit_book_id')

        if not book_id:
            await message.answer("❌ Ошибка")
            await state.clear()
            return

        photo = message.photo[-1]
        file = await message.bot.get_file(photo.file_id)
        file_url = f"https://api.telegram.org/file/bot{message.bot.token}/{file.file_path}"

        book = await db.get_book(book_id)
        current_images = parseBookImages(book.get('images') or '[]')
        current_images.append(file_url)

        # 🔧 ИСПРАВЛЕНИЕ 2: При добавлении фото мы обновляем ТОЛЬКО images, НЕ трогаем emoji (обложку)!
        await db.update_book_full(book_id, images=json.dumps(current_images))

        await message.answer(
            f"✅ <b>Фото добавлено в галерею!</b>\n\n"
            f"Всего фото: {len(current_images)}\n\n"
            f"Отправьте ещё фото или нажмите /cancel",
            parse_mode="HTML",
        )
    else:
        await message.answer("❌ Сейчас не ожидаю фото. Используйте /cancel для отмены.")


async def _send_admin_reply(
    message: Message,
    user_id: int,
    body: str,
    *,
    admin_id: int | None = None,
    admin_name: str | None = None,
):
    """Единая отправка ответа пользователю поддержки.

    Используется из двух точек входа:
      - нативный reply на уведомление/ответ в чате админа;
      - кнопка «⚡ Ответить» (через FSM SupportReplyState).

    Возвращает кортеж (ok: bool, status_text: str) — текст для показа админу.
    Рейт-лимит применяется в любом случае, чтобы нельзя было обойти его
    разными точками входа.
    """
    admin_id = admin_id if admin_id is not None else message.from_user.id
    admin_name = admin_name or message.from_user.full_name or str(admin_id)
    if not is_admin(admin_id):
        return False, "❌ Нет прав администратора."
    claimed_by = support_claims.get(user_id)
    if claimed_by is None and not await _is_in_support(user_id):
        return False, "❌ Тикет уже закрыт."
    if claimed_by is not None and claimed_by != admin_id:
        return False, (
            f"🚫 Тикет пользователя <code>{user_id}</code> ведёт другой админ "
            f"(ID <code>{claimed_by}</code>)."
        )

    # Рейт-лимит: пауза между ответами одному пользователю + лимит админа.
    err = _rate_limited(
        reply_last_user_ts, reply_admin_log,
        (admin_id, user_id), admin_id,
    )
    if err:
        return False, err

    is_template = body.startswith("+")
    if is_template:
        template_name = body[1:].split()[0] if body[1:].strip() else ""
        template_key = QUICK_TEMPLATE_KEYS.get(template_name)
        if template_key is None:
            names = ", ".join(f"<code>+{name}</code>" for name in QUICK_TEMPLATE_KEYS)
            return False, f"❌ Шаблон <code>+{template_name}</code> не найден.\n\nДоступные шаблоны: {names}"
        reply_text = await db.get_message_template(template_key)
        follow_up = await db.get_message_template("support.reply.follow_up")
        user_message = f"{reply_text}\n\n{follow_up}"
    else:
        if not body:
            body = "Без текста"
        header = await db.get_message_template("support.reply.header")
        follow_up = await db.get_message_template("support.reply.follow_up")
        user_message = f"{header}\n\n{html.escape(body, quote=False)}\n\n{follow_up}"

    # В историю пишем «чистый» текст ответа (без обёртки «Ответ от
    # поддержки…» и трейлера «Можете ответить прямо здесь…»), чтобы
    # админу было удобно читать историю.
    history_text = reply_text if is_template else (body or "Без текста")
    try:
        await _append_history(
            user_id,
            role="admin",
            name=admin_name,
            text=history_text,
        )
    except Exception:
        logger.exception("Не удалось сохранить ответ поддержки для user_id=%s", user_id)
        return False, "❌ Не удалось сохранить ответ. Попробуйте ещё раз."
    try:
        await message.bot.send_message(user_id, user_message, parse_mode="HTML")
    except TelegramForbiddenError:
        # Пользователь заблокировал бота — переключаем его из режима диалога,
        # чтобы дальнейшие попытки не сыпались в пустоту.
        await set_support_mode(user_id, False)
        logger.warning(f"Не удалось отправить ответ пользователю {user_id}: бот заблокирован")
        return False, (
            f"🚫 Пользователь ID <code>{user_id}</code> заблокировал бота — "
            f"доставить ответ нельзя. Диалог с поддержкой для него закрыт."
        )
    except Exception as e:
        return False, f"❌ Ошибка: {e}"

    # Уведомления админам (пересылки и эскалации) переписываем в
    # «на вопрос уже ответили», чтобы остальные не дублировали ответ.
    await _rewrite_ticket_notices(
        user_id,
        message.bot,
        f"✅ <b>На это сообщение уже ответили</b>\n\n"
        f"👤 Пользователь ID <code>{user_id}</code> получил ответ "
        f"от {html.escape(admin_name, quote=False)} "
        f"(ID: <code>{admin_id}</code>).",
    )
    _clear_support_tracking(user_id)
    # На случай, если пользователь был сброшен из support_pending_users
    # (например, после перезапуска бота) — вернём его в режим диалога,
    # чтобы ответ ушёл в поддержку без повторного нажатия кнопки.
    await set_support_mode(user_id, True)

    # Запоминаем сообщение-ответ админа в обратной карте: если админ потом
    # нажмёт «Ответить» на своё же сообщение (цепочка), мы поймём, кому оно.
    _remember_admin_chain_message(message, user_id)

    claim_hint = (
        "\n\n💡 <i>Чтобы коллеги не отвечали параллельно — возьмите тикет в работу кнопкой.</i>"
        if support_claims.get(user_id) != admin_id
        else "\n\n🔒 <i>Тикет уже закреплён за вами.</i>"
    )
    used_note = (
        f" (шаблон <code>+{template_name}</code>)" if is_template else ""
    )
    return True, f"✅ Ответ отправлен пользователю ID {user_id}!{used_note}{claim_hint}"


def _remember_admin_chain_message(message: Message, user_id: int) -> None:
    """Записать сообщение админа в обратную карту для «ответа на ответ».

    Админ может нажать Telegram-«Ответить» на уведомление бота или на
    предыдущее сообщение в цепочке. Чтобы во всех случаях найти тикет,
    запоминаем и ID сообщения-уведомления (это делает _track_admin_msg),
    и ID «чистого» сообщения админа.
    """
    if message.from_user:
        support_msg_owner[(message.chat.id, message.message_id)] = user_id
        # Сообщение, на которое админ ответил (если это было уведомление бота
        # или ответ коллеги) — тоже перепривязываем на текущего юзера.
        r = message.reply_to_message
        if r:
            support_msg_owner[(r.chat.id, r.message_id)] = user_id


async def _claim_ticket(user_id: int, admin_id: int, admin_name: str, bot: Bot) -> str:
    """Закрепить тикет за админом через кнопку.

    Возвращает текст-статус для админа.
    """
    in_support = user_id in settings.support_pending_users
    if not in_support:
        try:
            if await db.is_support_active(user_id):
                settings.support_pending_users.add(user_id)
                in_support = True
        except Exception as e:
            logger.warning(f"is_support_active({user_id}) упал: {e}")

    if not in_support:
        return (
            f"ℹ️ Пользователь ID <code>{user_id}</code> сейчас не в диалоге с поддержкой — "
            f"закреплять нечего."
        )

    claimed_by = support_claims.get(user_id)
    if claimed_by == admin_id:
        return f"✅ Тикет пользователя <code>{user_id}</code> уже закреплён за вами."
    if claimed_by is not None:
        return (
            f"🚫 Тикет пользователя <code>{user_id}</code> уже ведёт другой админ "
            f"(ID <code>{claimed_by}</code>)."
        )

    support_claims[user_id] = admin_id
    # Все разосланные по этому тикету уведомления (пересылки и дубли
    # эскалации) переписываем в «тикет уже в работе» — остальные админы
    # видят, что вопрос ведёт конкретный человек, и не отвечают параллельно.
    safe_admin_name = html.escape(admin_name, quote=False)
    await _rewrite_ticket_notices(
        user_id,
        bot,
        f"🔒 <b>Тикет уже в работе</b>\n\n"
        f"👤 Пользователь ID <code>{user_id}</code>\n"
        f"👮 Взял в работу: {safe_admin_name} "
        f"(ID: <code>{admin_id}</code>)\n\n"
        f"Отвечает он, параллельные ответы коллег не нужны.",
    )
    return (
        f"🔒 Тикет пользователя <code>{user_id}</code> закреплён за вами. "
        f"Следующие сообщения от него придут только вам.\n\n"
        f"💡 Для быстрого ответа введите <code>+greeting</code>, <code>+wait</code>, "
        f"<code>+resolved</code> или другой шаблон из раздела «Тексты»."
    )


def _release_ticket(user_id: int) -> str:
    """Отпустить тикет через кнопку.

    Снимать может любой админ — это страховка от «зависших» тикетов.
    """
    claimed_by = support_claims.get(user_id)
    if claimed_by is None:
        return f"ℹ️ Тикет пользователя <code>{user_id}</code> не был закреплён."

    support_claims.pop(user_id, None)
    return (
        f"🔓 Тикет пользователя <code>{user_id}</code> отпущен. "
        f"Сообщения снова приходят всем админам."
    )


SUPPORT_HISTORY_PAGE_SIZE = 8
SUPPORT_HISTORY_MESSAGE_LIMIT = 280
SUPPORT_HISTORY_NAME_LIMIT = 64


def _short_support_text(value: str, limit: int) -> str:
    value = value or "—"
    return value if len(value) <= limit else value[: limit - 1] + "…"


async def _build_history_text(user_id: int, page: int = 0) -> tuple[str, int, int, int]:
    """Сформировать постраничный transcript из постоянной истории поддержки."""
    total = await db.get_support_message_count(user_id)
    total_pages = max(1, (total + SUPPORT_HISTORY_PAGE_SIZE - 1) // SUPPORT_HISTORY_PAGE_SIZE)
    page = min(max(page, 0), total_pages - 1)
    entries = await db.get_support_messages(
        user_id,
        limit=SUPPORT_HISTORY_PAGE_SIZE,
        offset=page * SUPPORT_HISTORY_PAGE_SIZE,
    )
    if not entries:
        return (
            f"ℹ️ История диалога с <code>{user_id}</code> пока пуста.",
            total,
            page,
            total_pages,
        )

    header = (
        f"🕘 <b>История диалога с {user_id}</b>\n"
        f"Сообщения {page * SUPPORT_HISTORY_PAGE_SIZE + 1}–"
        f"{page * SUPPORT_HISTORY_PAGE_SIZE + len(entries)} из {total} "
        f"• страница {page + 1}/{total_pages}"
    )
    lines = [header]
    for entry in entries:
        role = "👤 Пользователь" if entry["role"] == "user" else "👮 Администратор"
        safe_name = html.escape(
            _short_support_text(entry["sender_name"], SUPPORT_HISTORY_NAME_LIMIT),
            quote=False,
        )
        safe_text = html.escape(
            _short_support_text(entry["text"], SUPPORT_HISTORY_MESSAGE_LIMIT),
            quote=False,
        )
        lines.append(
            f"<i>{format_local_time(entry['created_at'])}</i> {role} "
            f"<b>{safe_name}</b>:\n<code>{safe_text}</code>"
        )
    return "\n\n".join(lines), total, page, total_pages


async def support_escalation_loop(bot: Bot):
    """Фоновая задача: эскалация неотвеченных сообщений поддержки.

    Раз в ESCALATION_POLL_INTERVAL секунд перебирает последние сообщения
    пользователей поддержки и те, на которые админ не ответил в течение
    ESCALATION_TIMEOUT, дублирует всем админам — чтобы тикет не завис на
    одном закреплённом админе. Одно сообщение эскалируется только один раз:
    после отправки ставится флаг, сбрасывается он новым сообщением юзера
    или ответом админа.
    """
    logger.info("🔁 Поллер эскалации поддержки запущен")
    while True:
        try:
            now = time.time()
            for user_id, ts in list(support_last_msg_ts.items()):
                if support_escalated.get(user_id):
                    continue
                if now - ts < ESCALATION_TIMEOUT:
                    continue
                # Юзер вышел из поддержки, пока тикет висел без ответа —
                # снимаем трекинг, эскалировать уже нечего.
                if not await _is_in_support(user_id):
                    _clear_support_tracking(user_id)
                    continue

                support_escalated[user_id] = True
                text = support_last_msg_text.get(user_id, "")
                name = support_last_msg_name.get(user_id, str(user_id))
                # Экранируем, чтобы HTML в тексте юзера не ломал карточку.
                safe_text = html.escape(text, quote=False)
                safe_name = html.escape(name, quote=False)
                claimed_by = support_claims.get(user_id)
                claim_note = (
                    f"🔒 Тикет закреплён за <code>{claimed_by}</code>, но ответа "
                    f"нет — берёт любой другой админ."
                    if claimed_by
                    else "Тикет никто не закрепил — возьмите в работу."
                )
                header = (
                    f"⏰ <b>Эскалация: пользователь ждёт ответа "
                    f"больше {ESCALATION_TIMEOUT // 60} минуты</b>\n\n"
                    f"👤 {safe_name} (ID: {user_id})\n"
                    f"💬 Текст: {safe_text}\n\n"
                    f"{claim_note}\n\n"
                    f"Как ответить:\n"
                    f"• нажмите «Ответить» на это сообщение\n"
                    f"• или используйте кнопки ниже"
                )
                sent = 0
                for admin_id in recipient_ids_for_event_sync("support"):
                    try:
                        esc_msg = await bot.send_message(
                            admin_id,
                            header,
                            reply_markup=_ticket_action_markup(user_id),
                            parse_mode="HTML",
                        )
                        _track_admin_msg(user_id, admin_id, esc_msg.message_id,
                                         support_escalation_msgs)
                        sent += 1
                    except Exception as e:
                        logger.warning(f"Не удалось отправить эскалацию админу {admin_id}: {e}")
                if sent:
                    logger.info(f"⏰ Эскалация: сообщение юзера {user_id} продублировано {sent} админам")
        except Exception as e:
            logger.warning(f"⚠️ Ошибка поллера эскалации: {e}")
        await asyncio.sleep(ESCALATION_POLL_INTERVAL)


@router.message(Command("cancel"))
async def cancel_action(message: Message, state: FSMContext):
    user_id = message.from_user.id
    cancelled = []

    current_state = await state.get_state()
    if current_state:
        await state.clear()
        cancelled.append(f"текущий шаг (<code>{current_state}</code>)")

    # Аннулируем одноразовые коды подтверждения критичных действий
    revoke_otp(user_id)

    if cancelled:
        await message.answer(
            "✅ <b>Отменено:</b> " + ", ".join(cancelled) + ".",
            parse_mode="HTML"
        )
    else:
        await message.answer("ℹ️ Нечего отменять — сейчас нет активных действий.")


async def show_user_orders(message_or_callback, user_id: int):
    try:
        orders = await db.get_user_orders(user_id)
        if not orders:
            text = "📭 У вас пока нет заказов.\n\nЗагляните в магазин! 🌱"
            markup = None
        else:
            status_emoji = {'new': '🆕', 'confirmed': '✅', 'completed': '📦', 'cancelled': '❌', 'awaiting_payment': '💳', 'awaiting_stars_payment': '⭐', 'awaiting_yookassa_payment': '🟣', 'payment_pending': '⏳', 'paid': '💰'}
            status_names = {'new': 'Новый', 'confirmed': 'Подтверждён', 'completed': 'Выполнен', 'cancelled': 'Отменён', 'awaiting_payment': 'Ожидает оплаты', 'awaiting_stars_payment': 'Ожидает Stars', 'awaiting_yookassa_payment': 'Ожидает оплаты (ЮKassa)', 'payment_pending': 'Ожидает подтверждения', 'paid': 'Оплачен'}
            lines = []
            builder = InlineKeyboardBuilder()
            for order in orders:
                delivery = order.get("delivery")
                delivery_text = f" · {delivery['shipment_label']}" if delivery else " · Доставка не указана"
                preview = ", ".join(
                    f"{html.escape(item['title'])}{f' ×{item['quantity']}' if item['quantity'] > 1 else ''}"
                    for item in order.get("items", [])[:3]
                ) or "Состав не сохранён"
                discount_text = f" · скидка {order['total_discount']} ₽" if order.get("total_discount") else ""
                lines.append(
                    f"{status_emoji.get(order['status'], '')} Заказ #{order['id']} · "
                    f"{order['total']} ₽ · {status_names.get(order['status'], order['status'])}"
                    f"{delivery_text} · {format_local_time(order['created_at'])}\n"
                    f"📚 {preview}{discount_text}"
                )
                builder.button(text=f"📦 Заказ #{order['id']}", callback_data=f"user_order_detail:{order['id']}")
            builder.adjust(1)
            text = "📜 <b>Ваши заказы:</b>\n\n" + "\n".join(lines)
            markup = builder.as_markup()
        if isinstance(message_or_callback, Message):
            await message_or_callback.answer(text, reply_markup=markup, parse_mode="HTML")
        else:
            await message_or_callback.message.answer(text, reply_markup=markup, parse_mode="HTML")
            await message_or_callback.answer()
    except Exception:
        logger.exception("Failed to load customer order history")
        if isinstance(message_or_callback, Message):
            await message_or_callback.answer("❌ Ошибка при загрузке заказов")
        else:
            await message_or_callback.message.answer("❌ Ошибка при загрузке заказов")
            await message_or_callback.answer()


@router.callback_query(F.data.regexp(r"^user_order_detail:\d+$"))
async def user_order_detail(callback: CallbackQuery):
    order_id = int(callback.data.split(":", 1)[1])
    order = await db.get_order_full(order_id)
    if not order or order["user_id"] != callback.from_user.id:
        await callback.answer("❌ Заказ не найден", show_alert=True)
        return
    delivery = order.get("delivery_summary")
    delivery_text = "Доставка не указана"
    builder = InlineKeyboardBuilder()
    if delivery:
        delivery_text = f"{delivery['method_label']}\nСтатус: {delivery['shipment_label']}"
        if delivery.get("public_instructions"):
            delivery_text += f"\n{html.escape(delivery['public_instructions'])}"
        if delivery["tracking"]:
            tracking = delivery["tracking"]
            delivery_text += f"\nТрек-номер: <code>{html.escape(tracking['number'])}</code>"
            tracking_label = {
                "sdek": "СДЭК",
                "russian_post": "Почту России",
            }.get(tracking.get("carrier"), delivery["method_label"])
            builder.button(text=f"📍 Отследить {tracking_label}", url=tracking["url"])
    builder.button(text="◀️ Все заказы", callback_data="my_orders")
    builder.adjust(1)
    receipt = format_order_receipt_html(order)
    await callback.message.answer(
        f"📦 <b>Заказ #{order['id']}</b>\n\n"
        f"Статус заказа: {html.escape(order['status'])}\n"
        f"{delivery_text}\n\n"
        f"{receipt}",
        reply_markup=builder.as_markup(),
        parse_mode="HTML",
    )
    await callback.answer()


async def process_checkout(message: Message, data: dict, bot: Bot):
    """Заглушка — теперь заказы обрабатываются через сервер напрямую"""
    pass


@router.callback_query(F.data == "ref_accept")
async def ref_accept_callback(callback: CallbackQuery, state: FSMContext, bot: Bot):
    data = await state.get_data()
    referrer_id = data.get('referrer_id')
    referred_id = callback.from_user.id

    if not referrer_id:
        await callback.answer("❌ Ошибка", show_alert=True)
        return

    await db.create_referral(referrer_id, referred_id)
    await db.add_user_bonus(referrer_id, 'percent', 15)
    await db.add_user_bonus(referred_id, 'percent', 10)

    await state.clear()

    await callback.message.edit_text(
        f"🎉 <b>Отлично!</b>\n\n"
        f"✅ Вам начислена <b>скидка 10%</b> на первый заказ!\n"
        f"✅ Ваш друг получил <b>скидку 15%</b> на следующий заказ!\n\n"
        f"Скидка применится автоматически при оформлении заказа в Mini App 🛒",
        parse_mode="HTML"
    )
    await callback.answer()

    try:
        await bot.send_message(
            referrer_id,
            f"🎁 <b>Ваш друг присоединился!</b>\n\n"
            f"Вы получили <b>скидку 15%</b> на следующий заказ!\n"
            f"Скидка применится автоматически 🎉",
            parse_mode="HTML"
        )
    except Exception:
        pass


@router.callback_query(F.data == "ref_decline")
async def ref_decline_callback(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text(
        "Хорошо! Добро пожаловать в «Семена Знаний» 🌿\n\n"
        "Нажмите кнопку ниже, чтобы открыть каталог 👇",
    )
    await callback.answer()


@router.callback_query(F.data == "invite_friend")
async def invite_friend_callback(callback: CallbackQuery, bot: Bot):
    user_id = callback.from_user.id
    # Раздел «Пригласить» выводит из режима диалога с поддержкой
    await set_support_mode(user_id, False)
    ref_code = await db.get_referral_code(user_id)
    me = await bot.get_me()
    ref_link = f"https://t.me/{me.username}?start={ref_code}"

    stats = await db.get_referral_stats(user_id)

    builder = InlineKeyboardBuilder()
    builder.button(text="📤 Поделиться",
                   switch_inline_query=f"🌿 Приглашаю тебя в книжный магазин «Семена Знаний»!\n\nПолучи скидку 10% на первый заказ по моей ссылке:\n{ref_link}")
    builder.button(text="◀️ Назад", callback_data="main_menu")
    builder.adjust(1)

    await callback.message.answer(
        f"👥 <b>Пригласи друга — получи бонус!</b>\n\n"
        f"🔗 <b>Твоя ссылка:</b>\n<code>{ref_link}</code>\n\n"
        f"📊 <b>Твоя статистика:</b>\n"
        f"• Приглашено друзей: <b>{stats['total_invited']}</b>\n"
        f"• Получено бонусов: <b>{stats['bonuses_earned']}</b>\n"
        f"• Активных скидок: <b>{stats['active_bonuses']}</b>\n\n"
        f"🎁 <b>Как это работает:</b>\n"
        f"• Друг получает <b>скидку 10%</b> на первый заказ\n"
        f"• Ты получаешь <b>скидку 15%</b> на следующий заказ\n"
        f"• Скидки применяются автоматически в Mini App",
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(F.data == "skip_description")
async def skip_description(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await state.clear()
        await callback.answer("❌ Нет прав", show_alert=True)
        return
    data = await state.get_data()

    book_id = await db.add_book(
        data['title'],
        data['price'],
        data['category'],
        data.get('emoji', ''),
        '',
        '[]',
        data.get('category_id')
    )

    await callback.message.answer(
        f"✅ <b>Книга добавлена!</b>\n\n"
        f"🆔 ID: {book_id}\n"
        f"📖 {data['title']}\n"
        f"💰 {data['price']} ₽\n"
        f"📂 {data['category']}\n"
        f"🎨 {data.get('emoji', '') or '📚'}\n"
        f"📝 Описание не добавлено\n\n"
        f"🎉 Теперь она доступна в Mini App!\n\n"
        f"💡 Вы можете добавить описание позже через редактирование.",
        parse_mode="HTML"
    )
    await state.clear()
    await callback.answer()