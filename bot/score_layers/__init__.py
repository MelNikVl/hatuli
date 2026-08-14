"""
Слои скоринга Hatuli.

Каждый слой — отдельный модуль с функцией:
    async def compute(listing: dict) -> tuple[int, str]
        Возвращает (поправка к скору, человекочитаемая причина).
        Поправка может быть отрицательной (шум) или положительной (школы).

Базовый скор (0-100) — Deal Score v4, bot/core/deal_score.py, и НЕ
трогается. Слои дают ПОПРАВКИ сверху, применяемые ad hoc на выдаче (SQL
ORDER BY/фильтры), не пишутся обратно в score_total: score_total +
zone_bonus + layer_bonus (+ price_drop_bonus, см. deal_score.py). Так
каждый слой можно включать/выключать и отлаживать независимо.

Активные слои:
  noise     — внешний шум: близость магистралей (OSM)          −6..0
  schools   — школы/садики/вузы в пешей доступности (OSM)       0..+5
  transit   — остановки общественного транспорта (OSM)          0..+3
  amenities — магазины/аптеки/кафе, walkability (OSM)           0..+4
  parks     — зелёные зоны в пешей доступности (OSM)            0..+2
  banks     — предложения банков/ипотека (ЗАГЛУШКА, всегда 0)

Penalty-правило: при шуме ≤ −4 бонусы schools и parks режутся вдвое
(сочетаемость факторов — школа у магистрали не равна школе в тихом дворе).

Слои, живущие вне этого реестра (исторически):
  zones    — зоны приоритета, рисуются на карте (zone_bonus)   0..+20
  finish   — отделка (finish_level, bot/core/listing_intel.py) — задача
             2026-08-14 ("finish_level: убрать тихий инкремент"): раньше
             правила score_total напрямую в обход весов модели (мёртвый
             эффект — стиралось следующим apply_deal_scores() в том же
             цикле, см. docs/scoring_audit.md §5.2); теперь — небольшой
             (0.12) субкомпонент quality внутри Deal Score v4, не
             отдельная поправка сверху, как остальные в этом списке.
"""
from __future__ import annotations

import json
import logging

from bot.score_layers import noise, schools, banks, transit, amenities, parks

logger = logging.getLogger(__name__)

# Порядок = порядок в отчёте. (имя, модуль)
LAYERS = [
    ("noise", noise),
    ("schools", schools),
    ("transit", transit),
    ("amenities", amenities),
    ("parks", parks),
    ("banks", banks),
]


async def compute_all_layers(listing: dict) -> tuple[int, dict]:
    """
    Прогоняет объявление через все слои.
    Возвращает (суммарная поправка, {layer: {"adj": int, "reason": str}}).
    Ошибка одного слоя не валит остальные.
    """
    total = 0
    details: dict[str, dict] = {}
    for name, module in LAYERS:
        try:
            adj, reason = await module.compute(listing)
        except Exception as exc:
            logger.warning("layer %s failed for %s: %s", name, listing.get("id"), exc)
            adj, reason = 0, f"ошибка слоя: {exc}"
        details[name] = {"adj": adj, "reason": reason}

    # ── Penalty-правила сочетаемости (мировая практика: факторы не суммируются
    # слепо — школа у шумной магистрали не равна школе в тихом дворе) ──
    noise_adj = details.get("noise", {}).get("adj", 0)
    if noise_adj <= -4:
        for fam in ("schools", "parks"):
            d = details.get(fam)
            if d and d["adj"] > 0:
                cut = d["adj"] - d["adj"] // 2  # режем вдвое (вниз)
                d["adj"] -= cut
                d["reason"] += f" (−{cut}: рядом шумная магистраль)"

    total = sum(d["adj"] for d in details.values())
    return total, details


def details_to_json(details: dict) -> str:
    return json.dumps(details, ensure_ascii=False)
