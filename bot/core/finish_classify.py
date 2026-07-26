"""
Классификация состояния отделки квартиры: бетон (черновая) / с отделкой
(чистовая, без мебели) / с мебелью — по тексту объявления (title+description).

Это ПЕРВЫЙ, бесплатный шаг (regex-словарь ключевых слов) — покрывает те
объявления, где продавец сам явно описал состояние. Он не заменяет
bot/core/ai_text_analysis.py (DeepSeek): тот модуль уже умеет то же самое
точнее (LLM понимает контекст, а не только точные фразы), но стоит денег
за вызов и по умолчанию выключен (AI_TEXT_ANALYSIS=0). Значения совместимы
специально: ai_text_analysis.finish пишет то же самое множество
{"черновая","предчистовая","чистовая","ремонт","мебель"} — при включении
AI его результат можно писать в то же поле finish_type, точнее замещая
эвристику там, где текста было мало/неоднозначно.

Фотографии не анализируем (пока) — это отдельное решение (vision-модель на
каждое фото, дороже текстового анализа на порядок), см. обсуждение в задаче.

Покрытие: только те объявления, где в тексте есть явный сигнал. Для
остальных finish_type остаётся NULL — честное "неизвестно", а не угадывание.
"""
from __future__ import annotations

import re

_BARE_PATTERNS = [
    r"черновая\s*отделка", r"без\s*отделки", r"предчистов\w*",
    r"черновой\s*вариант", r"голые\s*стены", r"требует\s*ремонта",
    r"под\s*(чистовую\s*)?отделку", r"свободная\s*планировка.*без",
]
_FURNISHED_PATTERNS = [
    r"с\s*мебелью", r"меблирован\w*", r"вся\s*мебель\s*(остаётся|остается|в\s*подарок|остаётс)",
    r"заезжай\s*и\s*живи", r"дизайнерская\s*мебель", r"вместе\s*с\s*мебелью",
    r"техника\s*(вся\s*)?остаётся", r"техника\s*(вся\s*)?остается",
]
_FINISHED_PATTERNS = [
    r"чистовая\s*отделка", r"с\s*отделкой", r"хорош\w+\s*ремонт",
    r"евроремонт", r"качественн\w+\s*ремонт", r"нов\w+\s*ремонт",
    r"косметическ\w+\s*ремонт", r"дизайнерск\w+\s*ремонт", r"свеж\w+\s*ремонт",
]

_BARE_RE = re.compile("|".join(_BARE_PATTERNS), re.IGNORECASE)
_FURNISHED_RE = re.compile("|".join(_FURNISHED_PATTERNS), re.IGNORECASE)
_FINISHED_RE = re.compile("|".join(_FINISHED_PATTERNS), re.IGNORECASE)


def classify_finish(title: str | None, description: str | None) -> str | None:
    """Вернуть 'черновая' | 'с отделкой' | 'с мебелью' | None (нет сигнала).
    Порядок проверки важен: явный "без отделки" перекрывает случайное
    совпадение слова "мебель" где-то в другом месте текста того же объявления
    (например "продаю без мебели, отделка чистовая" — тогда сначала мебель
    исключаем явным сигналом, а не наоборот)."""
    text = f"{title or ''} {description or ''}".strip()
    if not text:
        return None
    if _BARE_RE.search(text):
        return "черновая"
    if _FURNISHED_RE.search(text):
        return "с мебелью"
    if _FINISHED_RE.search(text):
        return "с отделкой"
    return None


async def apply_finish_classification(batch: int = 500) -> int:
    """Проставить finish_type всем активным объявлениям с описанием, у
    которых он ещё не определён. Возвращает число обновлённых."""
    from bot.db.pg import fetch, execute

    await execute("ALTER TABLE apartment_listings ADD COLUMN IF NOT EXISTS finish_type TEXT")
    rows = await fetch("""
        SELECT id, title, description FROM apartment_listings
        WHERE is_active IS NOT FALSE
          AND finish_type IS NULL
          AND (description IS NOT NULL AND length(description) > 20)
        LIMIT $1
    """, batch)

    updated = 0
    for r in rows:
        finish = classify_finish(r["title"], r["description"])
        if finish is None:
            continue
        await execute(
            "UPDATE apartment_listings SET finish_type = $2 WHERE id = $1",
            r["id"], finish)
        updated += 1
    return updated
