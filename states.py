from aiogram.fsm.state import State, StatesGroup


class AddBookState(StatesGroup):
    waiting_for_title = State()
    waiting_for_author = State()
    waiting_for_description = State()
    waiting_for_price = State()
    waiting_for_category = State()
    waiting_for_emoji = State()
    waiting_for_inner_images = State()
    waiting_for_cover_photo = State()
    waiting_for_page_photos = State()
    confirming = State()


class EditBookState(StatesGroup):
    waiting_for_field = State()
    waiting_for_value = State()
    waiting_for_description = State()
    waiting_for_image_url = State()
    waiting_for_image_photo = State()
    waiting_for_new_category = State()
    waiting_for_new_title = State()
    waiting_for_new_author = State()
    waiting_for_new_price = State()
    waiting_for_new_description = State()
    waiting_for_new_cover = State()    # обложка (фото или URL) из admin_books
    waiting_for_new_page = State()     # фото страницы (фото или URL) из admin_books
    waiting_for_new_category_admin = State()  # новая категория из admin_books (возврат в admin_book_edit)


class AdminBooksState(StatesGroup):
    """Состояния админского списка книг: сортировка и поиск."""
    waiting_for_search = State()  # админ ввёл подстроку для поиска


class CategoryState(StatesGroup):
    waiting_for_name = State()
    waiting_for_emoji = State()
    editing_name = State()
    editing_emoji = State()


class PromoCodeState(StatesGroup):
    waiting_for_code = State()
    waiting_for_discount = State()
    waiting_for_min_order = State()
    waiting_for_max_uses = State()
    waiting_for_expires = State()
    editing_discount = State()
    editing_min_order = State()
    editing_max_uses = State()
    editing_expires = State()

class ReferralState(StatesGroup):
    waiting_for_confirmation = State()

class PaymentReceiptState(StatesGroup):
    """Ожидание фото чека от пользователя после нажатия «Я оплатил». """
    waiting_for_photo = State()

class PaymentSettingsState(StatesGroup):
    waiting_for_card = State()
    waiting_for_sbp_phone = State()
    waiting_for_sbp_bank = State()
    waiting_for_recipient = State()
    waiting_for_instructions = State()
    waiting_for_stars_rate = State()  # ← Добавь эту строку


class CriticalActionState(StatesGroup):
    """Ожидание одноразового кода подтверждения критичного действия."""
    waiting_for_code = State()


class TextSettingsState(StatesGroup):
    waiting_for_value = State()


class SupportReplyState(StatesGroup):
    """Админ нажал «⚡ Ответить» на тикет — ждём текст ответа."""
    waiting_for_text = State()