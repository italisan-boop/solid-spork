"""
Утилиты приложения.
"""
import json
import re
from datetime import datetime, timezone, timedelta

from .logger import setup_logger


def format_local_time(utc_time_str: str) -> str:
    """Конвертирует время из UTC в местное (Москва, UTC+3)"""
    if not utc_time_str:
        return "—"
    try:
        utc_dt = datetime.fromisoformat(utc_time_str).replace(tzinfo=timezone.utc)
        local_dt = utc_dt.astimezone(timezone(timedelta(hours=3)))
        return local_dt.strftime("%d.%m.%Y %H:%M")
    except Exception:
        return utc_time_str[:16]


def parseBookImages(field):
    """Парсинг изображений из поля БД"""
    if not field:
        return []
    if isinstance(field, list):
        return field
    if isinstance(field, str):
        if field.startswith('['):
            try:
                parsed = json.loads(field)
                return parsed if isinstance(parsed, list) else [parsed]
            except Exception:
                pass
        if field.startswith('http'):
            return [field]
        return [field] if field else []
    return []


# Telegram Bot API в режиме parse_mode='HTML' принимает только ограниченный
# набор тегов: b/strong, i/em, u/ins, s/strike/del, code, pre, blockquote,
# a (только http/https/tg://), tg-spoiler, tg-emoji. Всё остальное
# (<!doctype>, <script>, <html>, любой <class=...> кроме tg-spoiler, …)
# приводит к Bad Request: can't parse entities.
#
# 'a' сюда намеренно НЕ включён — он обрабатывается отдельным проходом
# (sanitize_telegram_html) с проверкой href. Так меньше шанс оставить
# «голый» <a>/</a> и сломать парсер Telegram.
_ALLOWED_TAGS = {
    "b", "strong", "i", "em", "u", "ins", "s", "strike", "del",
    "code", "pre", "blockquote", "tg-spoiler", "tg-emoji", "span",
}
_TAG_RE = re.compile(
    r"<\s*(/?)\s*([a-zA-Z][a-zA-Z0-9-]*)\b([^>]*)>", re.DOTALL
)
_HREF_RE = re.compile(r'href\s*=\s*"([^"]*)"', re.IGNORECASE)
_EMOJI_ID_RE = re.compile(r'emoji-id\s*=\s*"([^"]*)"', re.IGNORECASE)
_SAFE_URL_RE = re.compile(r"^(https?://|tg://)", re.IGNORECASE)


def sanitize_telegram_html(text: str) -> str:
    """Подготовить пользовательский текст к отправке через Telegram HTML.

    Удаляет <!doctype>, комментарии, <script>/<style> и любые теги вне
    разрешённого списка — Telegram их не парсит и крашит рассылку.
    Внутренний текст вырезанных тегов сохраняется. У <a href> оставляет
    только http/https/tg ссылки, остальные выкидывает целиком (вместе
    с закрывающим </a>, чтобы не оставлять «голый» тег).

    Проходы:
      1. Убираем структурный мусор (doctype, комментарии, скрипты, стили).
      2. Пары <a href=...>...</a> прячем в плейсхолдеры, валидируя href.
         Это гарантирует, что либо сохранятся ОБА тега пары, либо не
         сохранится ни один — иначе останется висящий </a>.
      3. Оставшиеся «голые» <a> / <a/> удаляем как обычные теги.
      4. Чистим остальные теги через общий фильтр.
      5. Возвращаем плейсхолдеры на место.
    """
    if not text:
        return ""

    # Структурные объявления и комментарии — Telegram их не понимает.
    text = re.sub(r"<!doctype[^>]*>", "", text, flags=re.IGNORECASE)
    text = re.sub(r"<\?xml[^>]*\?>", "", text, flags=re.IGNORECASE)
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)

    # Скрипты/стили вместе с телом — потенциально опасны и Telegram их не любит.
    text = re.sub(
        r"<\s*script\b[^>]*>.*?</\s*script\s*>", "", text, flags=re.DOTALL | re.IGNORECASE
    )
    text = re.sub(
        r"<\s*style\b[^>]*>.*?</\s*style\s*>", "", text, flags=re.DOTALL | re.IGNORECASE
    )

    # Проход 2: пары <a>...</a> → плейсхолдер, чтобы соседние проходы
    # случайно не оторвали закрывающий тег от открывающего.
    pairs: list[str] = []
    sentinel_re = re.compile(
        r"<\s*a\b[^>]*>(.*?)<\s*/\s*a\s*>", flags=re.DOTALL | re.IGNORECASE
    )

    def _stash_pair(m: re.Match) -> str:
        inner = m.group(1)
        href = _HREF_RE.search(m.group(0))
        url = href.group(1) if href else ""
        if _SAFE_URL_RE.match(url):
            pairs.append(f'<a href="{url}">{inner}</a>')
        else:
            pairs.append(inner)
        return f"\x00A{len(pairs) - 1}\x00"

    text = sentinel_re.sub(_stash_pair, text)

    # Проход 3: «голые» <a> / <a/> — Telegram их не примет, удаляем.
    text = re.sub(r"<\s*a\b[^>]*/?\s*>", "", text, flags=re.IGNORECASE)
    # На всякий случай — осиротевшие </a>.
    text = re.sub(r"<\s*/\s*a\s*>", "", text, flags=re.IGNORECASE)

    def _replace_tag(m: re.Match) -> str:
        closing = m.group(1) == "/"
        tag = m.group(2).lower()
        attrs = m.group(3) or ""

        if tag not in _ALLOWED_TAGS:
            return ""

        if tag == "tg-emoji":
            eid = _EMOJI_ID_RE.search(attrs)
            if not eid:
                return ""
            return f'<tg-emoji emoji-id="{eid.group(1)}">'

        # Все прочие атрибуты (class, id, style, onclick, …) отбрасываем —
        # Telegram их не валидирует и в худшем случае режет сообщение.
        return f"</{tag}>" if closing else f"<{tag}>"

    # Проход 4: чистим остальные теги.
    text = _TAG_RE.sub(_replace_tag, text)

    # Проход 5: возвращаем валидные пары ссылок на место.
    def _restore(m: re.Match) -> str:
        return pairs[int(m.group(1))]

    return re.sub(r"\x00A(\d+)\x00", _restore, text)


__all__ = ["setup_logger", "format_local_time", "parseBookImages", "sanitize_telegram_html"]
