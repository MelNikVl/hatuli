"""
Тестовый admin-only скор "класс жилья" по ЖК (не путать с housing_class /
housing_class_estimate — это категориальные лейблы эконом/комфорт/бизнес/
премиум, а здесь — числовой 0-100 скор для эксперимента). Используется
ТОЛЬКО на /admin/analytics/complexes (вкладка "Класс жилья") — НЕ входит
ни в production Deal Score, ни в Location Score, ни в housing_class_
estimate_recompute.py.

Веса (по ТЗ):
  price_per_m2     — наибольший вес (чем дороже м², тем выше класс)
  ceiling_height   — средний вес (выше потолки — выше класс)
  floors_total     — небольшой вес, ОБРАТНЫЙ (больше этажей — ниже класс)
  elevator         — небольшой вес: количество лифтов И их грузоподъёмность
                      (elevator_count, elevator_capacity_kg) нормализуются
                      отдельно и усредняются в один компонент под этим весом.

apartment_count УБРАН из весов (задача 2026-08-17, "исправить концепцию,
но пока не менять production-веса" — read-only аудит этого PR): гипотеза
"больше квартир в ЖК -> выше класс" не имеет под собой обоснования —
многоподъездные масс-маркет ЖК экономкласса систематически содержат
БОЛЬШЕ квартир, чем компактные премиум-клубные дома, значит сигнал скорее
ОБРАТНЫЙ интуиции или вообще не монотонный, а не положительный, каким он
был здесь. Новой калиброванной гипотезы по apartment_count в этой задаче
НЕТ (см. docs/scoring_audit.md, задача 2026-08-17: apartment_count можно
использовать только как характеристику плотности/масштаба ЖК, НЕ как
положительный признак класса) — до появления такой гипотезы сигнал
просто не учитывается (не "включён с весом 0" — вес удалён целиком, чтобы
не создавать видимость, что метрика используется).

Нормализация — min-max по всем ЖК, где метрика известна; для ЖК, где
какой-то метрики нет, вес просто не учитывается и перераспределяется между
имеющимися (сумма используемых весов приводится к 1.0).
"""
from __future__ import annotations

WEIGHTS = {
    "price_per_m2": 0.40,
    "ceiling_height": 0.20,
    "floors_total": 0.10,
    "elevator": 0.10,
}
# Метрики, где БОЛЬШЕ значение = НИЖЕ класс (шкала инвертируется при нормализации)
INVERSE = {"floors_total"}
# Метрики, из которых нормализацией строится сводный "elevator"-компонент
_ELEVATOR_INPUTS = ("elevator_count", "elevator_capacity_kg")
_SIMPLE_METRICS = ("price_per_m2", "ceiling_height", "floors_total")


def _minmax(values: list[float]) -> tuple[float, float]:
    return min(values), max(values)


def compute_housing_class_scores(rows: list[dict]) -> list[dict]:
    """rows: список dict с ключами id, name и метриками (могут быть None).
    Возвращает те же dict + score (0-100, None если вообще нет метрик) +
    score_details (какие метрики использованы и с каким весом)."""
    ranges: dict[str, tuple[float, float]] = {}
    for metric in (*_SIMPLE_METRICS, *_ELEVATOR_INPUTS):
        vals = [r[metric] for r in rows if r.get(metric) is not None]
        if vals:
            ranges[metric] = _minmax(vals)

    def _norm(metric: str, val: float) -> float:
        lo, hi = ranges[metric]
        if hi == lo:
            n = 50.0  # все ЖК одинаковы по этой метрике — нейтрально
        else:
            n = (val - lo) / (hi - lo) * 100.0
            if metric in INVERSE:
                n = 100.0 - n
        return n

    out = []
    for r in rows:
        r = dict(r)
        used_weights: dict[str, float] = {}
        norm_scores: dict[str, float] = {}

        for metric in _SIMPLE_METRICS:
            val = r.get(metric)
            if val is None or metric not in ranges:
                continue
            norm_scores[metric] = _norm(metric, val)
            used_weights[metric] = WEIGHTS[metric]

        elev_parts = [
            _norm(m, r[m]) for m in _ELEVATOR_INPUTS
            if r.get(m) is not None and m in ranges
        ]
        if elev_parts:
            norm_scores["elevator"] = sum(elev_parts) / len(elev_parts)
            used_weights["elevator"] = WEIGHTS["elevator"]

        total_weight = sum(used_weights.values())
        if total_weight > 0:
            score = sum(norm_scores[m] * used_weights[m] for m in used_weights) / total_weight
            r["score"] = round(score, 1)
        else:
            r["score"] = None
        r["score_details"] = {
            m: {"normalized": round(norm_scores[m], 1), "weight": used_weights[m]}
            for m in used_weights
        }
        out.append(r)
    return out
