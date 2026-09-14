import json
import asyncio
import time
from collections import deque
from aiogram import Bot, Router, F
from aiogram.types import Message, WebAppInfo, CallbackQuery, LabeledPrice
from aiogram.filters import CommandStart, Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.context import FSMContext
from aiogram.exceptions import TelegramForbiddenError

import db
from config import settings
from states import AddBookState, EditBookState, CategoryState, PromoCodeState, ReferralState, PaymentSettingsState
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

# История переписки с пользователями поддержки: user_id → список сообщений
# (по одному на каждое сообщение пользователя и на каждый ответ админа).
# Хранит до HISTORY_LIMIT последних сообщений, чтобы админ по
# /history_<id> мог быстро вспомнить контекст диалога. Живёт в памяти
# процесса (как support_claims) — при рестарте обнуляется; в БД не
# пишем намеренно: история не нужна после рестарта, а постоянный лог
# диалогов — это уже отдельная фича.
HISTORY_LIMIT = 50
support_history: dict[int, list[dict]] = {}

# === РЕЙТ-ЛИМИТ НА СООБЩЕНИЯ ПОДДЕРЖКИ ===
# Защита от спама в обе стороны:
#  - админ → пользователь (/reply_): пауза REPLY_MIN_GAP сек между двумя
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
# Когда кто-то берёт тикет в работу (/claim_<user_id>) или отвечает —
# все эти сообщения редактируются в «тикет уже в работе / на него ответили»,
# чтобы остальные админы не путались и не отвечали параллельно.
# user_id -> список кортежей (chat_id, message_id).
support_forward_msgs: dict[int, list[tuple[int, int]]] = {}
support_escalation_msgs: dict[int, list[tuple[int, int]]] = {}

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


