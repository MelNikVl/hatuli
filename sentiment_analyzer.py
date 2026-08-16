#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""sentiment_analyzer.py — LLM-анализ отзывов застройщиков (DeepSeek).

Определяет для каждого отзыва:
  * общий sentiment (positive/negative/neutral; 'spam' — не отправляется в LLM)
  * aspect-based оценки: качество_строительства, сроки, отделка,
    инфраструктура, управление, цена — каждый 1..5 + краткий комментарий
    (оцениваются ТОЛЬКО аспекты, реально затронутые в тексте; остальные None)
  * claims — конкретные претензии (строки из текста)
  * praises — конкретные похвалы (строки из текста)

Работает батчами (N отзывов за один вызов DeepSeek, response_format=json_object).
Сеть только в `_llm_call` — тесты подменяют её monkeypatch'ем, без реальных API.

Запуск (CLI, для разовой проверки):
    cd ~/krisha_bot && venv/bin/python sentiment_analyzer.py --text "отзыв..."

Конфиг — DEEPSEEK_API_KEY из os.environ (задача 2026-08-16, "P0 —
Integrity" — тот же фикс, что 2gis_reviews_collect.py: раньше жёстко
зашитый абсолютный путь /home/nik/krisha_bot/.env падал
FileNotFoundError на любой машине без него, включая CI).
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ASPECTS = [
    'качество_строительства',
    'сроки',
    'отделка',
    'инфраструктура',
    'управление',
    'цена',
]

SENTIMENTS = ('positive', 'negative', 'neutral')

_SPAM_HINTS = re.compile(
    r'продам|куплю|сдам|сниму|групп[уы]|whatsapp|wa\.me|8700\d{6}|877\d{7}'
    r'|срочно|торг уместен|коммерческ|звоните|добавьте|риелтор', re.IGNORECASE)


def is_spam(text: str) -> bool:
    """Регекс-фильтр спама (тот же, что в 2gis_reviews_collect.py)."""
    return bool(_SPAM_HINTS.search(text or ''))


def load_api_key() -> str:
    return os.getenv('DEEPSEEK_API_KEY', '')


def _llm_call(url: str, data: bytes, headers: dict):
    """Инжектируемая точка сети — тесты подменяют эту функцию."""
    req = urllib.request.Request(url, data=data, headers=headers, method='POST')
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode('utf-8'))


def _build_prompt(reviews: list[dict]) -> str:
    lines = []
    for i, r in enumerate(reviews, 1):
        ctx = ''
        if r.get('author'):
            ctx = f" (автор: {r['author']})"
        lines.append(f'## Отзыв {i}{ctx}\n{r.get("text", "")}')
    return '\n\n'.join(lines)


def _system_prompt() -> str:
    aspects = ', '.join(ASPECTS)
    return (
        'Ты — аналитик отзывов о жилых комплексах Астаны. Для КАЖДОГО отзыва в батче верни JSON-объект, '
        'где ключ — номер отзыва (строка), значение — объект вида:\n'
        '{"sentiment": "positive|negative|neutral", '
        '"aspects": {"<аспект>": {"score": 1-5, "comment": "кратко"}}, '
        '"claims": ["претензия 1", ...], "praises": ["похвала 1", ...]}\n'
        'Правила:\n'
        f'- аспекты — только из списка: {aspects};\n'
        '- оценивай ТОЛЬКО аспекты, которые реально затронуты в тексте (score 1..5);\n'
        '- claims/praises — конкретные факты из текста (по 1-4 штуки), короткие формулировки;\n'
        '- если отзыв нейтральный/рекламный — claims и praises пустые.\n'
        'Ответ — строго JSON, без markdown.'
    )


def analyze_reviews(reviews: list[dict], api_key: str = None, _call=None) -> list[dict]:
    """Батч-анализ отзывов. `reviews` — список {"text": str, "author": str|None}.

    Возвращает список словарей с ключами: sentiment, aspects, claims, praises.
    Спам-отзывы не отправляются в LLM (sentiment='spam'). При сбое LLM —
    консервативный fallback (neutral, пустые аспекты) без исключений.
    """
    call = _call or _llm_call
    api_key = api_key if api_key is not None else load_api_key()
    out = []
    todo = []
    for r in reviews:
        text = (r.get('text') or '').strip()
        if not text:
            out.append({'sentiment': 'neutral', 'aspects': {}, 'claims': [], 'praises': []})
            continue
        if is_spam(text):
            out.append({'sentiment': 'spam', 'aspects': {}, 'claims': [], 'praises': []})
            continue
        todo.append(r)
        out.append(None)

    if todo and api_key:
        body = {
            'model': 'deepseek-chat',
            'temperature': 0,
            'messages': [
                {'role': 'system', 'content': _system_prompt()},
                {'role': 'user', 'content': _build_prompt(todo)},
            ],
            'response_format': {'type': 'json_object'},
        }
        try:
            resp = call(
                'https://api.deepseek.com/chat/completions',
                json.dumps(body).encode('utf-8'),
                {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + api_key},
            )
            content = resp['choices'][0]['message']['content']
            parsed = json.loads(content) if isinstance(content, str) else content
        except Exception:
            parsed = {}

        j = 0
        for i, item in enumerate(out):
            if item is not None:
                continue
            res = parsed.get(str(j + 1)) if isinstance(parsed, dict) else None
            j += 1
            if not isinstance(res, dict):
                out[i] = {'sentiment': 'neutral', 'aspects': {}, 'claims': [], 'praises': []}
                continue
            sent = res.get('sentiment')
            if sent not in SENTIMENTS:
                sent = 'neutral'
            aspects = {}
            raw_aspects = res.get('aspects') or {}
            for a, v in raw_aspects.items():
                if a not in ASPECTS or not isinstance(v, dict):
                    continue
                try:
                    score = int(v.get('score'))
                except (TypeError, ValueError):
                    score = 0
                if not 1 <= score <= 5:
                    continue
                aspects[a] = {'score': score, 'comment': str(v.get('comment', ''))[:200]}
            out[i] = {
                'sentiment': sent,
                'aspects': aspects,
                'claims': [str(c)[:300] for c in (res.get('claims') or [])][:4],
                'praises': [str(p)[:300] for p in (res.get('praises') or [])][:4],
            }
    # fallback для необработанных (нет ключа)
    for i, item in enumerate(out):
        if item is None:
            out[i] = {'sentiment': 'neutral', 'aspects': {}, 'claims': [], 'praises': []}
    return out


def analyze_review(text: str, api_key: str = None) -> dict:
    """Одиночный отзыв."""
    return analyze_reviews([{'text': text}], api_key)[0]


def main():
    ap = argparse.ArgumentParser(description='LLM-анализ отзыва (DeepSeek)')
    ap.add_argument('--text', help='текст отзыва')
    ap.add_argument('--file', help='файл с отзывами (по одному на строку)')
    args = ap.parse_args()
    if args.text:
        print(json.dumps(analyze_review(args.text), ensure_ascii=False, indent=2))
    elif args.file:
        texts = [l.strip() for l in Path(args.file).read_text(encoding='utf-8').splitlines() if l.strip()]
        print(json.dumps(analyze_reviews([{'text': t} for t in texts]), ensure_ascii=False, indent=2))
    else:
        ap.error('нужен --text или --file')
    return 0


if __name__ == '__main__':
    sys.exit(main())
