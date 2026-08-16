#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tests/test_sentiment_analyzer.py — юнит-тесты LLM-анализа отзывов.

LLM замокан: подменяем sentiment_analyzer._llm_call фиксированными ответами,
реальных API-вызовов нет. Запуск: cd ~/krisha_bot && venv/bin/python -m pytest tests/test_sentiment_analyzer.py -v
"""
import json
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import sentiment_analyzer as sa
import developer_reputation as rep


def mock_llm_response(payload: dict, text: str = None):
    """Фабрика _llm_call: возвращает DeepSeek-подобный ответ с заданным content."""
    def _call(url, data, headers):
        assert url == 'https://api.deepseek.com/chat/completions'
        assert 'Authorization' in headers and headers['Authorization'].startswith('Bearer ')
        return {'choices': [{'message': {'content': text or json.dumps(payload, ensure_ascii=False)}}]}
    return _call


# ---------------------------------------------------------------- sentiment

def test_positive_review_aspects():
    """Позитивный отзыв: sentiment positive, аспекты с высокими скорами."""
    llm = mock_llm_response({
        '1': {
            'sentiment': 'positive',
            'aspects': {'качество_строительства': {'score': 5, 'comment': 'кирпич отличный'},
                        'инфраструктура': {'score': 4, 'comment': 'рядом парк'}},
            'claims': [], 'praises': ['Отличное качество кирпича', 'Рядом парк'],
        }
    })
    res = sa.analyze_reviews([{'text': 'Квартира супер, кирпич отличный, рядом парк!'}],
                             api_key='sk-test', _call=llm)[0]
    assert res['sentiment'] == 'positive'
    assert res['aspects']['качество_строительства']['score'] == 5
    assert res['aspects']['инфраструктура']['score'] == 4
    assert 'Рядом парк' in res['praises']
    assert res['claims'] == []


def test_negative_review_claims():
    """Негатив: sentiment negative, претензии извлечены, затронутые аспекты низкие."""
    llm = mock_llm_response({
        '1': {
            'sentiment': 'negative',
            'aspects': {'сроки': {'score': 1, 'comment': 'сдали на 8 месяцев позже'},
                        'отделка': {'score': 2, 'comment': 'кривые стены'}},
            'claims': ['Задержка сдачи 8 месяцев', 'Кривая отделка'],
            'praises': [],
        }
    })
    res = sa.analyze_reviews([{'text': 'Сдали на 8 месяцев позже, отделка кривая.'}],
                             api_key='sk-test', _call=llm)[0]
    assert res['sentiment'] == 'negative'
    assert res['aspects']['сроки']['score'] == 1
    assert any('Задержка' in c for c in res['claims'])


def test_batch_two_reviews_one_call():
    """Батч: 2 отзыва уходят одним вызовом, номера разложены по местам."""
    calls = []

    def llm(url, data, headers):
        calls.append(json.loads(data)['messages'][1]['content'])
        return {'choices': [{'message': {'content': json.dumps({
            '1': {'sentiment': 'positive', 'aspects': {}, 'claims': [], 'praises': ['ok']},
            '2': {'sentiment': 'negative', 'aspects': {'управление': {'score': 2, 'comment': 'УК молчит'}},
                  'claims': ['УК не отвечает'], 'praises': []},
        })}}]}
    res = sa.analyze_reviews(
        [{'text': 'Всё отлично.'}, {'text': 'УК не отвечает на заявки.'}],
        api_key='sk-test', _call=llm)
    assert len(calls) == 1            # один вызов на батч
    assert '## Отзыв 1' in calls[0] and '## Отзыв 2' in calls[0]
    assert res[0]['sentiment'] == 'positive'
    assert res[1]['sentiment'] == 'negative'
    assert res[1]['aspects']['управление']['score'] == 2


def test_spam_not_sent_to_llm():
    """Спам не уходит в LLM — sentiment='spam', _call не вызывается."""
    called = []

    def llm(url, data, headers):
        called.append(1)
        return {'choices': [{'message': {'content': '{}'}}]}

    res = sa.analyze_reviews([{'text': 'Срочно продам 2-комн квартиру, звоните 87001234567'}],
                             api_key='sk-test', _call=llm)[0]
    assert res['sentiment'] == 'spam'
    assert called == []


def test_llm_failure_fallback_neutral():
    """Сбой LLM (исключение) — консервативный fallback без падения."""
    def boom(url, data, headers):
        raise TimeoutError('deepseek timeout')

    res = sa.analyze_reviews([{'text': 'Какие-то мысли о доме.'}],
                             api_key='sk-test', _call=boom)[0]
    assert res['sentiment'] == 'neutral'
    assert res['aspects'] == {}


def test_aspects_whitelist_only():
    """Аспекты вне списка (и невалидные score) отбрасываются."""
    llm = mock_llm_response({
        '1': {
            'sentiment': 'neutral',
            'aspects': {'качество_строительства': {'score': 3, 'comment': 'ок'},
                        'парковка': {'score': 1, 'comment': 'не в списке'},
                        'сроки': {'score': 9, 'comment': 'score вне 1..5'}},
            'claims': [], 'praises': [],
        }
    })
    res = sa.analyze_reviews([{'text': 'Дом ок, парковки нет.'}], api_key='sk-test', _call=llm)[0]
    assert set(res['aspects'].keys()) == {'качество_строительства'}


# ------------------------------------------------------------- reputation

def test_reputation_recency_weighting():
    """Свежий негатив бьёт сильнее старого позитива."""
    old = date.today() - timedelta(days=700)
    recent = date.today() - timedelta(days=10)
    reviews = [
        {'text': 'старый хороший отзыв', 'sentiment': 'positive', 'topics': [], 'date': old, 'source': '2gis'},
        {'text': 'свежая жалоба', 'sentiment': 'negative', 'topics': [], 'date': recent, 'source': '2gis'},
    ]
    s = rep.reputation_score(reviews)
    assert s['negative'] == 1 and s['positive'] == 1
    assert s['score'] < 50.0, 'свежий негатив должен перевешивать старый позитив'


def test_reputation_source_trust():
    """Негатив из доверенного источника (1.0) бьёт сильнее, чем из слабого (0.6)."""
    d = date.today() - timedelta(days=30)
    mk = lambda src: [
        {'text': 'жалоба', 'sentiment': 'negative', 'topics': [], 'date': d, 'source': src},
        {'text': 'похвала', 'sentiment': 'positive', 'topics': [], 'date': d, 'source': '2gis'},
    ]
    a = rep.reputation_score(mk('2gis'))
    b = rep.reputation_score(mk('news'))
    assert a['score'] < b['score']


def test_reputation_no_reviews():
    """Нет отзывов — скор 50 (нейтрально), без падений."""
    s = rep.reputation_score([])
    assert s['score'] == 50.0


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v']))
