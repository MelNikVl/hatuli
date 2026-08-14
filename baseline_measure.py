#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Baseline-замер v2 (Фаза A.5, п.5 вердикт-стратегии, задача 2026-08-14,
docs/verdict_strategy.md) — переписан поверх Фазы A версии (docs/
scoring_roadmap.md, Часть 6 п.3) с временной защитой.

**Проблема версии Фазы A**: она сравнивала СЕГОДНЯШНИЙ score_total
(посчитанный сегодняшней формулой) с исходами, случившимися неделями
раньше при другой формуле/вводных — число получалось не бессмысленным
(AUC=0.87), но нечестным: неизвестно, было ли ИМЕННО ЭТО значение
показано пользователю, когда исход ещё не был известен. deal_score_
snapshots (Фаза A.5 п.2-3) даёт исторический ряд, позволяющий это
исправить.

**temporally_safe** — для каждого объявления берётся САМЫЙ РАННИЙ снимок
из deal_score_snapshots (MIN(observed_at)) и проверяется, что он снят
близко к началу окна исхода — observed_at <= first_seen + 3 дня (грейс на
то, что таймер снимка — ежедневный, объявление может появиться в любое
время суток). Если снимка нет вовсе, ИЛИ он снят позже — temporally_
safe=False, строка НЕ участвует в основных метриках (только в отчёте
покрытия). Дополнительно исключаются объявления с observation_days < 30
(окно исхода физически не успело закрыться, см. outcome_labels_
recompute.py) и censored=TRUE (ещё активны, исход не финален).

**Известное ограничение на дату задачи** (честно, не баг): deal_score_
snapshots начал копиться только 2026-08-14 — снимок берётся ТОЛЬКО для
активных объявлений (архивные не снимаются, docs см. deal_score_
snapshot.py), а разрешённый исход есть в основном у АРХИВНЫХ. Значит
пересечение "есть ранний снимок" И "есть разрешённый исход" на эту дату
почти пусто — временно-защищённая выборка будет МАЛЕНЬКОЙ или пустой,
пока не накопится достаточно дней (объявление должно быть снято, пока
ещё активно, и потом дожить до архивации/закрытия окна). Это ожидаемо,
не повод считать скрипт сломанным — то же самое ограничение, что уже
задокументировано в outcome_labels_recompute.py для clean_disappearance_
within_30d/relisted_within_60d.

Секция 2 (legacy, temporally_safe=False) сохраняет метрики версии Фазы A
для непрерывности сравнения — явно помечена как потенциально нечестная
(текущий score сравнивается с давним исходом), не как замена секции 1.

AUC — Манн-Уитни (scipy). PR-AUC (average precision) и lift@10/
precision@10/calibration-по-децилям — реализованы вручную на numpy
(sklearn в окружении нет, см. Фаза A baseline_measure.py v1) по
стандартной формуле AP = Σ(R_n - R_{n-1})·P_n.

Не пишет в БД — чистое чтение, разовый прогон, результат идёт в отчёт
(docs/scoring_roadmap.md).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os

import numpy as np
from scipy import stats

from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler("baseline_measure.log", encoding="utf-8", errors="replace")],
)
log = logging.getLogger("baseline_measure")

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")

MIN_N = 10  # минимум наблюдений, чтобы вообще пытаться считать метрику

