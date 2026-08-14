"""ER-калибровка по накопленным решениям оператора (Фаза B, п.4, задача
2026-08-14, docs/verdict_strategy.md) — самостоятельная задача по
качеству данных, не связана с price_score/AUC.

**Важная находка, ради которой этот файл появился именно таким**:
`unit_match_gold_labels` (юнит-уровень, `phase2_unit_match.py`) — ДРУГОЙ
механизм, чем `AUTO_MATCH_THRESHOLD`/`REVIEW_QUEUE_THRESHOLD`
(`bot/core/entity_resolution.py::score_match()`, ЖК-уровень). Юнит-
матчер — дерево правил (номер квартиры/этаж+метраж+цена-или-дата/
зеркальный кап), БЕЗ непрерывного confidence вообще — калибровать эти
два порога по `unit_match_gold_labels` физически нельзя, там нечего
калибровать (нет числа, которое сравнивалось бы с порогом). Раздел
ниже — про то, что РЕАЛЬНО можно сказать из этих gold-labels (для
юнит-матчера), отдельно от калибровки ЖК-уровневых порогов (для которой
использованы `complex_source_links`/`complex_source_link_candidates`/
`complex_source_link_rejections` — те данные, где confidence реально
есть).
"""
from __future__ import annotations


def summarize_confidence_distribution(confidences: list[float], auto_threshold: float,
                                       review_threshold: float, bucket_width: float = 0.05) -> dict:
    """Гистограмма подтверждённых (TRUE POSITIVE) confidence-значений по
    бакетам ширины bucket_width — используется и для ЖК-уровня
    (complex_source_links), и в тестах. Чистая функция, без БД."""
    if not confidences:
        return {"n": 0, "buckets": {}, "below_review": 0, "review_tier": 0, "auto_tier": 0}
    buckets: dict[float, int] = {}
    below_review = review_tier = auto_tier = 0
    for c in confidences:
        if c >= auto_threshold:
            auto_tier += 1
        elif c >= review_threshold:
            review_tier += 1
        else:
            below_review += 1
        b = round((c // bucket_width) * bucket_width, 10)
        buckets[b] = buckets.get(b, 0) + 1
    return {
        "n": len(confidences), "buckets": dict(sorted(buckets.items())),
        "below_review": below_review, "review_tier": review_tier, "auto_tier": auto_tier,
    }


def unit_gold_label_confirmation_rate(decisions: list[str]) -> dict:
    """decisions: список 'decision' из unit_match_gold_labels — доля
    'approve' среди всех. НЕ AUC/precision (нет отрицательных примеров в
    системе на дату задачи, см. докстринг модуля) — просто честная
    сводка того, что есть."""
    n = len(decisions)
    approve = sum(1 for d in decisions if d == "approve")
    return {
        "n": n, "approve": approve,
        "approve_rate": approve / n if n else None,
        "other": n - approve,
    }


def evidence_confirmation_breakdown(evidence_list: list[dict]) -> dict:
    """Среди подтверждённых (approve) gold-labels юнит-матчера — сколько
    имели price_ok/date_ok=true в evidence (контекст, который видел
    оператор при решении), а сколько подтверждены БЕЗ этих сигналов
    (чисто по mirror-неоднозначности + визуальному суждению оператора) —
    информирует, стоит ли пересматривать "mirror_count>1 -> всегда
    review, никогда auto" правило decide_pair() (bot/core/phase2_unit_
    match.py) для узкого случая price_ok=true."""
    n = len(evidence_list)
    price_ok = sum(1 for e in evidence_list if e.get("price_ok") is True)
    date_ok = sum(1 for e in evidence_list if e.get("date_ok") is True)
    neither = sum(1 for e in evidence_list if not e.get("price_ok") and not e.get("date_ok"))
    return {"n": n, "price_ok": price_ok, "date_ok": date_ok, "neither": neither}
