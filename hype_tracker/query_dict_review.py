#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Еженедельное ИИ-ревью словаря новостных запросов (суббота 11:00).

Собирает статистику news_query_stats за 7 дней, отдаёт DeepSeek список
запросов с метриками, LLM предлагает: убрать неактуальные, добавить новые
(из заголовков новостей, где упоминаются ЖК), переформулировать. Применяет
изменения в hype_tracker/news_collect.py автоматически, печатает отчёт.
"""
import json
import os
import re
import subprocess
import sys
from datetime import datetime

sys.path.insert(0, '/home/nik/krisha_bot')
BASE = '/home/nik/krisha_bot'

DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
MODEL = "deepseek-chat"
NEWS_COLLECT = f"{BASE}/hype_tracker/news_collect.py"

SYSTEM_PROMPT = """Ты — аналитик словаря поисковых запросов для агрегатора новостей о недвижимости Астаны. Каждую неделю ты получаешь:
1) статистику по существующим запросам (сколько новостей нашли, сколько дублей, ошибок),
2) список ЖК, упомянутых в свежих новостях, которых нет в словаре.

Верни ТОЛЬКО JSON:
{"remove": ["запрос1", ...], "add": ["запрос1", ...], "reason": "краткое пояснение на русском, 1-2 предложения"}

Правила:
- remove: запросы с 0 новостей за 7 дней И ошибками/только дублями; устаревшие формулировки. НЕ удаляй поимённые запросы ЖК, если ЖК ещё продаётся.
- add: новые ЖК из списка (формат «Название ЖК Астана»), актуальные темы. Не больше 15 за раз.
- Не выдумывай: добавляй только то, что реально в списке упомянутых или очевидно актуально."""

# ── извлечение кандидатов: только «ЖК X» с заглавной буквы, ≤3 слова ──
MENTION_RE = re.compile(
    r'ЖК\s+([А-ЯЁA-Z][А-ЯЁа-яёa-zA-Z0-9\-]{1,30}?)'
    r'(?=[\s,.;:»«)\u2014]|$)')
STOP_WORDS = re.compile(
    r'^(Акимат|Астана|Астане|Астаны|Алматы|Алмата|Жители|Жильцы|Дольщики|'
    r'Пожарные|Полиция|Врачи|Спасатели|Комиссия|Прокуратура|Суд|Семья|'
    r'Мужчина|Женщина|Дети|Ребенок|Подросток|Власти|Аким|Департамент|'
    r'Министерство|Премьер|Президент|Мажилис|Сенат|Комитет|Управление|'
    r'Застройщик|Застройщики|Новостройк|Квартир|Недвижимость|Ипотека|Цены|'
    r'Рынок|Район|Улица|Проспект|Пересечение|Дом|Дома|Подъезд|Лифт|Кровля|'
    r'Фасад|Паркинг|Парковка|Двор|Школа|Садик|Магазин|Парк|Сквер|Мост|Дорога|'
    r'Остановка|Аэропорт|Вокзал|ЖК|Жилой|Первый|Второй|Третий|Новый|Новые|'
    r'Город|Города|Микрорайон|Квартал|Комплекс|Объект|Территория|Площадка|'
    r'Строительство|Стройка|Ремонт|Программа|Льгот|Кредит|Субсид|'
    r'Старт|Продаж|Сдача|Долгострой|Проблемн|Элитн|Более|Вокруг|Скандал|'
    r'Инцидент|Затопл|Пожар|Снос|Достройк|Рейтинг|Задержк|Облицовк|'
    r'Библиотек|Шлагбаум|Оружие|Ящик|ДТП|Авария|Взрыв|Обрушение|Рухнул|'
    r'Упал|Горел|Сгорел|Эвакуация|Задержан|Пострадал|Погиб|Москвы|'
    r'Нур-Султана|Без|Вместо|Возле|Два|За|На|Наиболее|Началось|Недовольны|'
    r'Объявила|Остались|Перевели|Получили|Внесли|Бывшей|Около|'
    r'После|Перед|Между|Через|Один|Одна|Одни|Однажды|Пока|Ещё|Уже|'
    r'Только|Даже|Разве|Вот|Тут|Здесь|Теперь|Сейчас|Сегодня|Вчера|'
    r'Завтра|Впервые|Окончательно|Частично|Полностью|Якобы|Вроде|'
    r'Почему|Как|Где|Куда|Когда|Что|Чем|Кто)\b', re.I)
BAD_INNER = re.compile(r'\b(астан|алмат)\w*\b', re.I)


def load_env_key(key: str) -> str | None:
    v = os.getenv(key)
    if v:
        return v
    try:
        with open(f"{BASE}/.env", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith(f"{key}="):
                    return line.split("=", 1)[1].strip()
    except Exception:
        pass
    return None


def psql(sql: str) -> str:
    r = subprocess.run(['sudo', '-u', 'postgres', 'psql', '-d', 'krisha_bot', '-t', '-A', '-c', sql],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr[:300])
    return r.stdout.strip()


def call_deepseek(api_key: str, user_text: str) -> dict | None:
    import requests
    try:
        resp = requests.post(
            DEEPSEEK_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            json={"model": MODEL, "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_text},
            ], "temperature": 0.2, "max_tokens": 3000},
            timeout=90,
        )
    except Exception as e:
        print(f"[WARN] deepseek запрос упал: {e}", flush=True)
        return None
    if resp.status_code != 200:
        print(f"[WARN] deepseek {resp.status_code}: {resp.text[:200]}", flush=True)
        return None
    content = resp.json()["choices"][0]["message"]["content"].strip()
    if content.startswith("```json"):
        content = content[len("```json"):]
    elif content.startswith("```"):
        content = content[len("```"):]
    if content.endswith("```"):
        content = content[:-3]
    content = content.strip()
    try:
        return json.loads(content)
    except Exception as e:
        print(f"[WARN] не распарсил JSON: {e} -- {content[:300]}", flush=True)
        return None


def extract_mentions() -> list[str]:
    """ЖК из свежих заголовков: «ЖК X» с заглавной, ≤3 слов, без стоп-слов."""
    raw = psql("""
        SELECT title FROM news
        WHERE ts > now() - interval '7 days'
          AND title ~* '(жк|жилой комплекс)'
        LIMIT 200
    """)
    mentions = set()
    for t in raw.splitlines():
        t = t.strip()
        for m in MENTION_RE.finditer(t):
            cand = (m.group(1) or '').strip().strip('"').strip()
            if not cand or len(cand.split()) > 3:
                continue
            if cand[0].isdigit() or STOP_WORDS.search(cand) or BAD_INNER.search(cand):
                continue
            mentions.add(cand[:40])
    return sorted(mentions)[:25]


def apply_changes(remove: list[str], add: list[str]) -> tuple[int, int]:
    """Удалить запросы (целиком строки), добавить новые в COMPLEX_NAMES."""
    with open(NEWS_COLLECT, encoding='utf-8') as f:
        src = f.read()
    removed, added = 0, 0

    lines = src.split('\n')
    out = []
    for ln in lines:
        skip = False
        for q in remove:
            if q in ln and ln.lstrip().startswith('"'):
                cleaned = ln.lstrip().strip().rstrip(',')
                cleaned = cleaned.replace(f'"{q}"', '').replace(f'"{q}",', '')
                cleaned = cleaned.strip().strip(',').strip()
                if not cleaned:
                    skip = True
                    removed += 1
                    break
        if not skip:
            out.append(ln)
    src = '\n'.join(out)
    # почистить висящие запятые в начале строк
    lines = src.split('\n')
    out = []
    for ln in lines:
        stripped = ln.lstrip()
        if stripped.startswith(','):
            rest = stripped.lstrip(', ').strip()
            if rest:
                out.append(ln[:len(ln) - len(ln.lstrip())] + rest)
            continue
        out.append(ln)
    src = '\n'.join(out)

    if add:
        m = re.search(r'(COMPLEX_NAMES = \[.*?)(\n\])', src, re.S)
        if m:
            inner = m.group(1)
            additions = '\n'.join(f'    {json.dumps(a, ensure_ascii=False)},' for a in add)
            src = src.replace(inner, inner.rstrip() + '\n' + additions, 1)
            added = len(add)

    with open(NEWS_COLLECT, 'w', encoding='utf-8') as f:
        f.write(src)
    return removed, added


def main():
    api_key = load_env_key("DEEPSEEK_API_KEY") or load_env_key("OPENAI_API_KEY")
    if not api_key:
        print("❌ Нет DEEPSEEK_API_KEY в .env — ревью пропущено")
        return

    stats = psql("""
        SELECT query,
               SUM(total) AS total, SUM(new_items) AS new_items,
               SUM(duplicates) AS dups, SUM(blocked) AS blocked, SUM(errors) AS errs,
               COUNT(*) AS runs
        FROM news_query_stats
        WHERE run_at > now() - interval '7 days'
        GROUP BY query ORDER BY new_items DESC
    """)
    stat_lines = []
    for l in stats.splitlines():
        if not l:
            continue
        p = l.split('|')
        if len(p) >= 7:
            stat_lines.append(
                f"{p[0][:70]} | всего={p[1]} нов={p[2]} дуб={p[3]} блок={p[4]} ош={p[5]} запусков={p[6]}")

    mention_list = extract_mentions()

    current = psql("""
        SELECT DISTINCT query FROM news_query_stats WHERE run_at > now() - interval '14 days'
        ORDER BY 1
    """)
    current_list = [q for q in current.splitlines() if q.strip()]
    current_lower = {q.lower() for q in current_list}
    new_mentions = [m for m in mention_list if m.lower() not in current_lower][:25]

    user_text = f"""Статистика запросов за 7 дней:
{chr(10).join(stat_lines[:120]) if stat_lines else '(нет данных)'}

