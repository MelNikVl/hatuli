#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""developer_reputation.py — агрегация отзывов по застройщикам.

reputation_score (0..100, 50 = нейтрально) — взвешенное среднее тональностей:
  * recency-вес: свежие отзывы весомее (90 дней — 1.0; 1 год — 0.6; старше — 0.3)
  * source_trust: 2gis=1.0, krisha=0.85, google=0.8, akimat=0.9, news=0.6
  * sentiment-вклад: positive=+1, negative=-1, neutral=0
top_issues / top_strengths — самые частые topics[] среди негативных/позитивных
отзывов (fallback: по тексту, если topics пусты).

CLI:  venv/bin/python developer_reputation.py [--top 5] [--json]

Конфиг — DATABASE_URL из os.environ (задача 2026-08-16, "P0 —
Integrity" — тот же фикс, что 2gis_reviews_collect.py: раньше жёстко
зашитый абсолютный путь /home/nik/krisha_bot/.env падал
FileNotFoundError на любой машине без него, включая CI).
"""

import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import date, timedelta

import psycopg2
from dotenv import load_dotenv

load_dotenv()

SOURCE_TRUST = {
    '2gis': 1.0,
    'krisha': 0.85,
    'google': 0.8,
    'akimat': 0.9,
    'news': 0.6,
}
DEFAULT_TRUST = 0.7

SENTIMENT_VALUE = {'positive': 1.0, 'negative': -1.0, 'neutral': 0.0}


def _dsn() -> str:
    return os.getenv('DATABASE_URL', 'postgresql://krisha@localhost/krisha_bot')


def _recency_weight(d: date | None, ref: date | None = None) -> float:
    """Вес свежести отзыва: 90 дней — 1.0, 1 год — 0.6, старше — 0.3."""
    if d is None:
        return 0.7  # дата неизвестна — средний вес
    ref = ref or date.today()
    age = (ref - d).days
    if age < 0:
        return 1.0
    if age <= 90:
        return 1.0
    if age <= 365:
        return 0.6
    return 0.3


def load_reviews(developer_id: int, conn=None) -> list[dict]:
    """Отзывы застройщика из developer_reviews (без спама)."""
    own = conn or psycopg2.connect(_dsn())
    try:
        cur = own.cursor()
        cur.execute("""
            SELECT review_text, sentiment, topics, review_date, source, rating, author
            FROM developer_reviews
            WHERE developer_id = %s AND review_text IS NOT NULL AND review_text != ''
              AND sentiment != 'spam'
            ORDER BY review_date NULLS LAST
        """, (developer_id,))
        rows = cur.fetchall()
    finally:
        if conn is None:
            own.close()
    return [
        {'text': r[0], 'sentiment': r[1], 'topics': r[2] or [], 'date': r[3],
         'source': r[4] or '2gis', 'rating': r[5], 'author': r[6]}
        for r in rows
    ]


def reputation_score(reviews: list[dict], ref: date | None = None) -> dict:
    """Взвешенный скор репутации (0..100) + статистика."""
    total_w = 0.0
    acc = 0.0
    pos = neg = neu = 0
    by_source: Counter = Counter()
    for r in reviews:
        sent = r.get('sentiment')
        if sent not in SENTIMENT_VALUE:
            continue
        w = _recency_weight(r.get('date'), ref) * SOURCE_TRUST.get(r.get('source'), DEFAULT_TRUST)
        total_w += w
        acc += w * SENTIMENT_VALUE[sent]
        if sent == 'positive':
            pos += 1
        elif sent == 'negative':
            neg += 1
        else:
            neu += 1
        by_source[r.get('source', '?')] += 1

    if total_w == 0:
        score = 50.0
    else:
        score = max(0.0, min(100.0, 50.0 + 50.0 * acc / total_w))
    return {
        'score': round(score, 1),
        'positive': pos, 'negative': neg, 'neutral': neu,
        'weighted_sentiment': round(acc / total_w, 3) if total_w else 0.0,
        'by_source': dict(by_source),
    }


def _topic_counts(reviews: list[dict], sentiment: str) -> list[tuple[str, int]]:
    cnt: Counter = Counter()
    for r in reviews:
        if r.get('sentiment') != sentiment:
            continue
        topics = r.get('topics') or []
        if topics:
            for t in topics:
                cnt[t] += 1
        else:
            # fallback: первый осмысленный фрагмент текста как «тема»
            text = (r.get('text') or '').strip()
            if text:
                cnt['отзыв: ' + text[:48]] += 1
    return cnt.most_common(8)


def compute_reputation(developer_id: int, conn=None, limit: int = 5) -> dict:
    """Полная репутация застройщика: скор + top_issues + top_strengths."""
    reviews = load_reviews(developer_id, conn)
    stats = reputation_score(reviews)
    issues = _topic_counts(reviews, 'negative')
    strengths = _topic_counts(reviews, 'positive')
    return {
        'developer_id': developer_id,
        'reviews_count': len(reviews),
        'reputation_score': stats['score'],
        'sentiment': stats,
        'top_issues': [{'topic': t, 'count': c} for t, c in issues[:limit]],
        'top_strengths': [{'topic': t, 'count': c} for t, c in strengths[:limit]],
    }


def ranking(conn=None, min_reviews: int = 3, top: int = 10) -> list[dict]:
    """Рейтинг застройщиков по репутации (только с >= min_reviews отзывами)."""
    own = conn or psycopg2.connect(_dsn())
    cur = own.cursor()
    cur.execute("""
        SELECT developer_id, d.name
        FROM developer_reviews dr
        JOIN developers d ON d.id = dr.developer_id
        WHERE dr.sentiment != 'spam' AND dr.review_text != ''
        GROUP BY developer_id, d.name
        HAVING count(*) >= %s
    """, (min_reviews,))
    devs = cur.fetchall()
    out = []
    for dev_id, name in devs:
        rep = compute_reputation(dev_id, conn=own)
        rep['name'] = name
        out.append(rep)
    if conn is None:
        own.close()
    out.sort(key=lambda r: r['reputation_score'], reverse=True)
    return out[:top]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--top', type=int, default=10)
    ap.add_argument('--json', action='store_true')
    ap.add_argument('--dev', type=int, help='один застройщик по id')
    args = ap.parse_args()
    if args.dev:
        res = compute_reputation(args.dev)
        print(json.dumps(res, ensure_ascii=False, indent=2) if args.json else
              'dev=%d score=%.1f reviews=%d issues=%s strengths=%s' % (
                  args.dev, res['reputation_score'], res['reviews_count'],
                  [i['topic'] for i in res['top_issues']],
                  [s['topic'] for s in res['top_strengths']]))
    else:
        for r in ranking(top=args.top):
            print('%.1f  %-28s %d отз.  — :%s' % (
                r['reputation_score'], (r.get('name') or '?')[:28],
                r['reviews_count'],
                ', '.join(i['topic'][:28] for i in r['top_issues'][:3]) or '-'))
    return 0


if __name__ == '__main__':
    sys.exit(main())
