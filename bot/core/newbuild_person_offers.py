"""
"Предложения людей" в новостройках — задача 2026-08-14, продуктовое решение
"двойное размещение предложений людей в новостройках; аналитика считает
listing один раз".

Что это. apartment_listings (индивидуальные объявления с Крыши, реальные
люди) внутри ЖК-новостройки — принципиально другая природа предложения, чем
newbuild_units (шахматка застройщика, official supply). Раньше на странице
ЖК/дома они либо не отличались от вторички вообще, либо не показывались в
разделе "Новостройки" совсем. Теперь: показываем ОБА пула рядом — предложения
людей с бейджем "предложение человека" (не путать с официальным supply
застройщика) — но каждое apartment_listings.id считается РОВНО ОДИН РАЗ в
любой статистике (см. build_developer_price_index/newbuild_map_points —
listing уже привязан к ОДНОМУ complex_id/дому через _listing_id_match,
дважды никуда не попадает).

Два независимых сигнала здесь:

1. Тег «переуступка» (assignment) vs «вторичка» (resale) — см.
   classify_person_offer(). Переуступка — человек продаёт ПРАВО ТРЕБОВАНИЯ на
   квартиру в ещё не сданном доме (де-факто вместо застройщика, обычно с
   наценкой/дисконтом к прайсу застройщика); вторичка внутри "новостройки" —
   дом уже сдан, это обычная перепродажа готовой квартиры, отличается от
   ЖК-вторички вне новостроек только тем, что дом молодой. Сигналы (по
   убыванию надёжности):
     а) текст объявления явно говорит "переуступка"/"цессия" — сильный сигнал,
        перекрывает дату;
     б) срок сдачи ЖК (completion_year/quarter, есть только у is_newbuild)
        позже даты первого появления объявления (first_seen) — тогда дом
        физически ещё не сдан на момент публикации, значит это не может быть
        "вторичка готовой квартиры".
   Без даты и без текстового сигнала — тег НЕ проставляется (честное
   "не определено" вместо угадывания).

2. Дельта к актуальной цене застройщика — см. build_developer_price_index()
   + developer_price_for_listing(). Точная — если для этого apartment_listings
   уже есть unit_source_links (Фаза 2, юнит-мэтчинг, bot/core/entity_resolution
   .approve_unit_candidate) на конкретный newbuild_units.id — берём цену/м²
   именно этого юнита. Агрегатная — иначе медиана price_per_m2 доступных/
   забронированных юнитов того же ЖК и той же комнатности (если комнатность
   объявления известна), иначе по всем комнатностям ЖК разом. Фаза 2 не
   блокер — при отсутствии unit-link просто используется агрегат, секция не
   ждёт полного покрытия юнит-мэтчинга.
"""
from __future__ import annotations

from datetime import date, datetime
from statistics import median

_ASSIGNMENT_KEYWORDS = ("переуступ", "цесси")


def classify_person_offer(
    title: str | None, description: str | None,
    first_seen: datetime | date | None,
    completion_year: int | None, completion_quarter: int | None,
) -> tuple[str | None, str, str | None]:
    """-> (tag, reason, signal). tag: 'assignment' | 'resale' | None
    (не определено). signal: 'text' | 'date' | None — задача 2026-08-14
    ("бейдж: при слабом сигнале — «возможно, переуступка»"): живой отчёт
    по тегу assignment показал 89.9% (464 из 516) слабого сигнала (только
    по дате) против 10.1% сильного (явный текст «переуступка»/«цессия») —
    категоричный бейдж «переуступка» при слабом сигнале вводил в
    заблуждение. UI красит по signal, не по тексту reason (та остаётся
    человекочитаемым tooltip, не машиночитаемым полем)."""
    text = f"{title or ''} {description or ''}".lower()
    if any(kw in text for kw in _ASSIGNMENT_KEYWORDS):
        return "assignment", "в тексте объявления упомянута переуступка/цессия", "text"

    if completion_year and first_seen:
        q = completion_quarter or 2  # квартал не указан — берём середину года, нейтральная оценка
        try:
            completion_approx = date(int(completion_year), (int(q) - 1) * 3 + 1, 1)
        except ValueError:
            return None, "срок сдачи в данных некорректен", None
        fs = first_seen.date() if hasattr(first_seen, "date") else first_seen
        if fs < completion_approx:
            return ("assignment",
                    f"объявление появилось раньше срока сдачи ({completion_year} г., {q} кв.) — дом ещё не сдан",
                    "date")
        return ("resale",
                f"объявление появилось после срока сдачи ({completion_year} г., {q} кв.) — дом уже сдан",
                "date")

    return None, "срок сдачи ЖК неизвестен, признаков переуступки в тексте нет", None


def build_developer_price_index(newbuild_unit_rows: list[dict]) -> dict:
    """rows: [{id, rooms, price, area, price_per_m2}] — ДОСТУПНЫЕ/забронированные
    юниты ОДНОГО ЖК (сколько бы разных people-offers мы ни считали для этого
    ЖК дальше — индекс строится один раз на ЖК, не на каждое объявление).
    -> {"by_unit_id": {unit_id: ppm2}, "by_rooms": {rooms: median_ppm2},
        "overall": median_ppm2 | None}."""
    by_unit: dict[int, float] = {}
    by_rooms: dict[int | None, list[float]] = {}
    overall: list[float] = []
    for u in newbuild_unit_rows:
        ppm2 = u.get("price_per_m2")
        if not ppm2 and u.get("price") and u.get("area"):
            ppm2 = float(u["price"]) / float(u["area"])
        if not ppm2:
            continue
        ppm2 = float(ppm2)
        by_unit[u["id"]] = ppm2
        by_rooms.setdefault(u.get("rooms"), []).append(ppm2)
        overall.append(ppm2)
    return {
        "by_unit_id": by_unit,
        "by_rooms": {k: median(v) for k, v in by_rooms.items() if v},
        "overall": median(overall) if overall else None,
    }


def developer_price_for_listing(
    price_index: dict, listing_rooms: int | None, unit_id: int | None = None,
) -> tuple[float | None, str | None]:
    """-> (price_per_m2 застройщика | None, method: 'unit' | 'rooms' | 'overall' | None).
    'unit' — точная цена конкретного смэтченного юнита (Фаза 2); 'rooms' —
    медиана по той же комнатности в этом ЖК; 'overall' — медиана по всему ЖК
    (комнатность объявления неизвестна или в ЖК нет юнитов той комнатности)."""
    if unit_id is not None and unit_id in price_index["by_unit_id"]:
        return price_index["by_unit_id"][unit_id], "unit"
    if listing_rooms in price_index["by_rooms"]:
        return price_index["by_rooms"][listing_rooms], "rooms"
    if price_index["overall"] is not None:
        return price_index["overall"], "overall"
    return None, None


def price_delta_pct(listing_price: float | None, listing_area: float | None,
                    dev_price_per_m2: float | None) -> float | None:
    """Дельта цены/м² объявления к цене застройщика, в процентах.
    > 0 — объявление ДОРОЖЕ актуального прайса застройщика (наценка
    переуступки/жадный вторичный продавец); < 0 — дешевле (дисконт)."""
    if not listing_price or not listing_area or not dev_price_per_m2:
        return None
    listing_ppm2 = float(listing_price) / float(listing_area)
    return round((listing_ppm2 - dev_price_per_m2) / dev_price_per_m2 * 100, 1)
