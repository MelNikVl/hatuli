"""
Тестовый admin-only скор "класс жилья" по ЖК (не путать с housing_class /
housing_class_estimate — это категориальные лейблы эконом/комфорт/бизнес/
премиум, а здесь — числовой 0-100 скор для эксперимента).

Веса (по ТЗ):
  price_per_m2     — наибольший вес (чем дороже м², тем выше класс)
  ceiling_height   — средний вес (выше потолки — выше класс)
  apartment_count  — средний вес (больше квартир в ЖК — выше класс)
  floors_total     — небольшой вес, ОБРАТНЫЙ (больше этажей — ниже класс)
  elevator_count   — небольшой вес (больше лифтов — выше класс)

Нормализация — min-max по всем ЖК, где метрика известна; для ЖК, где
какой-то метрики нет, вес просто не учитывается и перераспределяется между
имеющимися (сумма используемых весов приводится к 1.0).
"""
from __future__ import annotations

WEIGHTS = {
    "price_per_m2": 0.40,
    "ceiling_height": 0.20,
    "apartment_count": 0.20,
    "floors_total": 0.10,
    "elevator_count": 0.10,
}
# Метрики, где БОЛЬШЕ значение = НИЖЕ класс (шкала инвертируется при нормализации)
INVERSE = {"floors_total"}


def _minmax(values: list[float]) -> tuple[float, float]:
    return min(values), max(values)


def compute_housing_class_scores(rows: list[dict]) -> list[dict]:
    """rows: список dict с ключами id, name и метриками (могут быть None).
    Возвращает те же dict + score (0-100, None если вообще нет метрик) +
    score_details (какие метрики использованы и с каким весом)."""
    ranges: dict[str, tuple[float, float]] = {}
    for metric in WEIGHTS:
        vals = [r[metric] for r in rows if r.get(metric) is not None]
        if vals:
            ranges[metric] = _minmax(vals)

    out = []
    for r in rows:
        r = dict(r)
        used_weights = {}
        norm_scores = {}
        for metric, weight in WEIGHTS.items():
            val = r.get(metric)
            if val is None or metric not in ranges:
                continue
            lo, hi = ranges[metric]
            if hi == lo:
                norm = 50.0  # все ЖК одинаковы по этой метрике — нейтрально
            else:
                norm = (val - lo) / (hi - lo) * 100.0
                if metric in INVERSE:
                    norm = 100.0 - norm
            norm_scores[metric] = norm
            used_weights[metric] = weight

        total_weight = sum(used_weights.values())
        if total_weight > 0:
            score = sum(norm_scores[m] * used_weights[m] for m in used_weights) / total_weight
            r["score"] = round(score, 1)
        else:
            r["score"] = None
        r["score_details"] = {
            m: {"value": r.get(m), "normalized": round(norm_scores[m], 1), "weight": used_weights[m]}
            for m in used_weights
        }
        out.append(r)
    return out
