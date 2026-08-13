"""Регрессия decide_pair() — фаза 2 (юниты), гейт п.Б, 2026-08-13
(см. docs/entity_resolution_plan.md). Реальные пары из живого гейта
(--limit 15, 2026-08-13): 2 auto (floor_area_price), 9 reject
(unit_number mismatch), 73 review (69 ambiguous_floorplan негативами
зеркального капа + 4 no_confirmation). unit_number-auto — конструиро-
ванный пример (в реальных 50 Крыша-объявлениях этого гейта номер
квартиры распознался только у 2, и ни разу не совпал ни с одним
застройщиковым — см. докстринг модуля phase2_unit_match.py, покрытие
4%), но механизм самого сигнала должен быть проверен."""
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from phase2_unit_match import decide_pair


def _nb(id_, source="svoydom", floor=None, area=None, price=None, raw_json=None,
        source_unit_id=None, first_seen_at=None, last_seen_at=None):
    return {"id": id_, "source": source, "floor": floor, "area": area, "price": price,
            "raw_json": raw_json, "source_unit_id": source_unit_id,
            "first_seen_at": first_seen_at, "last_seen_at": last_seen_at}


def _al(id_, floor=None, area=None, price=None, description=None, title=None,
        first_seen=None, last_seen=None):
    return {"id": id_, "floor": floor, "area": area, "price": price,
            "description": description, "title": title,
            "first_seen": first_seen, "last_seen": last_seen}


# ── auto: реальная пара гейта (nb#36818 svoydom <-> al#1007595223) ────

def test_real_auto_pair_floor_area_price():
    nb = _nb(36818, floor=7, area=69.5, price=38781000,
             source_unit_id="sv-x-1-27-69.5")
    al = _al(1007595223, floor=7, area=69.62, price=39500000,
             title="2-комнатная квартира · 69.62 м² · 7/9 этаж")
    decision, reason, ev = decide_pair(nb, al, [nb], [al])
    assert decision == "auto", (decision, reason, ev)
    assert ev["method"] == "floor_area_price"
    assert ev["price_ok"] is True


def test_real_auto_pair_2():
    nb = _nb(36618, floor=4, area=40.2, price=22626970, source_unit_id="sv-x-1-241-40.2")
    al = _al(1010418532, floor=4, area=39.31, price=23300000,
             title="1-комнатная квартира · 39.31 м² · 4/10 этаж")
    decision, reason, ev = decide_pair(nb, al, [nb], [al])
    assert decision == "auto", (decision, reason, ev)


# ── unit_number: конструированный (механизм проверен, реальной пары
#    сейчас нет — покрытие номера на Крыша-стороне 4%, см. модуль) ────

def test_unit_number_equal_auto_overrides_everything():
    """Номер совпал — auto, ДАЖЕ если этаж/метраж расходятся сильно
    (номер — сильнейший сигнал, перебивает остальное по правилу)."""
    nb = _nb(1, floor=5, area=50.0, price=20000000, raw_json='{"number": "82"}', source="sensata")
    al = _al(2, floor=9, area=90.0, price=99000000, description="Продаю квартиру №82, хороший вид")
    decision, reason, ev = decide_pair(nb, al, [nb], [al])
    assert decision == "auto"
    assert ev["method"] == "unit_number"


# ── reject: реальная пара (nb#12382 номер 107 vs al#1014362892 номер 119) ──

def test_real_number_mismatch_rejects_not_review():
    nb = _nb(12382, floor=3, area=70.0, price=30000000, raw_json='{"number": "107"}', source="sensata")
    al = _al(1014362892, floor=3, area=70.0, price=30500000, description="Продаю квартиру №119")
    decision, reason, ev = decide_pair(nb, al, [nb], [al])
    assert decision == "skip", (decision, reason, ev)  # не review, не auto — прямое противоречие


# ── review, ambiguous_floorplan: реальная пара (nb#35066, mirror_count_nb=2) ──

def test_real_ambiguous_floorplan_never_auto_even_with_price_match():
    nb1 = _nb(35066, floor=6, area=35.62, price=24043500, source_unit_id="sv-x-1-81-35.62")
    nb2 = _nb(35067, floor=6, area=35.7, price=24100000, source_unit_id="sv-x-2-81-35.7")  # тот же этаж/метраж, другая секция
    al = _al(1011888333, floor=6, area=36.0, price=20700000)  # price НЕ совпадает (20.7 vs 24.0) — но проверяем именно кап
    al_match_price = _al(1011888334, floor=6, area=35.62, price=24043500)  # искусственно точная цена — кап всё равно должен победить
    decision, reason, ev = decide_pair(nb1, al_match_price, [nb1, nb2], [al_match_price])
    assert decision == "review" and reason == "ambiguous_floorplan", (decision, reason, ev)
    assert ev["mirror_count_nb"] == 2


# ── review, no_confirmation: реальная пара (nb#33480 <-> al#1014488190) ──

def test_real_no_confirmation_goes_to_review_not_auto():
    nb = _nb(33480, floor=7, area=54.4, price=34600000, raw_json='{"number": "50"}', source="sensata")
    al = _al(1014488190, floor=7, area=54.4, price=32868480)  # цена расходится >5%, номера на al-стороне нет
    decision, reason, ev = decide_pair(nb, al, [nb], [al])
    assert decision == "review" and reason == "no_confirmation", (decision, reason, ev)


def test_price_within_5pct_but_not_exact_still_auto():
    """Разница в цене 1.82% (реальная пара nb#36818/al#1007595223) —
    внутри допуска, должно пройти, не только идеальное совпадение."""
    nb = _nb(1, floor=1, area=50.0, price=38781000, source_unit_id="sv-x-1-1-50.0")
    al = _al(2, floor=1, area=50.0, price=39500000)
    decision, reason, ev = decide_pair(nb, al, [nb], [al])
    assert decision == "auto"
    assert ev["price_ok"] is True


def test_date_overlap_alone_sufficient_without_price():
    now = datetime(2026, 6, 1, tzinfo=timezone.utc)
    later = datetime(2026, 7, 1, tzinfo=timezone.utc)
    nb = _nb(1, floor=2, area=60.0, price=None, source_unit_id="sv-x-1-1-60.0",
             first_seen_at=now, last_seen_at=later)
    al = _al(2, floor=2, area=60.0, price=None, first_seen=now, last_seen=later)
    decision, reason, ev = decide_pair(nb, al, [nb], [al])
    assert decision == "auto"
    assert ev["method"] == "floor_area_dates"


def test_neither_floor_nor_area_match_is_skip_not_review():
    nb = _nb(1, floor=5, area=50.0, price=20000000, source_unit_id="sv-x-1-1-50.0")
    al = _al(2, floor=8, area=90.0, price=20000000)
    decision, reason, ev = decide_pair(nb, al, [nb], [al])
    assert decision == "skip"