QUERY = """
    WITH first_snapshot AS (
        -- Самый ранний снимок на объявление — DISTINCT ON + ORDER BY
        -- вместо коррелированного подзапроса на каждую строку (тот же
        -- урок, что уже был у deal_score.py/outcome_labels_recompute.py
        -- про O(n²) на большой таблице).
        SELECT DISTINCT ON (listing_id)
               listing_id, observed_at, score_total AS snap_score_total,
               price_score AS snap_price_score, quality_score AS snap_quality_score,
               market_score AS snap_market_score, risk_score AS snap_risk_score,
               bargain_discount_pct AS snap_bargain_discount_pct
        FROM deal_score_snapshots
        ORDER BY listing_id, observed_at ASC
    )
    SELECT
        a.id, a.first_seen, a.score_total AS current_score_total,
        a.hex_details AS current_hex_details, a.bargain_discount_pct AS current_bargain_discount_pct,
        o.disappeared_within_30d, o.clean_disappearance_within_30d,
        o.observation_days, o.censored, o.time_on_market,
        fs.observed_at AS snapshot_observed_at,
        fs.snap_score_total, fs.snap_price_score, fs.snap_quality_score,
        fs.snap_market_score, fs.snap_risk_score, fs.snap_bargain_discount_pct,
        (fs.observed_at IS NOT NULL AND fs.observed_at <= a.first_seen + INTERVAL '3 days') AS temporally_safe
    FROM apartment_listings a
    JOIN outcome_labels o ON o.listing_id = a.id
    LEFT JOIN first_snapshot fs ON fs.listing_id = a.id
    WHERE a.market_type = 'secondary'
      AND COALESCE(a.is_duplicate, FALSE) = FALSE
      AND o.disappeared_within_30d IS NOT NULL
"""
# "Недостаточное observation window" (задача Фазы A.5 п.5) — НЕ голое
# observation_days>=30 И НЕ censored=FALSE. Обе идеи оказались неверны на
# практике (живые баги первой версии этого файла, пойманы прогоном на
# реальных данных, не оставлены "на совесть" — исправлены здесь же):
#  1) observation_days>=30 парадоксально вырезал бы ИМЕННО интересующие
#     TRUE-случаи (быстро исчезло -> мало дней наблюдения нужно, чтобы
#     честно это знать) — давал n_true=0 из 186.
#  2) censored=TRUE (ещё активно, archived_at IS NULL) НЕ значит "исход
#     неизвестен" для ЭТОЙ метки: старое (>30д), но всё ещё активное
#     объявление имеет полностью разрешённый FALSE ("не исчезло за 30
#     дней" — окончательный факт, не намёк) — фильтр по censored убирал
#     7627->1164 строк, почти все отфильтрованные были ИМЕННО такими
#     легитимными FALSE-случаями, не censored-в-смысле-неизвестности.
# Правильная (и единственная нужная) граница уже встроена в саму
# disappeared_within_30d (NULL, пока окно честно не разрешилось, см.
# outcome_labels_recompute.py) — o.disappeared_within_30d IS NOT NULL
# самодостаточно, ничего досчитывать не нужно.


def auc_mannwhitney(scores: np.ndarray, labels: np.ndarray) -> tuple[float | None, int, int]:
    pos, neg = scores[labels], scores[~labels]
    n_pos, n_neg = len(pos), len(neg)
    if n_pos == 0 or n_neg == 0:
        return None, n_pos, n_neg
    u_stat, _ = stats.mannwhitneyu(pos, neg, alternative="two-sided")
    return float(u_stat / (n_pos * n_neg)), n_pos, n_neg


def average_precision(scores: np.ndarray, labels: np.ndarray) -> float | None:
    """PR-AUC вручную (sklearn недоступен) — стандартная формула
    AP = Σ_n (R_n - R_{n-1})·P_n по точкам, отсортированным по убыванию
    скора (тот же результат, что sklearn.metrics.average_precision_score
    при отсутствии совпадающих скоров на границе; при связках — небольшое
    расхождение в третьем знаке, не критично для порядка величины)."""
    n_pos = int(labels.sum())
    if n_pos == 0 or n_pos == len(labels):
        return None
    order = np.argsort(-scores, kind="mergesort")
    labels_sorted = labels[order]
    tp_cum = np.cumsum(labels_sorted)
    fp_cum = np.cumsum(~labels_sorted)
    precision = tp_cum / (tp_cum + fp_cum)
    recall = tp_cum / n_pos
    recall_prev = np.concatenate(([0.0], recall[:-1]))
    return float(np.sum((recall - recall_prev) * precision))


def lift_and_precision_at_k(scores: np.ndarray, labels: np.ndarray, k_frac: float = 0.1) -> tuple[float, float, int]:
    n = len(scores)
    k = max(1, int(np.ceil(n * k_frac)))
    order = np.argsort(-scores, kind="mergesort")
    top_labels = labels[order[:k]]
    precision_at_k = float(top_labels.mean())
    base_rate = float(labels.mean())
    lift = precision_at_k / base_rate if base_rate > 0 else float("nan")
    return precision_at_k, lift, k


