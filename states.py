from aiogram.fsm.state import State, StatesGroup


class AddBookState(StatesGroup):
    waiting_for_title = State()
    waiting_for_price = State()
    waiting_for_category = State()
    waiting_for_emoji = State()
    waiting_for_description = State()
    waiting_for_inner_images = State()


class EditBookState(StatesGroup):
    waiting_for_field = State()
    waiting_for_value = State()
    waiting_for_description = State()
    waiting_for_image_url = State()
    waiting_for_image_photo = State()
    waiting_for_new_category = State()


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

class PaymentSettingsState(StatesGroup):
    waiting_for_card = State()
    waiting_for_sbp_phone = State()
    waiting_for_sbp_bank = State()
    waiting_for_recipient = State()
    waiting_for_instructions = State()
    waiting_for_stars_rate = State()  # ← Добавь эту строку