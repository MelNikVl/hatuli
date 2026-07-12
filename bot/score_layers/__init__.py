"""
Слои скоринга Hatuli.

Каждый слой — отдельный модуль с функцией:
    async def compute(listing: dict) -> tuple[int, str]
        Возвращает (поправка к скору, человекочитаемая причина).
        Поправка может быть отрицательной (шум) или положительной (школы).

Базовый скор (0-100) считается в apartment_score_v2 и НЕ трогается.
Слои дают ПОПРАВКИ сверху: score_total + zone_bonus + layer_bonus.
Так каждый слой можно включать/выключать и отлаживать независимо.

Активные слои:
  noise    — внешний шум: близость магистралей (OSM)          −6..0
  schools  — школы/садики/вузы в пешей доступности (OSM)       0..+5
  banks    — предложения банков/ипотека (ЗАГЛУШКА, всегда 0)

Слои, живущие вне этого реестра (исторически):
  zones    — зоны приоритета, рисуются на карте (zone_bonus)   0..+20
  finish   — отделка, правит score_total напрямую              −5..+6
"""
from __future__ import annotations

import json
import logging

from bot.score_layers import noise, schools, banks

logger = logging.getLogger(__name__)

# Порядок = порядок в отчёте. (имя, модуль)
LAYERS = [
    ("noise", noise),
    ("schools", schools),
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
        total += adj
        details[name] = {"adj": adj, "reason": reason}
    return total, details


def details_to_json(details: dict) -> str:
    return json.dumps(details, ensure_ascii=False)
