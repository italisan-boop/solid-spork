import html
import string
from html.parser import HTMLParser

import aiosqlite

from content_defaults import TEMPLATES, get_template
from db.connection import connection


_ALLOWED_TAGS = {
    "b", "strong", "i", "em", "u", "ins", "s", "strike", "del",
    "code", "pre", "blockquote", "tg-spoiler", "tg-emoji",
}


class TemplateValidationError(ValueError):
    pass


class _TelegramHtmlValidator(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.stack: list[str] = []
        self.error: str | None = None

    def _fail(self, message: str) -> None:
        if self.error is None:
            self.error = message

    def handle_startendtag(self, tag: str, attrs) -> None:
        self._fail("Самозакрывающиеся HTML-теги не поддерживаются Telegram.")

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        attributes = dict(attrs)
        if tag == "span":
            if attributes != {"class": "tg-spoiler"}:
                self._fail("Разрешён только <span class=\"tg-spoiler\">.")
                return
            tag = "tg-spoiler"
        elif tag == "a":
            href = attributes.get("href", "")
            if set(attributes) != {"href"} or not href.lower().startswith(("https://", "http://", "tg://")):
                self._fail("Ссылка должна содержать безопасный href.")
                return
        elif tag == "tg-emoji":
            if set(attributes) != {"emoji-id"} or not attributes["emoji-id"]:
                self._fail("Тег tg-emoji должен содержать emoji-id.")
                return
        elif tag not in _ALLOWED_TAGS or attrs:
            self._fail(f"Тег <{tag}> не поддерживается Telegram.")
            return
        self.stack.append(tag)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "span":
            tag = "tg-spoiler"
        if not self.stack or self.stack[-1] != tag:
            self._fail(f"Несбалансированный закрывающий тег </{tag}>.")
            return
        self.stack.pop()

    def close(self) -> None:
        super().close()
        if self.stack and self.error is None:
            self.error = f"Не закрыт тег <{self.stack[-1]}>."


def _placeholder_names(value: str) -> set[str]:
    names: set[str] = set()
    try:
        fields = string.Formatter().parse(value)
    except ValueError as exc:
        raise TemplateValidationError("Некорректные фигурные скобки в шаблоне.") from exc
    for _, field_name, format_spec, conversion in fields:
        if field_name is None:
            continue
        if not field_name or any(marker in field_name for marker in ".[") or format_spec or conversion:
            raise TemplateValidationError("Используйте только плейсхолдеры вида {name}.")
        names.add(field_name)
    return names


def validate_template_value(key: str, value: str) -> None:
    definition = get_template(key)
    if not value.strip():
        raise TemplateValidationError("Текст шаблона не может быть пустым.")
    if len(value) > 4096:
        raise TemplateValidationError("Текст шаблона не должен превышать 4096 символов.")
    actual = _placeholder_names(value)
    if actual != set(definition.placeholders):
        expected_text = ", ".join(f"{{{name}}}" for name in sorted(definition.placeholders)) or "без плейсхолдеров"
        raise TemplateValidationError(f"Допустимы только плейсхолдеры: {expected_text}.")
    if definition.render_mode == "telegram_html":
        parser = _TelegramHtmlValidator()
        try:
            parser.feed(value)
            parser.close()
        except Exception as exc:
            raise TemplateValidationError("Некорректная HTML-разметка Telegram.") from exc
        if parser.error:
            raise TemplateValidationError(parser.error)


def render_template(key: str, value: str, **values: object) -> str:
    definition = get_template(key)
    if set(values) != set(definition.placeholders):
        raise ValueError(f"Плейсхолдеры для {key} переданы неверно.")
    rendered_values = {
        name: html.escape(str(item), quote=False)
        for name, item in values.items()
    }
    return value.format(**rendered_values)




async def get_message_template(key: str) -> str:
    definition = get_template(key)
    async with connection() as database:
        cursor = await database.execute(
            "SELECT template_value FROM message_templates WHERE template_key = ?", (key,)
        )
        row = await cursor.fetchone()
    return row[0] if row else definition.default


async def get_message_templates() -> dict[str, str]:
    async with connection() as database:
        cursor = await database.execute("SELECT template_key, template_value FROM message_templates")
        rows = await cursor.fetchall()
    values = {template.key: template.default for template in TEMPLATES}
    values.update({row[0]: row[1] for row in rows})
    return values


async def set_message_template(key: str, value: str) -> None:
    validate_template_value(key, value)
    async with connection() as database:
        await database.execute(
            """
            INSERT INTO message_templates (template_key, template_value, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(template_key) DO UPDATE SET
                template_value = excluded.template_value,
                updated_at = CURRENT_TIMESTAMP
            """,
            (key, value),
        )
        await database.commit()


async def reset_message_template(key: str) -> str:
    definition = get_template(key)
    async with connection() as database:
        await database.execute(
            """
            INSERT INTO message_templates (template_key, template_value, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(template_key) DO UPDATE SET
                template_value = excluded.template_value,
                updated_at = CURRENT_TIMESTAMP
            """,
            (key, definition.default),
        )
        await database.commit()
    return definition.default
