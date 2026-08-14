"""Регрессия для bot/core/newbuild_person_offers.py — задача 2026-08-14,
"двойное размещение предложений людей в новостройках": тег переуступка/
вторичка (classify_person_offer) и дельта к цене застройщика (build_
developer_price_index/developer_price_for_listing/price_delta_pct).
Чистая логика, без БД — юнит-тесты, не интеграционные."""
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from bot.core.newbuild_person_offers import (
    classify_person_offer,
    build_developer_price_index,
    developer_price_for_listing,
    price_delta_pct,
)


def test_classify_text_signal_wins_over_date():
    # Срок сдачи уже прошёл (дом сдан), но текст прямо говорит "переуступка" —
    # текстовый сигнал сильнее и должен победить дату.
    tag, reason, signal = classify_person_offer(
        "Переуступка прав требования, 2-комн",
        "Продаю по переуступке, ДДУ на руках",
        datetime(2026, 6, 1, tzinfo=timezone.utc),
        completion_year=2024, completion_quarter=1,
    )
    assert tag == "assignment"
    assert signal == "text"
    assert "переуступка" in reason or "цесси" in reason


def test_classify_cessia_keyword_detected():
    tag, reason, signal = classify_person_offer(
        "Продажа по цессии", None,
        datetime(2026, 1, 1, tzinfo=timezone.utc), None, None,
    )
    assert tag == "assignment"
    assert signal == "text"


def test_classify_by_date_before_completion_is_assignment():
    # Объявление появилось до срока сдачи -> дом ещё не сдан -> переуступка,
    # даже без текстового сигнала — но сигнал СЛАБЫЙ (только дата).
    tag, reason, signal = classify_person_offer(
        "Продам 2-комнатную квартиру", "хорошая планировка",
        datetime(2026, 3, 1, tzinfo=timezone.utc),
        completion_year=2026, completion_quarter=4,
    )
    assert tag == "assignment"
    assert signal == "date"
    assert "2026" in reason


def test_classify_by_date_after_completion_is_resale():
    tag, reason, signal = classify_person_offer(
        "Продам 2-комнатную квартиру", "хорошая планировка",
        datetime(2026, 6, 1, tzinfo=timezone.utc),
        completion_year=2026, completion_quarter=1,
    )
    assert tag == "resale"
    assert signal == "date"


def test_classify_no_signal_returns_none():
    tag, reason, signal = classify_person_offer(
        "Продам квартиру", None,
        datetime(2026, 1, 1, tzinfo=timezone.utc), None, None,
    )
    assert tag is None
    assert signal is None
    assert reason  # честное объяснение, не пустая строка


def test_classify_missing_quarter_defaults_to_q2():
    # completion_quarter не указан -> середина года (нейтральная оценка),
    # не должно падать.
    tag, reason, signal = classify_person_offer(
        "Продам", None, datetime(2026, 8, 1, tzinfo=timezone.utc),
        completion_year=2026, completion_quarter=None,
    )
    assert tag == "resale"  # после 1 июля (Q2 default), дом уже "сдан"
    assert signal == "date"
