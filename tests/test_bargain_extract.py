"""Регрессия для задачи 2026-08-14 ("Bargain — единый источник"): живой баг
нашли на живой БД (bargain_target/discount_pct/rec не писались ни для
одного объявления с first_seen после 2026-07-25, см. docs/scoring_audit.md
§3/§5.2) — service_apartments.py читал r["score_data"]["bargain"], а
bot/core/apartment_parser.py (единственное место, вызывающее get_comparables
()/analyze_bargain() при парсинге) кладёт результат ПЛОСКО прямо на r:
r["bargain_target"]/r["bargain_discount_pct"]/r["bargain_rec"]. score_data
парсер не заполняет вовсе — ключ был протухшим путём от старой
apartment_score_v2. Регрессия: extract_bargain() должна читать плоские
ключи (то, что реально кладёт парсер), а НЕ протухший вложенный путь."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from service_apartments import extract_bargain


def test_extract_bargain_reads_flat_keys_from_parser():
    # Именно так apartment_parser.py кладёт результат analyze_bargain()
    # на объект листинга (bot/core/apartment_parser.py, конец
    # analyze_apartments) — плоско, не под "bargain".
    r = {
        "id": "123", "price": 30_000_000,
        "bargain_target": 28_500_000,
        "bargain_discount_pct": 5.0,
        "bargain_rec": "цена на уровне рынка, реальный торг 5-8%",
        "comparables_cnt": 12,
        "bargain_method": "hex+ring+class",
    }
    bargain = extract_bargain(r)
    assert bargain == {
        "target_price": 28_500_000,
        "discount_pct": 5.0,
        "recommendation": "цена на уровне рынка, реальный торг 5-8%",
        "comparables_cnt": 12,
        "method": "hex+ring+class",
    }


def test_extract_bargain_comparables_cnt_and_method_persisted():
    # Задача 2026-08-14 (Фаза A.5 п.2 вердикт-стратегии): живой баг —
    # apartment_listings.comparables_cnt был 0/47016 заполнен, потому что
    # INSERT/UPDATE в service_apartments.py никогда его не включал, хотя
    # apartment_parser.py считал s["comparables_cnt"] каждый цикл (тот же
    # класс бага, что finish_level до волны 1). extract_bargain() —
    # единственная точка, откуда SQL берёт эти значения.
    r = {"id": "1", "comparables_cnt": 7, "bargain_method": "city_segment"}
    bargain = extract_bargain(r)
    assert bargain["comparables_cnt"] == 7
    assert bargain["method"] == "city_segment"


def test_extract_bargain_comparables_cnt_defaults_to_zero():
    r = {"id": "2"}
    bargain = extract_bargain(r)
    assert bargain["comparables_cnt"] == 0
    assert bargain["method"] is None


def test_extract_bargain_ignores_dead_score_data_path():
    # Регрессия самого бага: живой листинг НЕ имеет score_data.bargain
    # (парсер такой ключ никогда не создаёт) — старый код
    # (sd.get("bargain", {})) на этом всегда возвращал {}, даже когда
    # плоские bargain_* поля были заполнены по-настоящему.
    r = {
        "id": "456",
        "score_data": {"reasons": [], "breakdown": {}},  # ключа "bargain" нет
        "bargain_target": 40_000_000,
        "bargain_discount_pct": 3.0,
        "bargain_rec": "торг минимальный",
    }
    bargain = extract_bargain(r)
    assert bargain["target_price"] == 40_000_000
    assert bargain["discount_pct"] == 3.0
    assert bargain["recommendation"] == "торг минимальный"


def test_extract_bargain_missing_fields_return_none():
    # Листинг без аналогов (get_comparables пуст) — analyze_bargain
    # возвращает target_price=None и т.п.; extract_bargain должна честно
    # пробросить None, не притворяться нулём/пустой строкой.
    r = {"id": "789"}
    bargain = extract_bargain(r)
    assert bargain == {
        "target_price": None, "discount_pct": None, "recommendation": None,
        "comparables_cnt": 0, "method": None,
    }