def calibration_deciles(scores: np.ndarray, labels: np.ndarray) -> list[tuple[int, int, float, float]]:
    """(decile 1..10 по возрастанию скора, n, доля TRUE, средний скор)."""
    order = np.argsort(scores, kind="mergesort")
    groups = np.array_split(order, 10)
    return [(i + 1, len(idx), float(labels[idx].mean()) if len(idx) else float("nan"),
              float(scores[idx].mean()) if len(idx) else float("nan"))
             for i, idx in enumerate(groups) if len(idx) > 0]


def _report_signal(name: str, scores: np.ndarray, labels: np.ndarray, valid: np.ndarray) -> None:
    v = valid & ~np.isnan(scores)
    n = int(v.sum())
    if n < MIN_N:
        log.info("  %-22s недостаточно данных (n=%d < %d)", name, n, MIN_N)
        return
    s, l = scores[v], labels[v]
    # Живой баг, найденный на реальных данных (Фаза A.5 п.5): price_score
    # целочисленный 0-100 — до 19% значений связаны в один узел (score=100).
    # np.argsort стабилен — при связках сохраняет порядок строк ИЗ SQL
    # (без ORDER BY, физический порядок таблицы), который coen НЕ случаен
    # относительно исхода. Из-за этого "топ-10% по убыванию" (argsort(-s))
    # и "дециль 10" (argsort(s), последняя группа) выбирали РАЗНЫЕ
    # подмножества одной и той же связки — precision@10%=0.000 против
    # дециля 0.57 для ОДНОГО И ТОГО ЖЕ сигнала. Фикс — одна фиксированная
    # случайная перестановка перед ВСЕМИ ранжированиями ниже (AP/lift/
    # calibration), чтобы связки бились одинаково и не тянули артефакт
    # исходного порядка строк за реальный сигнал.
    rng = np.random.default_rng(20260814)
    perm = rng.permutation(n)
    s, l = s[perm], l[perm]
    auc, n_pos, n_neg = auc_mannwhitney(s, l)
    ap = average_precision(s, l)
    prec10, lift10, k = lift_and_precision_at_k(s, l)
    base_rate = float(l.mean())
    log.info("  %-22s AUC=%s  PR-AUC=%s  precision@10%%=%s  lift@10%%=%s  (n=%d, n_true=%d, base_rate=%.3f, k=%d)",
              name,
              f"{auc:.4f}" if auc is not None else "n/a",
              f"{ap:.4f}" if ap is not None else "n/a",
              f"{prec10:.3f}", f"{lift10:.2f}" if lift10 == lift10 else "n/a",
              n, n_pos, base_rate, k)
    deciles = calibration_deciles(s, l)
    dec_str = " ".join(f"d{d}:{rate:.2f}" for d, _, rate, _ in deciles)
    log.info("    калибровка по децилям (низкий->высокий скор, доля TRUE): %s", dec_str)


