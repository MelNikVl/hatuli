"""Регресс-чек для задачи 2026-08-14 ("Bargain — единый источник"): строка
"🤝 Торг" в Telegram-карточке объявления (service_alerts.py._listing_card)
должна появляться, когда bargain_target реально посчитан (после фикса
service_apartments.extract_bargain — см. test_bargain_extract.py — эта
колонка снова заполняется для новых/пересканированных объявлений), и не
появляться, когда его нет (честное отсутствие данных, не пустая строка)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from service_alerts import _listing_card


def _base_listing(**overrides) -> dict:
    r = {
        "title": "2-комнатная квартира", "district": "Есильский р-н",
        "complex_name": "ЖК Тест", "score_total": 82, "yield_pct": 9.5,
        "price": 30_000_000, "url": "https://krisha.kz/a/show/123456789",
    }
    r.update(overrides)
    return r


def test_bargain_line_present_when_target_computed():
    # Ровно то, что теперь оказывается в apartment_listings после
    # прохождения через service_apartments.extract_bargain (объявление
    # младше 3 недель, только что пересканировано).
    r = _base_listing(bargain_target=28_500_000,
                      bargain_rec="цена на уровне рынка, реальный торг 5-8%")
    card = _listing_card(r)
    assert "🤝 Торг" in card
    assert "28" in card  # цель торга где-то в тексте (форматированная сумма)
    assert "реальный торг 5-8%" in card


def test_bargain_line_absent_when_no_target():
    # Честно нет данных (0 аналогов) — bargain_target остаётся NULL,
    # строка "Торг" не должна появляться (не выдумываем цифру).
    r = _base_listing(bargain_target=None, bargain_rec=None)
    card = _listing_card(r)
    assert "🤝 Торг" not in card


def test_bargain_line_survives_missing_rec_text():
    # target есть, а текст рекомендации почему-то пуст — строка всё равно
    # должна появиться (просто без " — <rec>" хвоста).
    r = _base_listing(bargain_target=25_000_000, bargain_rec=None)
    card = _listing_card(r)
    assert "🤝 Торг" in card