async def _rewrite_ticket_notices(user_id: int, bot: Bot, status_text: str) -> None:
    """Переписать все уведомления админов по тикету (пересылки + эскалации).

    Вызывается, когда тикет берут в работу (/claim_<user_id>) или на него уже
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
                parse_mode="HTML",
            )
        except Exception as e:
            logger.warning(
                f"Не удалось обновить уведомление по тикету {user_id} "
                f"({chat_id}/{message_id}): {e}"
            )


def _append_history(user_id: int, role: str, name: str, text: str) -> None:
    """Дописать сообщение в историю диалога и подрезать до HISTORY_LIMIT.

    role: "user" (сообщение от пользователя) или "admin" (ответ поддержки).
    """
    support_history.setdefault(user_id, []).append({
        "role": role,
        "name": name,
        "text": text,
        "ts": time.time(),
    })
    if len(support_history[user_id]) > HISTORY_LIMIT:
        support_history[user_id] = support_history[user_id][-HISTORY_LIMIT:]

# Шаблоны быстрых ответов поддержки. Использование:
#   /reply_<user_id> +<имя>  →  бот подставит текст из этого словаря.
# Текст шаблона уходит пользователю как есть, без префикса «Ответ от поддержки».
SUPPORT_TEMPLATES: dict[str, str] = {
    "greeting": "Здравствуйте! 👋 Чем можем помочь?",
    "wait": "Спасибо за обращение! 🙏 Мы разберёмся и скоро вернёмся с ответом.",
    "ask_details": (
        "Подскажите, пожалуйста, подробнее:\n"
        "• номер заказа или название книги\n"
        "• что именно произошло\n"
        "Так мы сможем помочь быстрее."
    ),
    "payment": (
        "Оплата доступна прямо в Mini App через раздел «Корзина».\n"
        "Если что-то не получается — опишите, что видите на экране, поможем."
    ),
    "resolved": "Ваш вопрос решён ✅. Если появятся ещё вопросы — пишите, мы на связи.",
}


def is_admin(user_id: int) -> bool:
    return user_id in settings.ADMIN_IDS


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
    settings.broadcast_pending_users.discard(message.from_user.id)

    ref_code = message.text.split()[1] if len(message.text.split()) > 1 else ""
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
    settings.broadcast_pending_users.discard(message.from_user.id)
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


@router.callback_query(F.data == "support")
async def support_callback(callback: CallbackQuery, state: FSMContext):
    """Обработка кнопки поддержки"""
    # Сбрасываем любое накопившееся FSM-состояние, чтобы следующее текстовое
    # сообщение гарантированно ушло в поддержку, а не было проглочено
    # обработчиком какого-нибудь waiting_for_* из админки.
    await state.clear()
    await set_support_mode(callback.from_user.id, True)
    await callback.message.answer(
        "🆘 <b>Служба поддержки</b>\n\n"
        "Напишите ваш вопрос, и администратор ответит!\n\n"
        "💬 Вы можете отправлять несколько сообщений подряд — все они уйдут "
        "в поддержку, отвечать на них можно прямо здесь.\n\n"
        "/cancel — выйти из диалога с поддержкой.",
        parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(F.data.startswith("confirm_"))
async def confirm_order(callback: CallbackQuery):
    order_id = int(callback.data.split("_")[1])
    order = await db.get_order_full(order_id)
    if not order:
        await callback.answer("❌ Заказ не найден", show_alert=True)
        return
    await db.update_order_status(order_id, 'confirmed')
    books_list = "\n".join([f"  • {item['title']}" for item in order['items']])
    await callback.message.answer(
        f"✅ <b>Заказ #{order_id} подтверждён!</b>\n\n{books_list}\n\n"
        f"💰 Сумма: <b>{order['total']} ₽</b>\n\n"
        f"Мы свяжемся с вами для уточнения доставки.",
        parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(F.data.startswith("cancel_"))
async def cancel_order(callback: CallbackQuery):
    order_id = int(callback.data.split("_")[1])
    await db.update_order_status(order_id, 'cancelled')
    await callback.message.answer(f"❌ Заказ #{order_id} отменён.\n\nВозвращайтесь, когда будете готовы! 🌱")
    await callback.answer()


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
        # Пишем в историю ДО отправки админам — даже если рассылка упадёт,
        # сообщение пользователя в логе останется.
        _append_history(
            user_id,
            role="user",
            name=message.from_user.full_name or str(user_id),
            text=message.text,
        )
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
        recipients = (
            [claimed_by] if claimed_by in settings.ADMIN_IDS
            else settings.ADMIN_IDS
        )
        claim_note = (
            f"\n🔒 <i>Тикет закреплён за вами. Чтобы отпустить: "
            f"<code>/release_{user_id}</code></i>"
            if claimed_by
            else f"\nЧтобы взять тикет в работу и не отвечать параллельно с коллегами: "
                 f"<code>/claim_{user_id}</code>"
        )
        for admin_id in recipients:
            try:
                sent = await bot.send_message(
                    admin_id,
                    f"🆘 <b>Сообщение от пользователя</b>\n\n"
                    f"👤 {message.from_user.full_name} (ID: {message.from_user.id})\n"
                    f"💬 Текст: {message.text}\n\n"
                    f"Чтобы ответить:\n<code>/reply_{message.from_user.id} ваш_ответ</code>\n"
                    f"Быстрые шаблоны: <code>/reply_{message.from_user.id} +greeting</code> и др. — "
                    f"список: /templates\n"
                    f"Посмотреть последние 20 сообщений: "
                    f"<code>/history_{message.from_user.id}</code>"
                    f"{claim_note}",
                    parse_mode="HTML"
                )
                _track_admin_msg(user_id, admin_id, sent.message_id,
                                 support_forward_msgs)
            except Exception as e:
                logger.warning(f"Не удалось отправить сообщение админу {admin_id}: {e}")
        # Не убираем пользователя из support_pending_users — пусть продолжает
        # диалог, не нажимая каждый раз кнопку «Поддержка».
        await message.answer(
            "✅ <b>Сообщение отправлено в поддержку!</b>\n\n"
            "Можете продолжать писать здесь — администратор ответит в этом же чате.\n"
            "Чтобы выйти из диалога, отправьте /cancel."
        )
        return

    # 🔧 ВАЖНО: пропускаем сообщения, если пользователь в состоянии настройки оплаты
    payment_states = [
        PaymentSettingsState.waiting_for_card.state,
        PaymentSettingsState.waiting_for_sbp_phone.state,
        PaymentSettingsState.waiting_for_sbp_bank.state,
        PaymentSettingsState.waiting_for_recipient.state,
        PaymentSettingsState.waiting_for_instructions.state,
        PaymentSettingsState.waiting_for_stars_rate.state,
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
        await message.answer("💰 Теперь отправьте <b>цену</b> (только цифры):")
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
                await message.answer("📂 Отправьте <b>категорию</b>:")
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
            f"Отправьте ещё URL или нажмите /cancel"
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

    # === РАССЫЛКА ===
    # === РАССЫЛКА ===
    if is_admin(user_id) and user_id in settings.broadcast_pending_users:
        settings.broadcast_pending_users.discard(user_id)
        user_ids = await db.get_all_unique_users()

        if not user_ids:
            await message.answer("📭 Нет пользователей для рассылки.")
            return

        await message.answer(
            f"📢 Начинаю рассылку <b>{len(user_ids)}</b> пользователям... Это может занять некоторое время.",
            parse_mode="HTML")

        success, failed = 0, 0
        for uid in user_ids:
            try:
                await bot.send_message(uid, message.text, parse_mode="HTML")
                success += 1
            except Exception:
                # Ошибка обычно означает, что пользователь заблокировал бота или удалил аккаунт
                failed += 1
            await asyncio.sleep(0.05)  # Небольшая задержка, чтобы не получить бан от Telegram API

        await message.answer(
            f"✅ <b>Рассылка завершена!</b>\n\n"
            f"📤 Успешно отправлено: <b>{success}</b>\n"
            f"⚠️ Ошибок (заблокировали бота): <b>{failed}</b>",
            parse_mode="HTML"
        )
        return

    # Поддержка обрабатывается в начале функции, чтобы ни одно FSM-состояние
    # не перехватывало сообщение раньше.


@router.message(F.photo)
async def handle_photo(message: Message, state: FSMContext):
    """Обработка загруженных фото"""
    current_state = await state.get_state()

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
            f"Отправьте ещё фото или нажмите /cancel"
        )
    else:
        await message.answer("❌ Сейчас не ожидаю фото. Используйте /cancel для отмены.")


@router.message(F.text.startswith("/reply_"))
async def admin_reply_to_user(message: Message):
    if not is_admin(message.from_user.id):
        return
    try:
        parts = message.text.split(maxsplit=1)
        user_id = int(parts[0].replace("/reply_", ""))
        body = parts[1].strip() if len(parts) > 1 else ""

        # Рейт-лимит: пауза между ответами одному пользователю + лимит админа.
        err = _rate_limited(
            reply_last_user_ts, reply_admin_log,
            (message.from_user.id, user_id), message.from_user.id,
        )
        if err:
            await message.answer(err)
            return

        # Шаблон быстрого ответа: /reply_<id> +<имя> → подставляем текст.
        # Шаблон отправляется пользователю как есть, без обёртки «Ответ от
        # поддержки», чтобы не дублировать приветствие из самого шаблона.
        is_template = body.startswith("+")
        if is_template:
            template_name = body[1:].split()[0] if body[1:].strip() else ""
            reply_text = SUPPORT_TEMPLATES.get(template_name)
            if reply_text is None:
                names = ", ".join(f"<code>+{n}</code>" for n in SUPPORT_TEMPLATES)
                await message.answer(
                    f"❌ Шаблон <code>+{template_name}</code> не найден.\n\n"
                    f"Доступные шаблоны: {names}\n"
                    f"Полный список с текстами — /templates",
                    parse_mode="HTML",
                )
                return
            user_message = f"{reply_text}\n\nМожете ответить прямо здесь — сообщение придёт администратору."
        else:
            # Обычный ответ: оборачиваем в шапку «Ответ от поддержки».
            if not body:
                body = "Без текста"
            user_message = (
                f"💬 <b>Ответ от поддержки «Семена Знаний»:</b>\n\n"
                f"{body}\n\n"
                f"Можете ответить прямо здесь — сообщение придёт администратору."
            )

        # В историю пишем «чистый» текст ответа (без обёртки «Ответ от
        # поддержки…» и трейлера «Можете ответить прямо здесь…»), чтобы
        # админу в /history_<id> было удобно читать.
        history_text = reply_text if is_template else (body or "Без текста")
        _append_history(
            user_id,
            role="admin",
            name=message.from_user.full_name or str(message.from_user.id),
            text=history_text,
        )
        await message.bot.send_message(user_id, user_message, parse_mode="HTML")
        # Админ ответил — сбрасываем трекинг эскалации, чтобы поллер не
        # дублировал это сообщение повторно.
        _clear_support_tracking(user_id)
        # Уведомления админам (пересылки и эскалации) переписываем в
        # «на вопрос уже ответили», чтобы остальные не дублировали ответ.
        await _rewrite_ticket_notices(
            user_id,
            message.bot,
            f"✅ <b>На это сообщение уже ответили</b>\n\n"
            f"👤 Пользователь ID <code>{user_id}</code> получил ответ "
            f"от {message.from_user.full_name} (ID: <code>{message.from_user.id}</code>).",
        )
        # На случай, если пользователь был сброшен из support_pending_users
        # (например, после перезапуска бота) — вернём его в режим диалога,
        # чтобы ответ ушёл в поддержку без повторного нажатия кнопки.
        await set_support_mode(user_id, True)
        claim_hint = (
            f"\n\n💡 <i>Чтобы коллеги не отвечали параллельно — "
            f"закрепите тикет за собой:</i> <code>/claim_{user_id}</code>\n"
            f"<i>Когда закончите — отпустите:</i> <code>/release_{user_id}</code>"
            if support_claims.get(user_id) != message.from_user.id
            else f"\n\n🔒 <i>Тикет уже закреплён за вами. "
                 f"Отпустить:</i> <code>/release_{user_id}</code>"
        )
        used_note = (
            f" (шаблон <code>+{template_name}</code>)" if is_template else ""
        )
        await message.answer(
            f"✅ Ответ отправлен пользователю ID {user_id}!{used_note}{claim_hint}",
            parse_mode="HTML",
        )
    except (ValueError, IndexError):
        await message.answer("❌ Неверный формат: `/reply_ID текст`", parse_mode="HTML")
    except TelegramForbiddenError:
        # Пользователь заблокировал бота — переключаем его из режима диалога,
        # чтобы дальнейшие попытки не сыпались в пустоту.
        await set_support_mode(user_id, False)
        logger.warning(f"Не удалось отправить ответ пользователю {user_id}: бот заблокирован")
        await message.answer(
            f"🚫 Пользователь ID <code>{user_id}</code> заблокировал бота — "
            f"доставить ответ нельзя. Диалог с поддержкой для него закрыт.",
            parse_mode="HTML"
        )
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")


@router.message(Command("templates"))
async def admin_list_templates(message: Message):
    """Показывает админу список быстрых шаблонов и их текст."""
    if not is_admin(message.from_user.id):
        return
    if not SUPPORT_TEMPLATES:
        await message.answer("ℹ️ Шаблонов пока нет.")
        return
    lines = ["📝 <b>Шаблоны быстрых ответов</b>\n"]
    lines.append(
        "Использование: <code>/reply_&lt;user_id&gt; +&lt;имя&gt;</code>\n"
    )
    for name, text in SUPPORT_TEMPLATES.items():
        # Пре��ью режем по строкам, чтобы карточка не разрасталась.
        preview = text if len(text) <= 120 else text[:120] + "…"
        lines.append(f"<b>+{name}</b>\n<code>{preview}</code>")
    await message.answer("\n\n".join(lines), parse_mode="HTML")


@router.message(F.text.startswith("/claim_"))
async def admin_claim_ticket(message: Message):
    """Админ берёт тикет пользователя в работу: `/claim_<user_id>`.

    После этого сообщения пользователя из поддержки идут только этому
    админу, чтобы коллеги не отвечали параллельно.
    """
    if not is_admin(message.from_user.id):
        return
    try:
        user_id = int(message.text.split()[0].replace("/claim_", ""))
    except (ValueError, IndexError):
        await message.answer("❌ Неверный формат: `/claim_ID`", parse_mode="HTML")
        return

    in_support = user_id in settings.support_pending_users
    if not in_support:
        try:
            if await db.is_support_active(user_id):
                settings.support_pending_users.add(user_id)
                in_support = True
        except Exception as e:
            logger.warning(f"is_support_active({user_id}) упал: {e}")

    if not in_support:
        await message.answer(
            f"ℹ️ Пользователь ID <code>{user_id}</code> сейчас не в диалоге с поддержкой — "
            f"закреплять нечего.",
            parse_mode="HTML",
        )
        return

    claimed_by = support_claims.get(user_id)
    if claimed_by == message.from_user.id:
        await message.answer(
            f"✅ Тикет пользователя <code>{user_id}</code> уже закреплён за вами.",
            parse_mode="HTML",
        )
        return
    if claimed_by is not None:
        await message.answer(
            f"🚫 Тикет пользователя <code>{user_id}</code> уже ведёт другой админ "
            f"(ID <code>{claimed_by}</code>). Попросите коллегу отпустить: "
            f"<code>/release_{user_id}</code>.",
            parse_mode="HTML",
        )
        return

    support_claims[user_id] = message.from_user.id
    # Все разосланные по этому тикету уведомления (пересылки и дубли
    # эскалации) переписываем в «тикет уже в работе» — остальные админы
    # видят, что вопрос ведёт конкретный человек, и не отвечают параллельно.
    await _rewrite_ticket_notices(
        user_id,
        message.bot,
        f"🔒 <b>Тикет уже в работе</b>\n\n"
        f"👤 Пользователь ID <code>{user_id}</code>\n"
        f"👮 Взял в работу: {message.from_user.full_name} "
        f"(ID: <code>{message.from_user.id}</code>)\n\n"
        f"Отвечает он, параллельные ответы коллег не нужны.",
    )
    await message.answer(
        f"🔒 Тикет пользователя <code>{user_id}</code> закреплён за вами. "
        f"Следующие сообщения от него придут только вам.\n"
        f"Когда закончите — отпустите командой <code>/release_{user_id}</code>.\n\n"
        f"💡 Для быстрых ответов есть шаблоны: <code>/reply_{user_id} +greeting</code> "
        f"(приветствие), <code>+wait</code>, <code>+resolved</code> и др. "
        f"Полный список — /templates.",
        parse_mode="HTML",
    )


@router.message(F.text.startswith("/release_"))
async def admin_release_ticket(message: Message):
    """Админ отпускает тикет пользователя: `/release_<user_id>`.

    После этого сообщения снова идут всем админам. Снимать может любой
    админ — это страховка от «зависших» тикетов.
    """
    if not is_admin(message.from_user.id):
        return
    try:
        user_id = int(message.text.split()[0].replace("/release_", ""))
    except (ValueError, IndexError):
        await message.answer("❌ Неверный формат: `/release_ID`", parse_mode="HTML")
        return

    claimed_by = support_claims.get(user_id)
    if claimed_by is None:
        await message.answer(
            f"ℹ️ Тикет пользователя <code>{user_id}</code> не был закреплён.",
            parse_mode="HTML",
        )
        return

    support_claims.pop(user_id, None)
    await message.answer(
        f"🔓 Тикет пользователя <code>{user_id}</code> отпущен. "
        f"Сообщения снова приходят всем админам.",
        parse_mode="HTML",
    )


@router.message(F.text.startswith("/history_"))
async def admin_history(message: Message):
    """История диалога с пользователем: `/history_<user_id>`.

    Показывает последние 20 сообщений (от пользователя и от поддержки)
    из in-memory лога, который пишется в `universal_text_handler` и
    `admin_reply_to_user`. При рестарте процесса лог обнуляется.
    """
    if not is_admin(message.from_user.id):
        return
    try:
        user_id = int(message.text.split()[0].replace("/history_", ""))
    except (ValueError, IndexError):
        await message.answer("❌ Неверный формат: `/history_ID`", parse_mode="HTML")
        return

    entries = support_history.get(user_id) or []
    if not entries:
        await message.answer(
            f"ℹ️ История диалога с <code>{user_id}</code> пуста "
            f"(либо диалога ещё не было, либо процесс был перезапущен).",
            parse_mode="HTML",
        )
        return

    last = entries[-20:]
    header = (
        f"🕘 <b>История диалога с {user_id}</b> "
        f"(показаны последние {len(last)} из {len(entries)}):\n"
    )
    lines = [header]
    for e in last:
        ts = time.strftime("%H:%M:%S", time.localtime(e["ts"]))
        role = "👤 Юзер" if e["role"] == "user" else "👮 Админ"
        # Имя и текст экранируем, чтобы HTML в сообщении юзера не ломал карточку.
        raw_name = e["name"] or "—"
        safe_name = raw_name.replace("<", "&lt;").replace(">", "&gt;")
        raw_text = e["text"] if len(e["text"]) <= 200 else e["text"][:200] + "…"
        safe_text = raw_text.replace("<", "&lt;").replace(">", "&gt;")
        lines.append(
            f"<i>{ts}</i> {role} <b>{safe_name}</b>:\n<code>{safe_text}</code>"
        )
    await message.answer("\n\n".join(lines), parse_mode="HTML")


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
                safe_text = text.replace("<", "&lt;").replace(">", "&gt;")
                safe_name = name.replace("<", "&lt;").replace(">", "&gt;")
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
                    f"Чтобы ответить:\n<code>/reply_{user_id} ваш_ответ</code>\n"
                    f"Посмотреть историю диалога: <code>/history_{user_id}</code>\n"
                    f"Закрепить тикет за собой: <code>/claim_{user_id}</code>"
                )
                sent = 0
                for admin_id in settings.ADMIN_IDS:
                    try:
                        esc_msg = await bot.send_message(admin_id, header, parse_mode="HTML")
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

    if user_id in settings.support_pending_users or await db.is_support_active(user_id):
        await set_support_mode(user_id, False)
        cancelled.append("диалог с поддержкой")

    if user_id in settings.broadcast_pending_users:
        settings.broadcast_pending_users.discard(user_id)
        cancelled.append("черновик рассылки")

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
        else:
            status_emoji = {'new': '🆕', 'confirmed': '✅', 'completed': '📦', 'cancelled': '❌', 'awaiting_payment': '💳', 'awaiting_stars_payment': '⭐', 'payment_pending': '⏳', 'paid': '💰'}
            status_names = {'new': 'Новый', 'confirmed': 'Подтверждён', 'completed': 'Выполнен', 'cancelled': 'Отменён', 'awaiting_payment': 'Ожидает оплаты', 'awaiting_stars_payment': 'Ожидает Stars', 'payment_pending': 'Ожидает подтверждения', 'paid': 'Оплачен'}
            orders_list = [
                f"{status_emoji.get(o['status'], '')} Заказ #{o['id']} · {o['total']} ₽ · {status_names.get(o['status'], o['status'])} · {format_local_time(o['created_at'])}"
                for o in orders]
            text = "📜 <b>Ваши заказы:</b>\n\n" + "\n".join(orders_list)
        if isinstance(message_or_callback, Message):
            await message_or_callback.answer(text, parse_mode="HTML")
        else:
            await message_or_callback.message.answer(text, parse_mode="HTML")
            await message_or_callback.answer()
    except Exception as e:
        logger.warning(f"Ошибка заказов: {e}")
        if isinstance(message_or_callback, Message):
            await message_or_callback.answer("❌ Ошибка при загрузке заказов")
        else:
            await message_or_callback.message.answer("❌ Ошибка при загрузке заказов")
            await message_or_callback.answer()


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