async def main() -> None:
    from bot.db.pg import init_pool, close_pool, fetch
    await init_pool(DATABASE_URL)
    try:
        rows = await fetch(QUERY)
    finally:
        await close_pool()

    n_total = len(rows)
    temporally_safe_mask = np.array([bool(r["temporally_safe"]) for r in rows])
    n_safe = int(temporally_safe_mask.sum())

    log.info("=" * 74)
    log.info("BASELINE-ЗАМЕР v2 (Фаза A.5 п.5) — выборка: %d разрешённых исходов (вторичка)", n_total)
    log.info("temporally_safe=True: %d (%.1f%%) — есть ранний снимок (<=3дн от first_seen)",
              n_safe, 100 * n_safe / n_total if n_total else 0)
    log.info("temporally_safe=False: %d — снимка нет или снят поздно (ожидаемо на 2026-08-14, "
              "см. докстринг модуля)", n_total - n_safe)
    log.info("=" * 74)

    disappeared = np.array([bool(r["disappeared_within_30d"]) for r in rows])
    clean_resolved = np.array([r["clean_disappearance_within_30d"] is not None for r in rows])
    clean = np.array([bool(r["clean_disappearance_within_30d"]) if r["clean_disappearance_within_30d"] is not None else False for r in rows])

    # ── Секция 1: temporally_safe — snapshot-скоры (снятые ДО начала окна) ──
    log.info("СЕКЦИЯ 1 — temporally_safe=True (snapshot-скор, снят рано, до исхода)")
    if n_safe < MIN_N:
        log.info("  Недостаточно данных (n=%d < %d) — deal_score_snapshots начал копиться "
                  "2026-08-14, нужно больше дней накопления (см. докстринг). НЕ баг.", n_safe, MIN_N)
    else:
        snap_price = np.array([r["snap_price_score"] if r["snap_price_score"] is not None else np.nan for r in rows], dtype=float)
        snap_quality = np.array([r["snap_quality_score"] if r["snap_quality_score"] is not None else np.nan for r in rows], dtype=float)
        snap_market = np.array([r["snap_market_score"] if r["snap_market_score"] is not None else np.nan for r in rows], dtype=float)
        snap_total = np.array([r["snap_score_total"] if r["snap_score_total"] is not None else np.nan for r in rows], dtype=float)
        snap_bargain = np.array([r["snap_bargain_discount_pct"] if r["snap_bargain_discount_pct"] is not None else np.nan for r in rows], dtype=float)
        for label_name, label_arr, resolved in [
            ("disappeared_within_30d", disappeared, temporally_safe_mask),
            ("clean_disappearance_within_30d", clean, temporally_safe_mask & clean_resolved),
        ]:
            log.info("-- по %s --", label_name)
            _report_signal("snapshot score_total", snap_total, label_arr, resolved)
            _report_signal("snapshot price_score", snap_price, label_arr, resolved)
            _report_signal("snapshot quality_score", snap_quality, label_arr, resolved)
            _report_signal("snapshot market_score", snap_market, label_arr, resolved)
            _report_signal("snapshot bargain_discount_pct", snap_bargain, label_arr, resolved)

    log.info("=" * 74)
    # ── Секция 2: legacy (temporally_safe=False) — для непрерывности с Фазой A ──
    log.info("СЕКЦИЯ 2 — legacy/temporally_safe=False (ТЕКУЩИЙ score_total против ДАВНЕГО "
              "исхода — потенциально нечестно, только для сравнения с Фазой A)")
    cur_total = np.array([r["current_score_total"] if r["current_score_total"] is not None else np.nan for r in rows], dtype=float)
    cur_price, cur_quality, cur_market = [], [], []
    for r in rows:
        hd = r["current_hex_details"]
        parsed = json.loads(hd) if hd else {}
        comp = parsed.get("components", {}) if isinstance(parsed, dict) else {}
        cur_price.append(comp.get("price", {}).get("score", np.nan))
        cur_quality.append(comp.get("quality", {}).get("score", np.nan))
        cur_market.append(comp.get("market", {}).get("score", np.nan))
    cur_price = np.array(cur_price, dtype=float)
    cur_quality = np.array(cur_quality, dtype=float)
    cur_market = np.array(cur_market, dtype=float)
    cur_bargain = np.array([r["current_bargain_discount_pct"] if r["current_bargain_discount_pct"] is not None else np.nan for r in rows], dtype=float)
    all_true = np.ones(n_total, dtype=bool)
    for label_name, label_arr, resolved in [
        ("disappeared_within_30d", disappeared, all_true),
        ("clean_disappearance_within_30d", clean, clean_resolved),
    ]:
        log.info("-- по %s --", label_name)
        _report_signal("current score_total", cur_total, label_arr, resolved)
        _report_signal("current price_score", cur_price, label_arr, resolved)
        _report_signal("current quality_score", cur_quality, label_arr, resolved)
        _report_signal("current market_score", cur_market, label_arr, resolved)
        _report_signal("current bargain_discount_pct", cur_bargain, label_arr, resolved)

    log.info("=" * 74)
    log.info("survives_90d/TOM-корреляция — см. Фазу A (scoring_roadmap.md Часть 6 п.3), "
              "не переизмерено здесь: то же структурное ограничение (датасет младше 90 дней) "
              "актуально и сейчас, повторный прогон дал бы тот же честный пропуск.")


if __name__ == "__main__":
    asyncio.run(main())
