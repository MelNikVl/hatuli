"""
Отдельная модель скоринга ПЕРВИЧКИ (market_type='primary').

Почему отдельная модель, а не поправки к базовой: у первички нет
сегодняшнего yield (аренды ещё нет, дом не заселён) — базовая модель
Hatuli на 20% состоит из yield, который для первички фиктивен. Вместо
этого первичку важно оценивать по риску и потенциалу удорожания:
застройщик, стадия готовности, дисконт к аналогичной вторичке.

Компоненты (100 баллов, до нормировки 80):
  Застройщик (30)      — репутация/масштаб (данные Korter: число проектов)
  Стадия строительства (20) — готово/строится/котлован — риск переноса
  Дисконт к вторичке (30)   — цена/м² первички против медианы вторички
                               того же класса и района (данные Korter/
                               Homsters дают housing_class для сравнения)

Локация (район-веса) убрана по запросу — плоский скоринг «по названию
района» не имеет смысла. Оставшиеся 3 компонента (сумма 80) нормируются
до 100, т.е. просто пропорционально делят освободившиеся баллы.

Данные о застройщике/классе берутся из complexes.source_info (Korter/
Homsters), поэтому качество первички-скора растёт по мере накопления
обогащения ЖК.
"""
from __future__ import annotations

import re
from datetime import datetime

# Крупные застройщики с подтверждённым портфелем (Korter: число проектов
# в продаже). Список расширять по мере поступления данных с Korter/Homsters.
_MAJOR_DEVELOPERS = {
    "bi group", "nak", "bazis-а", "bazis-a", "tumar group", "ulytau group",
    "mabetex group", "esta group", "sat-ns", "highvill kazakhstan",
    "orda invest", "jetisu",
}

_STAGE_DONE = ("сдан", "построено", "сдача завершена", "готово")
_STAGE_EARLY = ("котлован", "старт продаж", "предварит", "ждём старт", "ждем старт")

def _developer_score(developer: str | None) -> tuple[int, str]:
    if not developer:
        return 10, "застройщик не определён — риск не оценить"
    key = developer.strip().lower()
    if key in _MAJOR_DEVELOPERS:
        return 30, f"{developer} — крупный застройщик с портфелем проектов"
    return 18, f"{developer} — застройщик определён, портфель неизвестен"


def _stage_score(year_built: int | None, text: str) -> tuple[int, str]:
    blob = text.lower()
    if any(s in blob for s in _STAGE_DONE):
        return 20, "дом сдан — риска переноса сроков нет"
    if any(s in blob for s in _STAGE_EARLY):
        return 5, "ранняя стадия (котлован/старт продаж) — высокий риск сроков"
    if year_built:
        years_left = year_built - datetime.now().year
        if years_left <= 0:
            return 18, "год сдачи наступил/прошёл"
        if years_left == 1:
            return 14, "сдача через год"
        return 8, f"сдача через {years_left}+ года — типичный риск переноса"
    return 10, "стадия строительства не определена"


def _discount_score(price_m2: float | None, secondary_median_m2: float | None) -> tuple[int, str]:
    if not price_m2 or not secondary_median_m2 or secondary_median_m2 <= 0:
        return 12, "нет данных по вторичке для сравнения — нейтральная оценка"
    delta_pct = (secondary_median_m2 - price_m2) / secondary_median_m2 * 100
    if delta_pct > 40:
        return 15, f"дисконт {delta_pct:.0f}% к вторичке — подозрительно дёшево, проверь риски"
    if delta_pct >= 15:
        return 30, f"дисконт {delta_pct:.0f}% к вторичке — здоровая цена за ожидание"
    if delta_pct >= 5:
        return 20, f"дисконт {delta_pct:.0f}% к вторичке — умеренный"
    if delta_pct >= -5:
        return 10, "цена почти как у готовой вторички — дисконта за ожидание нет"
    return 5, f"дороже вторички на {abs(delta_pct):.0f}% — потенциал уже заложен в цену"


def compute_primary_score(listing: dict, developer: str | None,
                          secondary_median_m2: float | None) -> tuple[int, dict]:
    """Возвращает (score 0-100, details). Локация (район) убрана из модели —
    developer/stage/discount (сумма 80 до нормировки) масштабируются до 100."""
    text = f"{listing.get('title','')} {listing.get('description','')}"
    price_m2 = None
    if listing.get("price") and listing.get("area"):
        price_m2 = listing["price"] / listing["area"]

    dev_s, dev_r = _developer_score(developer)
    stage_s, stage_r = _stage_score(listing.get("year_built"), text)
    disc_s, disc_r = _discount_score(price_m2, secondary_median_m2)

    raw = dev_s + stage_s + disc_s  # из 80
    total = round(raw * 100 / 80)

    details = {
        "developer":  {"score": dev_s,   "max": 30, "reason": dev_r},
        "stage":      {"score": stage_s, "max": 20, "reason": stage_r},
        "discount":   {"score": disc_s,  "max": 30, "reason": disc_r},
    }
    return min(100, total), details
