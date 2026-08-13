"""Регрессия _extract_krisha_name() в rescore_review_queue.py (задача
"очередь кандидатов" п.А, 2026-08-13). Живой баг: <title> добавляет
город + маркетинговый хвост ("ЖК Alatau Eco Park Астана: 🏘️ цены,
планировки | BR Building - Крыша") — этого достаточно, чтобы pg_trgm
similarity упал ниже FUZZY_NAME_THRESHOLD и рескор дал бы ложный
no_match/removed на РЕАЛЬНО совпадающих ЖК (53/79 при первой версии
скрипта). JSON-LD "name" — чище, первый гейт (--dry) это подтвердил
(53 removed -> 0 removed после фикса)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from rescore_review_queue import _extract_krisha_name


def test_prefers_jsonld_over_title():
    html = (
        '<title>ЖК Alatau Eco Park Астана: \U0001f3d8️ цены, планировки | BR Building - Крыша</title>'
        '<script type="application/ld+json">{"name": "ЖК Alatau Eco Park"}</script>'
    )
    assert _extract_krisha_name(html) == "ЖК Alatau Eco Park"


def test_falls_back_to_title_without_jsonld():
    html = '<title>ЖК Salt Астана: цены, планировки | Aiva Group - Крыша</title>'
    assert _extract_krisha_name(html) == "ЖК Salt Астана"


def test_returns_none_without_either():
    assert _extract_krisha_name("<html><body>нет ни title, ни json-ld</body></html>") is None


def test_jsonld_takes_first_occurrence_not_listing_name():
    """Первое "name" в JSON-LD — сам ЖК; дальше по странице идут
    карточки квартир ("2-комнатная квартира...") — берём первое, не
    последнее/любое совпадение."""
    html = (
        '<script type="application/ld+json">{"name": "ЖК Eleven"}</script>'
        '<script type="application/ld+json">{"name":"2-комнатная квартира · 55 м² · 3/9 этаж"}</script>'
    )
    assert _extract_krisha_name(html) == "ЖК Eleven"
