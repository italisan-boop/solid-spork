from dataclasses import dataclass


@dataclass(frozen=True)
class MessageTemplate:
    key: str
    group: str
    title: str
    default: str
    placeholders: frozenset[str] = frozenset()
    render_mode: str = "telegram_html"


TEMPLATES: tuple[MessageTemplate, ...] = (
    MessageTemplate(
        key="support.quick.greeting",
        group="support",
        title="Приветствие",
        default="Здравствуйте! 👋 Чем можем помочь?",
    ),
    MessageTemplate(
        key="support.quick.wait",
        group="support",
        title="Ожидание ответа",
        default="Спасибо за обращение! 🙏 Мы разберёмся и скоро вернёмся с ответом.",
    ),
    MessageTemplate(
        key="support.quick.ask_details",
        group="support",
        title="Запрос деталей",
        default=(
            "Подскажите, пожалуйста, подробнее:\n"
            "• номер заказа или название книги\n"
            "• что именно произошло\n"
            "Так мы сможем помочь быстрее."
        ),
    ),
    MessageTemplate(
        key="support.quick.payment",
        group="support",
        title="Оплата",
        default=(
            "Оплата доступна прямо в Mini App через раздел «Корзина».\n"
            "Если что-то не получается — опишите, что видите на экране, поможем."
        ),
    ),
    MessageTemplate(
        key="support.quick.resolved",
        group="support",
        title="Вопрос решён",
        default="Ваш вопрос решён ✅. Если появятся ещё вопросы — пишите, мы на связи.",
    ),
    MessageTemplate(
        key="support.reply.header",
        group="support",
        title="Заголовок обычного ответа",
        default="💬 <b>Ответ от поддержки «Семена Знаний»:</b>",
    ),
    MessageTemplate(
        key="support.reply.follow_up",
        group="support",
        title="Подсказка после ответа",
        default="Можете ответить прямо здесь — сообщение придёт администратору.",
    ),
)


TEMPLATES_BY_KEY = {template.key: template for template in TEMPLATES}
QUICK_TEMPLATE_KEYS = {
    "greeting": "support.quick.greeting",
    "wait": "support.quick.wait",
    "ask_details": "support.quick.ask_details",
    "payment": "support.quick.payment",
    "resolved": "support.quick.resolved",
}


def get_template(key: str) -> MessageTemplate:
    return TEMPLATES_BY_KEY[key]


def templates_for_group(group: str) -> tuple[MessageTemplate, ...]:
    return tuple(template for template in TEMPLATES if template.group == group)