ЖК, упомянутые в свежих новостях, которых нет в словаре (кандидаты на добавление):
{chr(10).join(new_mentions) if new_mentions else '(нет)'}

Текущих запросов в словаре: {len(current_list)}."""

    print("🤖 ИИ-ревью словаря новостных запросов…", flush=True)
    res = call_deepseek(api_key, user_text)
    if not res:
        print("❌ DeepSeek не ответил — изменения не применены")
        return

    remove = [r for r in res.get("remove", []) if r.strip()]
    add = [a for a in res.get("add", []) if a.strip()]
    reason = res.get("reason", "")

    removed, added = 0, 0
    if remove or add:
        removed, added = apply_changes(remove, add)
        print("✅ Изменения применены к news_collect.py", flush=True)
    else:
        print("ℹ️ Изменений не требуется", flush=True)

    print("=" * 50)
    print(f"📰 ИИ-ревью словаря новостей — {datetime.now().strftime('%d.%m.%Y')}")
    print(f"Запросов в прогонах: {len(current_list)} · новых упоминаний: {len(new_mentions)}")
    if remove:
        print(f"\n🗑 Убрано ({removed}):")
        for q in remove[:15]:
            print(f"  • {q}")
    if add:
        print(f"\n➕ Добавлено ({added}):")
        for q in add[:15]:
            print(f"  • {q}")
    if reason:
        print(f"\n💬 Пояснение: {reason[:400]}")
    print("=" * 50)


if __name__ == "__main__":
    main()
