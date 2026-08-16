#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""2gis_reviews_collect.py — сбор отзывов на ЖК Астаны с 2GIS.

Для каждого ЖК (complexes, не мусорных, с координатами, ещё не обработанных):
  1. Поиск geo_id: https://2gis.kz/astana/search/{имя} (SSR HTML)
  2. Отзывы: https://2gis.kz/astana/geo/{id}/tab/reviews (SSR: топ по доверию)
  3. Спам-фильтр regex -> sentiment='spam'
  4. LLM (DeepSeek) для не-спама: sentiment + topics
Вежливость: ~60 c между ЖК (флаг --fast 15 c для тестов).

Запуск: cd ~/krisha_bot && venv/bin/python 2gis_reviews_collect.py --limit 50 [--fast]
Таблица: developer_reviews (complex_id, source='2gis', source_entity_id=geo_id, ...)

Конфиг — DATABASE_URL/DEEPSEEK_API_KEY читаются из os.environ (задача
2026-08-16, "P0 — Integrity", найдено CI: раньше был жёстко зашитый
абсолютный путь /home/nik/krisha_bot/.env, читаемый вручную построчно —
падало FileNotFoundError на любой машине без этого конкретного пути,
включая GitHub Actions runner). load_dotenv() ниже — тот же паттерн, что
уже используется по всему проекту (2gis_schools_rating_collect.py,
tests/*.py и т.д.): .env — необязательный локальный фолбэк (тихо
no-op'ает, если файла нет — python-dotenv никогда не бросает исключение
на отсутствующий файл), не обязательное условие для запуска."""
import argparse
import json
import os
import random
import re
import sys
import time
import urllib.parse
import urllib.request

import psycopg2
from dotenv import load_dotenv

load_dotenv()

UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'

SPAM_RE = re.compile(
    r'продам|куплю|сдам|сниму|групп[уы]|whatsapp|wa\.me|8700\d{6}|877\d{7}|8\s?7\d{2}\s?\d{3}\s?\d{2}\s?\d{2}'
    r'|срочно|торг уместен|коммерческ|звоните|пишите|писать|добавьте|переехала|помещени|недвижимост|риелтор',
    re.IGNORECASE)

TOPICS = ['задержка_сдачи', 'качество_работ', 'управляющая_компания', 'шум', 'парковка',
          'лифты', 'двор', 'безопасность', 'спам', 'другое']

REVIEW_RE = re.compile(
    r'class="_19h0cqe"[^>]*>([^<]{1,60})</span>.*?class="_oxthv5">(.*?)</a>', re.S)
GEO_RE = re.compile(r'href="/astana/geo/(\d+)"[^>]*>(?:\s*<[^>]+>)*\s*([^<]{2,90})<', re.S)
DATE_RE = re.compile(r'20\d{2}-\d{2}-\d{2}')


def norm(s):
    n = re.sub(r'[\s\.\-_()«»"|]', '', (s or '').lower())
    for p in ('жк', 'кг', 'кд', 'мжк', 'гп'):
        if n.startswith(p) and len(n) > len(p) + 2:
            n = n[len(p):]
            break
    return n


def get(url):
    req = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept-Language': 'ru'})
    with urllib.request.urlopen(req, timeout=40) as r:
        return r.read().decode('utf-8', 'ignore')


def find_geo_id(name):
    """Поиск geo_id по названию ЖК. Возвращает (geo_id, title) или None."""
    variants = [name]
    n0 = re.sub(r'^(жк|кг|кд|мжк)\s+', '', name.strip(), flags=re.I)
    if n0 != name:
        variants.append(n0)
    words = re.split(r'\s+', n0)
    if len(words) > 2:
        variants.append(' '.join(words[:2]))
    variants = list(dict.fromkeys(variants))  # уникальные
    for v in variants:
        try:
            h = get('https://2gis.kz/astana/search/' + urllib.parse.quote(v))
        except Exception:
            continue
        n = norm(name)
        for gid, title in GEO_RE.findall(h):
            t = norm(title.split(',')[0])
            if len(n) < 5:
                continue
            if n in t or (t in n and len(t) >= 0.7 * len(n)):
                return gid, title
    return None


def fetch_reviews(geo_id):
    try:
        h = get('https://2gis.kz/astana/geo/%s/tab/reviews' % geo_id)
    except Exception:
        return []
    pairs = REVIEW_RE.findall(h)
    dates = DATE_RE.findall(h)
    # даты в JSON идут в обратном порядке к SSR-отзывам; если кол-во совпадает — привязываем reverse
    out = []
    for i, (author, text) in enumerate(pairs):
        text = re.sub(r'\\n', ' ', text).strip()
        if not text:
            continue
        date = None
        if len(dates) == len(pairs) and i < len(dates):
            date = dates[len(dates) - 1 - i]
        out.append({'author': author.strip(), 'text': text[:2000], 'date': date})
    return out


def classify_llm(reviews, api_key):
    """DeepSeek batch: sentiment + topics для не-спамовых отзывов."""
    if not reviews:
        return {}
    system = ('Ты анализируешь отзывы о жилых комплексах Астаны. Для каждого отзыва верни JSON-объект '
              '{"sentiment": "positive|negative|neutral", "topics": ["топики"]}, где топики из списка: '
              + ', '.join(TOPICS[:-1]) + '. Отзыв — текст от жильца о качестве/задержке/дворе/УК и т.п.')
    body = {
        'model': 'deepseek-chat', 'temperature': 0,
        'messages': [{'role': 'system', 'content': system},
                     {'role': 'user', 'content': '\n'.join('## %d\n%s' % (i + 1, r['text']) for i, r in enumerate(reviews))}],
        'response_format': {'type': 'json_object'},
    }
    req = urllib.request.Request('https://api.deepseek.com/chat/completions',
                                 data=json.dumps(body).encode(),
                                 headers={'Content-Type': 'application/json',
                                          'Authorization': 'Bearer ' + api_key}, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            d = json.loads(r.read().decode())
        out = json.loads(d['choices'][0]['message']['content'])
        if isinstance(out, dict) and 'sentiment' in out:
            return {0: out}
        # если вернул массив/объект с ключами 1..N
        return {int(k) - 1: v for k, v in out.items() if k.isdigit()} if isinstance(out, dict) else {}
    except Exception:
        return {}


def _process_one_complex(cur, cid, cname, stats, api_key):
    """Вся работа по ОДНОМУ ЖК: поиск geo_id, отзывы, спам-фильтр, LLM,
    вставка строк через cur (без commit — им управляет вызывающий main()).
    Выделено из main() в отдельную функцию (задача 2026-08-15, правка
    транзакций) специально ради `return` вместо `continue`: `continue`
    внутри `try` в main() пропускал бы `time.sleep()`/commit ПОСЛЕ
    try/except (ровно так и произошло в первой версии этой правки — цикл
    гнал запросы к 2GIS без вежливой паузы и без коммита на "geo не
    найден"/"отзывов нет"). return из обычной функции такой проблемы не
    создаёт."""
    geo = find_geo_id(cname)
    if not geo:
        print('  #%d %s — geo НЕ найден' % (cid, cname[:40]))
        return
    gid, gtitle = geo
    stats['geo_found'] += 1
    revs = fetch_reviews(gid)
    print('  #%d %s -> geo %s (%s), отзывов: %d' % (cid, cname[:36], gid, gtitle[:40], len(revs)))
    if not revs:
        # пометить обработанным (пустой) — чтобы не перебирать вечно
        cur.execute("INSERT INTO developer_reviews (complex_id, source, source_entity_id, review_text, sentiment, fetched_at) "
                    "VALUES (%s, '2gis', %s, '', 'neutral', now()) ON CONFLICT DO NOTHING", (cid, gid))
        return
    # спам-фильтр
    for r in revs:
        if SPAM_RE.search(r['text']):
            r['sentiment'] = 'spam'
            stats['spam'] += 1
    # LLM для не-спама
    todo = [r for r in revs if 'sentiment' not in r]
    if todo and api_key:
        res = classify_llm(todo, api_key)
        for i, r in enumerate(todo):
            if i in res and res[i].get('sentiment') in ('positive', 'negative', 'neutral'):
                r['sentiment'] = res[i]['sentiment']
                r['topics'] = [t for t in res[i].get('topics', []) if t in TOPICS]
                stats['llm'] += 1
    for r in revs:
        r.setdefault('sentiment', 'neutral')
        r.setdefault('topics', [])
    # вставка
    for r in revs:
        cur.execute("""
            INSERT INTO developer_reviews
              (developer_id, complex_id, source, source_entity_id, review_text, rating,
               sentiment, topics, review_date, author, verified, source_url, fetched_at)
            VALUES ((SELECT developer_id FROM complexes WHERE id=%s), %s, '2gis', %s, %s, NULL,
                    %s, %s, %s, %s, TRUE, %s, now())
            ON CONFLICT (complex_id, source_entity_id, review_text) DO NOTHING
        """, (cid, cid, gid, r['text'], r['sentiment'], r['topics'],
              r.get('date'), r['author'],
              'https://2gis.kz/astana/geo/%s/tab/reviews' % gid))
        stats['reviews'] += 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--limit', type=int, default=50)
    ap.add_argument('--fast', action='store_true', help='пауза 15с вместо 60с')
    ap.add_argument('--sleep', type=float, default=None)
    args = ap.parse_args()
    sleep_s = args.sleep or (15 if args.fast else 60)

    dsn = os.getenv('DATABASE_URL', 'postgresql://krisha@localhost/krisha_bot')
    api_key = os.getenv('DEEPSEEK_API_KEY', '')

    conn = psycopg2.connect(dsn)
    cur = conn.cursor()
    cur.execute("""
        SELECT c.id, c.name FROM complexes c
        WHERE c.is_garbage = FALSE AND c.lat IS NOT NULL AND c.lon IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM developer_reviews dr WHERE dr.complex_id = c.id)
        ORDER BY c.id LIMIT %s
    """, (args.limit,))
    jk = cur.fetchall()
    # Явный commit здесь же — эта SELECT сама по себе не должна оставаться
    # висеть в открытой транзакции всё то время, пока идёт HTTP-работа
    # первой итерации цикла ниже (см. докстринг про короткие транзакции).
    conn.commit()
    print('ЖК к обработке:', len(jk))

    # ПРАВКА 2026-08-15 (найдено при аудите: test_umbrellas_page.py падал
    # TimeoutError — транзакция от начальной SELECT висела "idle in
    # transaction" 15+ минут, блокируя ALTER TABLE complexes в
    # НЕСВЯЗАННОМ роуте /admin/entity-ids): psycopg2 по умолчанию
    # autocommit=False — каждый cur.execute() без предшествующего commit()
    # продолжает ОДНУ и ТУ ЖЕ транзакцию. Раньше commit() стоял только в
    # двух из трёх веток цикла (revs пустой / revs есть) — ветка "geo НЕ
    # найден" (самая частая на практике — большинство названий ЖК не
    # матчится с поиском 2GIS) НЕ коммитила вовсе. При нескольких подряд
    # промахах транзакция от начальной SELECT росла на sleep_s (45-60с)
    # КАЖДУЮ такую итерацию — минуты простоя с открытой транзакцией,
    # держащей ACCESS SHARE на `complexes`, при живом вежливом sleep между
    # ЖК это гарантированно происходит на каждом прогоне.
    #
    # commit/rollback здесь — СНАРУЖИ вызова _process_one_complex(), не
    # внутри неё: он гарантирован на каждой итерации (одна транзакция =
    # один ЖК, "commit пачками", не на весь прогон) независимо от того,
    # какой веткой (return/исключение/нормальное завершение) закончилась
    # обработка. time.sleep() — тоже снаружи try/except, БЕЗ вложенных
    # `continue` внутри try (та ошибка была в первой версии этой правки:
    # `continue` внутри try пропускал бы и commit, и sleep ПОСЛЕ
    # try/except целиком) — вежливая пауза перед следующим ЖК соблюдается
    # на любом исходе.
    stats = {'geo_found': 0, 'reviews': 0, 'spam': 0, 'llm': 0}
    for cid, cname in jk:
        try:
            _process_one_complex(cur, cid, cname, stats, api_key)
        except Exception as exc:
            conn.rollback()
            print('  #%d %s — ошибка, транзакция отменена: %s' % (cid, cname[:40], exc))
        else:
            conn.commit()
        time.sleep(sleep_s)

    conn.close()
    print('ИТОГ: geo найдено %d, отзывов %d (спам %d, LLM %d)' % (
        stats['geo_found'], stats['reviews'], stats['spam'], stats['llm']))
    return 0


if __name__ == '__main__':
    sys.exit(main())
