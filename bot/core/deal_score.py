"""
Deal Score v4 — единый многофакторный скоринг привлекательности сделки.

Это ЕДИНСТВЕННЫЙ источник score_total и его breakdown — старая параллельная
система (bot/core/apartment_score_v2.py, независимо считавшая score_yield/
score_price_market/score_location/score_apt_type/score_floor/score_complex/
score_supply при каждом парсинге) удалена: два независимых скоринга давали
рассинхрон — score_total уже был v3, а breakdown в попапе/аналитике всё ещё
показывал старые числа v2. Теперь breakdown-колонки — это ПРОЕКЦИЯ компонентов
v4 (см. _legacy_breakdown), т.е. всегда согласованы со score_total.

Синтез подходов (по материалам анализа 4 моделей, см. заметку в Notion):
  1. Hedonic-ядро: ожидаемая цена P_expected строится НЕ из ручных весов,
     а из локальной рыночной медианы (гексагон + кольцо + город, δ-доступность),
     скорректированной на класс ЖК и высоту потолков (см. _class_adj/_ceiling_adj) —
     это и есть "цена складывается из локации, метража (сегментация по комнатам
     уже это учитывает), потолков, этажа (см. риск) и класса ЖК".
     Deal Index = P_expected / P_ask — цена участвует только в сравнении
     с ожиданием, а не «весом в скоре».
  2. Субиндексы 0–100 с прозрачными весами (как у ЦИАН/Zillow):
     Price · Location · Quality · Market · Risk — веса настраиваются в
     /admin/settings (см. SCORE_W_* в app_settings), а не захардкожены,
     потому что "правильные" веса зависят от профиля покупателя (семья vs
     инвестор) — так делают все нормальные платформы.
  3. Location включает инфраструктурный слой (школы/сады/вузы из city_poi,
     OSM) — не только уровень цен гексагона, но и реальные удобства района.
  4. Quality: при отсутствии housing_class (для ~89% ЖК он не известен —
     Korter/Homster покрывают только часть каталога) используем прокси —
     перцентиль цены/м² в сегменте — вместо плоского дефолта 50.
  5. Confidence: доля компонентов, посчитанных на реальных данных —
     низкий confidence = оценке доверяем меньше (показываем в UI).

ВАЖНО про эмпирическую калибровку весов (то, о чём пишут все 4 модели):
она требует данных о РЕАЛЬНЫХ исходах сделок (цена продажи vs цена в
объявлении) — этого у нас пока нет (Krisha просто убирает проданное
объявление, конечную цену мы не видим). Без этого регрессия/ML только
переобучится на самой цене (циркулярно). Экспертные веса-слайдеры — это
осознанный шаг 1; шаг 2 (калибровка) — когда появится сигнал об исходе
сделки (см. TODO в apply_deal_scores).

Первичку (market_type='primary') не трогаем — там своя модель.
"""
from __future__ import annotations

import bisect
import json
import logging
from statistics import median

from bot.core.hexgrid import hex_id, neighbors
from bot.core.hedonic_constants import AREA_BAND_PCT, MIN_BLDG, MIN_HEX, MIN_RING, W0, W1, W2

logger = logging.getLogger(__name__)

# ── Веса компонентов по умолчанию (сумма = 1.0) — переопределяются через
# /admin/settings (SCORE_W_PRICE/LOCATION/QUALITY/MARKET/RISK, в процентах) ──
# Location убран из скоринга по явному запросу (район/локация как отдельный
# фактор посчитали не имеющим смысла) — вес 0, доли остальных компонентов
# нормализуются автоматически (см. wsum-деление ниже), т.е. price/quality/
# market/risk просто пропорционально занимают освободившиеся 20%.
_DEFAULT_WEIGHTS = {"price": 0.40, "location": 0.0, "quality": 0.20,
                    "market": 0.15, "risk": 0.05}

# δ-доступность гекс-модели. Задача 2026-08-14 (Часть 2, п.9: "общее
# hedonic-ядро для bargain.py и deal_score.py") — эти константы больше
# НЕ определяются здесь по-своему, импортируются из bot/core/
# hedonic_constants.py вместе с bargain.py: раньше синхронизировались
# вручную комментарием (см. живой кейс #1014506231 "Landmark" ниже —
# до фикса own_bldg не фильтровался по площади, тот же комнатный сегмент
# ("3-комн") сравнивал 110м² квартиру с 74-80м² квартирами того же ЖК
# — площадь большая почти всегда дешевле за м², не аномалия — badge
# показал "Недооценено на 48%", а параллельный bargain.py для ТОГО ЖЕ
# объявления честно нашёл "переоценена на 43%" по гекс+кольцо аналогам
# — оба числа одновременно попадали на одну страницу объявления,
# противореча друг другу). Импорт из одного модуля не даёт двум файлам
# физически разойтись снова — комментарий-синхронизация могла (сами
# константы — см. import в начале файла).

# Отделка (finish_level, bot/core/listing_intel.detect_finish_level —
# определяется по тексту объявления при парсинге) — задача 2026-08-14,
# "finish_level: убрать тихий инкремент... отделку — входом в quality-
# компонент v4 с небольшим весом". Раньше отделка ПРАВИЛА score_total
# напрямую (service_apartments.py: score_total += adj), в обход всей
# модели весов и БЕЗ учёта того, что apply_deal_scores() тут же следом
# полностью перезаписывает score_total = deal — инкремент почти всегда
# стирался в том же цикле (см. docs/scoring_audit.md §5.2, живой баг
# "не имеет эффекта"). Теперь — небольшой (0.12 из quality, т.е. не
# сильнее прокси-класса и заметно слабее года/рейтинга) полноправный
# субкомпонент quality, виден в breakdown, не теряется при пересчёте.
_FINISH_QUALITY_SCORE = {
    "rough": 20, "prefinish": 35, "needs_repair": 25,
    "finished": 60, "renovated": 75, "furnished": 80, "designer": 95,
}
_FINISH_LABEL = {
    "rough": "черновая", "prefinish": "предчистовая", "needs_repair": "требует ремонта",
    "finished": "чистовая", "renovated": "свежий ремонт", "furnished": "с мебелью", "designer": "дизайнерский ремонт",
}
_W_FINISH_IN_QUALITY = 0.12

_CLASS_SCORE = {"элит": 100, "бизнес": 80, "комфорт": 60, "эконом": 35}
# Ценовая надбавка/дисконт класса ЖК к ожидаемой цене — тот же порядок величин,
# что и субиндекс, но применяется к P_expected (класс — часть цены, а не
# только описательный атрибут).
_CLASS_PRICE_ADJ = {"элит": 1.25, "бизнес": 1.12, "комфорт": 1.0, "эконом": 0.85}

# Высота потолков: в Астане заметный ценовой фактор (панель ~2.5-2.7м vs
# монолит 3м+). База — 2.7м, ±6%/10см, капим ±18%/-12% чтобы не улетать
# на выбросах разметки.
_CEILING_BASELINE = 2.7
_CEILING_PCT_PER_10CM = 0.06
_CEILING_ADJ_MIN, _CEILING_ADJ_MAX = -0.12, 0.18

# POI (city_poi, OSM): гексагон 500м + кольцо ~ радиус пешей доступности
POI_EDGE_M = 500.0
POI_WEIGHTS = {"school": 12, "kindergarten": 8, "university": 15}
POI_NEUTRAL = 50  # если справочник city_poi пуст — не штрафуем/не бонусим


def _rooms_bucket(rooms) -> str:
    if not rooms or rooms <= 1:
        return "1"
    if rooms >= 4:
        return "4+"
    return str(int(rooms))


def _clamp(v, lo=0, hi=100):
    return max(lo, min(hi, v))


def _year_score(y):
    if not y:
        return None
    if y >= 2022:
        return 100
    if y >= 2018:
        return 80
    if y >= 2012:
        return 60
    if y >= 2000:
        return 40
    return 20


def _class_key(cls: str) -> str | None:
    return next((k for k in _CLASS_SCORE if k in cls), None)


def _ceiling_adj(ceiling_height) -> float:
    if not ceiling_height:
        return 1.0
    pct = (float(ceiling_height) - _CEILING_BASELINE) / 0.10 * _CEILING_PCT_PER_10CM
    return 1.0 + max(_CEILING_ADJ_MIN, min(_CEILING_ADJ_MAX, pct))


def _apt_type_pts(rooms, area) -> int:
    """Ликвидность по комнатности/площади (0-15) — та же эвристика, что была
    в apartment_score_v2, но теперь только проекция для legacy-колонки
    score_apt_type (см. модуль docstring), не отдельный вес в total."""
    score = 8
    if rooms == 1:
        score = 15
    elif rooms == 2:
        score = 13
    elif rooms == 3:
        score = 8
    elif rooms and rooms >= 4:
        score = 4
    if area:
        if area > 100:
            score = max(score - min(6, int((area - 100) / 10)), 1)
        elif area > 70:
            score = max(score - 2, 2)
    return score


def _poi_index_from_rows(rows) -> dict[str, list[tuple[str, float, float]]]:
    idx: dict[str, list[tuple[str, float, float]]] = {}
    for r in rows:
        if r["lat"] is None or r["lon"] is None:
            continue
        hid = hex_id(float(r["lat"]), float(r["lon"]), POI_EDGE_M)
        idx.setdefault(hid, []).append((r["kind"], float(r["lat"]), float(r["lon"])))
    return idx


def _poi_score(lat: float, lon: float, poi_idx: dict) -> tuple[int, int]:
    if not poi_idx:
        return POI_NEUTRAL, 0
    hid = hex_id(lat, lon, POI_EDGE_M)
    cnt, pts = 0, 0.0
    for cell in [hid] + neighbors(hid):
        for kind, _plat, _plon in poi_idx.get(cell, []):
            pts += POI_WEIGHTS.get(kind, 5)
            cnt += 1
    return min(100, round(pts)), cnt


def compute_deal_scores(listings: list[dict], complexes: dict[str, dict],
                        edge_m: float, weights: dict | None = None,
                        poi_idx: dict | None = None,
                        complexes_by_id: dict[int, dict] | None = None) -> dict[str, dict]:
    """
    listings: [{id, lat, lon, price, area, rooms, floor, floors_total,
                year_built, complex_name, is_owner, first_seen_days, yield_pct,
                same_complex_cnt, district, ceiling_height, resolved_house_id,
                finish_level}]
    complexes: {lower(trim(name)): {housing_class, year_built, krisha_rating}}
    complexes_by_id: {complexes.id: то же самое} — задача 2026-08-14
             ("House-resolution в скоринге"): объявление зонтика, которое
             house_resolution.py уже привязал К КОНКРЕТНОМУ ДОМУ по адресу/
             токену/гео (resolved_house_id), может по-прежнему называть ЖК
             именем зонтика в самом тексте (complex_name) — старый lookup
             ТОЛЬКО по имени тогда брал housing_class/year_built зонтика
             (агрегата), а не дома, к которому объявление реально
             привязано. Если передан и у листинга есть resolved_house_id —
             это приоритетный путь (тот же принцип, что _listing_id_match
             в terminal_extras.py — resolved_house_id точнее текстового
             имени); без него (None, старые вызовы) поведение не меняется —
             только текстовый lookup, как раньше.
    weights: {"price","location","quality","market","risk"} — доли, сумма 1.0
             (если None — дефолт _DEFAULT_WEIGHTS). "risk" принимается для
             обратной совместимости вызова (и всё ещё читается из
             app_settings SCORE_W_RISK на /admin/settings), но с задачи
             2026-08-14 (Фаза A п.6 вердикт-стратегии, docs/
             verdict_strategy.md §5) НЕ участвует в взвешенной сумме —
             см. докстринг ниже про risk_score/flags.
    poi_idx: индекс city_poi по гексагонам (см. _poi_index_from_rows) —
             если None, локация считается без инфраструктурного слоя.
    Возвращает {id: {deal, confidence, di, expected_m2, components{...},
                flags: [...]}} — flags см. докстринг у блока RISK ниже.
    """
    w_in = {**_DEFAULT_WEIGHTS, **(weights or {})}
    # risk больше НЕ входит в нормировку суммы весов (Фаза A п.6) — тот же
    # приём, что уже применялся к location (W_LOC=0, прежнее решение
    # заказчика): числитель/знаменатель нормировки считаются только по
    # price+location+quality+market, пропорции price:quality:market между
    # собой сохраняются (40:20:15 — если app_settings не переопределяет).
    # risk остаётся полноценным компонентом (score+text в breakdown), но
    # его роль — список флагов вердикта (risk_bits → flags ниже), не вес
    # в среднем, который тонул на фоне остальных 95%.
    wsum = (w_in["price"] + w_in["location"] + w_in["quality"] + w_in["market"]) or 1.0
    W_PRICE = w_in["price"] / wsum
    W_LOC = w_in["location"] / wsum
    W_QUALITY = w_in["quality"] / wsum
    W_MARKET = w_in["market"] / wsum
    W_RISK = 0.0

    # ── Гекс-агрегации (hedonic ядро) ────────────────────────────────────────
    seg_hex: dict[tuple[str, str], list[float]] = {}
    # (area, p_m2) — не просто p_m2 — чтобы own_bldg ниже мог отфильтровать
    # по площади ±AREA_BAND_PCT перед медианой (см. MIN_BLDG docstring).
    seg_bldg: dict[tuple[str, str], list[tuple[float, float]]] = {}
    seg_city: dict[str, list[float]] = {}
    enriched = []
    for l in listings:
        if not l.get("lat") or not l.get("price") or not l.get("area"):
            continue
        p_m2 = float(l["price"]) / float(l["area"])
        if p_m2 <= 0:
            continue
        seg = _rooms_bucket(l.get("rooms"))
        hid = hex_id(float(l["lat"]), float(l["lon"]), edge_m)
        bkey = (l.get("complex_name") or "").strip().lower() or None
        seg_hex.setdefault((seg, hid), []).append(p_m2)
        seg_city.setdefault(seg, []).append(p_m2)
        if bkey:
            seg_bldg.setdefault((seg, bkey), []).append((float(l["area"]), p_m2))
        enriched.append((l, seg, hid, bkey, p_m2))
    city_med = {s: median(v) for s, v in seg_city.items() if v}
    city_sorted = {s: sorted(v) for s, v in seg_city.items()}

    out: dict[str, dict] = {}
    for l, seg, hid, bkey, p_m2 in enriched:
        own = list(seg_hex.get((seg, hid), []))
        try:
            own.remove(p_m2)
        except ValueError:
            pass
        ring: list[float] = []
        for nb in neighbors(hid):
            ring.extend(seg_hex.get((seg, nb), []))
        d0 = len(own) >= MIN_HEX
        d1 = len(ring) >= MIN_RING
        p_city = city_med.get(seg)
        if not p_city:
            continue
        area = float(l["area"])
        area_lo, area_hi = area * (1 - AREA_BAND_PCT), area * (1 + AREA_BAND_PCT)
        own_bldg = [pm for a, pm in seg_bldg.get((seg, bkey), [])
                    if area_lo <= a <= area_hi] if bkey else []
        try:
            own_bldg.remove(p_m2)
        except ValueError:
            pass
        db = len(own_bldg) >= MIN_BLDG
        if db:
            # Хватает аналогов в том же доме/ЖК — они точнее любого
            # геометрического гекса, дальше вниз по цепочке не спускаемся.
            expected_raw = median(own_bldg)
            sources_bldg = True
        else:
            num, den = W2 * p_city, W2
            if d0:
                num += W0 * median(own)
                den += W0
            if d1:
                num += W1 * median(ring)
                den += W1
            expected_raw = num / den
            sources_bldg = False
        sources = ("тот же дом/ЖК" if sources_bldg else
                   "гекс+кольцо+город" if d0 and d1 else
                   "кольцо+город" if d1 else "гекс+город" if d0 else "только город")

        # ── Hedonic-корректировка P_expected: класс ЖК + высота потолков ────
        # (метраж уже учтён через сегментацию по комнатности; этаж — в Risk)
        _house_id = l.get("resolved_house_id")
        cx = ((complexes_by_id.get(_house_id) if complexes_by_id and _house_id else None)
              or complexes.get((l.get("complex_name") or "").strip().lower()) or {})
        cls = (cx.get("housing_class") or "").lower()
        cls_key = _class_key(cls)
        class_adj = _CLASS_PRICE_ADJ.get(cls_key, 1.0)
        ceiling_adj = _ceiling_adj(l.get("ceiling_height"))
        expected = expected_raw * class_adj * ceiling_adj
        di = expected / p_m2

        # ── 1. PRICE (40%): DI → 0..100 ─────────────────────────────────────
        price_score = _clamp(round(50 + (di - 1) * 200))
        pct = round((di - 1) * 100)
        adj_bits = []
        if cls_key:
            adj_bits.append(f"класс «{cls_key}» {'+' if class_adj > 1 else ''}{round((class_adj-1)*100)}%")
        if l.get("ceiling_height") and abs(ceiling_adj - 1) > 0.005:
            adj_bits.append(f"потолки {l['ceiling_height']}м {'+' if ceiling_adj > 1 else ''}{round((ceiling_adj-1)*100)}%")
        adj_txt = f" (с поправкой: {', '.join(adj_bits)})" if adj_bits else ""
        price_txt = ((f"на {abs(pct)}% дешевле локального ожидания" if pct > 2 else
                     f"на {abs(pct)}% дороже локального ожидания" if pct < -2 else
                     "цена на уровне локального рынка") + adj_txt)

        # ── 2. LOCATION (20%): цена гексагона vs город + инфраструктура ─────
        loc_ratio = expected_raw / p_city
        price_loc_score = _clamp(round(50 + (loc_ratio - 1) * 100), 5, 100)
        poi_sc, poi_cnt = _poi_score(float(l["lat"]), float(l["lon"]), poi_idx or {})
        loc_score = _clamp(round(price_loc_score * 0.7 + poi_sc * 0.3))
        loc_txt = (f"локация дороже городской медианы на {round((loc_ratio-1)*100)}%"
                   if loc_ratio > 1.02 else
                   f"локация дешевле городской медианы на {round((1-loc_ratio)*100)}%"
                   if loc_ratio < 0.98 else "локация на уровне города")
        loc_txt += f", рядом {poi_cnt} объектов инфраструктуры (школы/сады/вузы)" if poi_idx else ""

        # ── 3. QUALITY (20%): класс ЖК + год + рейтинг Крыши ────────────────
        # Задача 2026-08-14 (Фаза A п.4 вердикт-стратегии, docs/
        # verdict_strategy.md §3.1 "Unknown ≠ average"): раньше при
        # неизвестном классе сюда подставлялся прокси — перцентиль цены/м²
        # объявления В ЕГО ЖЕ СЕГМЕНТЕ (см. историю этого файла) — то есть
        # цена объявления второй раз просачивалась в quality, который сам
        # взвешенно суммируется с price-компонентом (40% веса) в один
        # score_total: скрытая циклическая зависимость, эмпирически
        # подтверждённая baseline-замером (Фаза A п.3) — AUC quality на
        # disappeared_within_30d = 0.517 (случайность), притом что AUC
        # price = 0.871 (реальный сигнал) на той же выборке — прокси не
        # нёс своего сигнала, просто размывал price другим именем.
        # Теперь: класс неизвестен → просто НЕТ вклада от класса (тот же
        # принцип, что уже применялся к неизвестному году/рейтингу ниже —
        # там компонент честно пропускается, не подменяется), доля класса
        # не заполняется — confidence падает симметрично (см. conf ниже).
        parts, wq = [], 0.0
        cls_score = _CLASS_SCORE.get(cls_key)
        if cls_score is not None:
            parts.append(cls_score * 0.45)
            wq += 0.45
        yr = l.get("year_built") or cx.get("year_built")
        ys = _year_score(yr)
        if ys is not None:
            parts.append(ys * 0.35)
            wq += 0.35
        rating = cx.get("krisha_rating")
        if rating:
            parts.append(rating / 5 * 100 * 0.20)
            wq += 0.20
        finish_code = l.get("finish_level")
        finish_score = _FINISH_QUALITY_SCORE.get(finish_code)
        if finish_score is not None:
            parts.append(finish_score * _W_FINISH_IN_QUALITY)
            wq += _W_FINISH_IN_QUALITY
        class_unknown = cls_score is None
        if wq:
            quality_score = round(sum(parts) / wq)
            q_bits = []
            if cls_score is not None:
                q_bits.append(f"класс «{cls_key}»")
            else:
                q_bits.append("класс ЖК не известен")
            if ys is not None:
                q_bits.append(f"{yr} г.")
            if rating:
                q_bits.append(f"⭐ {rating}")
            if finish_score is not None:
                q_bits.append(f"отделка «{_FINISH_LABEL.get(finish_code, finish_code)}»")
            quality_txt = ", ".join(q_bits)
        else:
            quality_score, quality_txt = 50, "нет данных о ЖК (дефолт)"

        # ── 4. MARKET (15%): yield + ликвидность ────────────────────────────
        yp = float(l.get("yield_pct") or 0)
        yield_sc = _clamp(round(yp / 15 * 100))
        supply = l.get("same_complex_cnt") or 1
        supply_sc = 90 if supply <= 3 else (60 if supply <= 8 else 30)
        age = l.get("first_seen_days")
        age_sc = (90 if age <= 7 else 60 if age <= 30 else 35) if age is not None else 50
        market_score = round(yield_sc * 0.6 + (supply_sc * 0.5 + age_sc * 0.5) * 0.4)
        market_txt = (f"yield {yp}%" if yp else "нет данных аренды") + \
                     f", в ЖК {supply} объявл."

        # ── 5. RISK: штрафы — score/text для breakdown (компонент остаётся
        # видимым), но с задачи 2026-08-14 (Фаза A п.6 вердикт-стратегии,
        # docs/verdict_strategy.md §5) НЕ взвешивается в deal (W_RISK=0,
        # см. докстринг функции) — раньше даже серьёзный флаг тонул в
        # общей сумме (было всего 5% веса). Вместо этого risk_bits +
        # два новых сигнала идут отдельным списком flags вердикта ниже.
        risk, risk_bits = 100, []
        fl, flt = l.get("floor"), l.get("floors_total")
        floor_pts = 8  # проекция для legacy score_floor (0-8)
        if fl == 1:
            risk -= 40
            floor_pts = 0
            risk_bits.append("1й этаж")
        elif fl and flt and fl == flt:
            risk -= 25
            floor_pts = 2
            risk_bits.append("последний этаж")
        if l.get("is_owner") is False:
            risk -= 30
            risk_bits.append("риелтор (+комиссия)")
        risk_score = _clamp(risk)
        risk_txt = ", ".join(risk_bits) if risk_bits else "флагов нет"

        # ── Флаги вердикта (Фаза A п.6) — risk_bits как есть (⚠-префикс)
        # + два новых, которых раньше не было явно в UI:
        #  · "мало аналогов" — sources == "только город" означает, что ни
        #    свой дом/ЖК (MIN_BLDG), ни свой гексагон (MIN_HEX), ни кольцо
        #    (MIN_RING, все три — bot/core/hedonic_constants.py) не дали
        #    достаточно аналогов, P_expected целиком держится на городской
        #    медиане — самый слабый локальный сигнал из возможных.
        #  · "класс ЖК не известен" — прямое следствие Фазы A п.4
        #    (class_unknown уже вычислен в блоке QUALITY выше).
        flags = [f"⚠ {b}" for b in risk_bits]
        if sources == "только город":
            flags.append("⚠ мало аналогов")
        if class_unknown:
            flags.append("⚠ класс ЖК не известен")

        deal = round(price_score * W_PRICE + loc_score * W_LOC +
                     quality_score * W_QUALITY + market_score * W_MARKET +
                     risk_score * W_RISK)

        # ── Confidence (= "полнота данных", Фаза A п.5 вердикт-стратегии):
        # сколько компонентов посчитано на реальных данных. Задача
        # 2026-08-14 (Фаза A п.4): убрана частичная надбавка (было +8) за
        # прокси-класс по цене — та надбавка награждала confidence именно
        # за наличие данных для ПОДМЕНЫ сигнала, а не за настоящую
        # полноту; класс неизвестен теперь просто не добавляет 20 баллов,
        # ничем не компенсируется ("Unknown ≠ average" — data_completeness
        # вниз, не подмена, см. docs/verdict_strategy.md §3.1).
        conf = 0
        conf += 30 if (d0 or d1) else 0           # локальная ценовая модель есть
        conf += 10 if d0 else 0                   # свой гексагон не пуст
        conf += 20 if cls_score is not None else 0
        conf += 15 if ys is not None else 0
        conf += 15 if yp else 0
        conf += 10 if rating else 0
        conf = min(conf, 100)

        out[str(l["id"])] = {
            "deal": deal,
            "confidence": conf,
            "di": round(di, 3),
            "expected_m2": round(expected),
            "actual_m2": round(p_m2),
            "hex_n": len(own), "ring_n": len(ring),
            "segment": f"{seg}-комн", "edge_m": edge_m, "sources": sources,
            "flags": flags,
            "components": {
                "price": {"score": price_score, "weight": W_PRICE, "text": price_txt},
                "location": {"score": loc_score, "weight": W_LOC, "text": loc_txt},
                "quality": {"score": quality_score, "weight": W_QUALITY, "text": quality_txt},
                "market": {"score": market_score, "weight": W_MARKET, "text": market_txt},
                "risk": {"score": risk_score, "weight": W_RISK, "text": risk_txt},
            },
            # ── Проекция на legacy-колонки (score_yield и т.п.) — см. docstring.
            # Единственная цель: старые потребители (Telegram-алерты,
            # sheets_sync, попап на карте, аналитика) продолжают показывать
            # breakdown, но теперь СОГЛАСОВАННЫЙ со score_total, а не отдельно
            # посчитанный.
            "legacy": {
                "yield": round(yield_sc / 100 * 20),
                "price_market": round(price_score / 100 * 15),
                "location": 0,  # вес обнулён (W_LOC=0) — в total не участвует, не показываем и в breakdown
                "apt_type": _apt_type_pts(l.get("rooms"), l.get("area")),
                "floor": floor_pts,
                "complex": round(quality_score / 100 * 15),
                "supply": round(supply_sc / 100 * 7),
            },
            "version": 4,
        }
    return out


async def apply_deal_scores() -> int:
    """Считает Deal Score v4 для всех активных вторичных объявлений и
    записывает score_total (=deal), компоненты, confidence и legacy-breakdown
    (score_yield/score_price_market/... — проекция, см. модуль docstring)."""
    from bot.db.pg import fetch, execute
    from bot.db import settings as app_settings

    await execute("ALTER TABLE apartment_listings ADD COLUMN IF NOT EXISTS deal_confidence INT")

    edge = float(app_settings.get_int("HEX_EDGE_M", 50))
    weights = {
        "price": app_settings.get_float("SCORE_W_PRICE", 40),
        "location": app_settings.get_float("SCORE_W_LOCATION", 0),
        "quality": app_settings.get_float("SCORE_W_QUALITY", 20),
        "market": app_settings.get_float("SCORE_W_MARKET", 15),
        "risk": app_settings.get_float("SCORE_W_RISK", 5),
    }

    # same_complex_cnt раньше считался коррелированным подзапросом на КАЖДУЮ
    # строку (O(n²) full-scan) — это и есть причина, по которой apply_deal_scores
    # тихо падал по 30с command_timeout в ~83% циклов (см. "hex price failed"
    # в apartments.log без текста ошибки — пустой TimeoutError). Тот же паттерн
    # уже был исправлен для /admin/api/map-points?type=rental (см. migrations/
    # 016_apt_complex_name_lower_idx.sql) — здесь применяем то же решение:
    # один GROUP BY вместо N подзапросов, дальше просто dict-lookup в Python.
    complex_counts_rows = await fetch("""
        SELECT lower(trim(complex_name)) AS cx, COUNT(*) AS cnt
        FROM apartment_listings
        WHERE is_active IS NOT FALSE AND complex_name IS NOT NULL
          AND btrim(complex_name) != ''
        GROUP BY 1
    """)
    complex_counts = {r["cx"]: r["cnt"] for r in complex_counts_rows}

    rows = await fetch("""
        SELECT id, lat, lon, price, area, rooms, floor, floors_total,
               year_built, complex_name, is_owner, district, yield_pct,
               ceiling_height, resolved_house_id, finish_level,
               EXTRACT(EPOCH FROM (now() - first_seen)) / 86400 AS first_seen_days
        FROM apartment_listings a
        WHERE is_active IS NOT FALSE
          AND COALESCE(is_duplicate, FALSE) = FALSE
          AND market_type IS DISTINCT FROM 'primary'
          AND price > 0 AND area > 0
    """)
    listings = [dict(r) for r in rows]
    if not listings:
        return 0
    for l in listings:
        cx_key = (l.get("complex_name") or "").strip().lower()
        l["same_complex_cnt"] = complex_counts.get(cx_key, 1)

    # complexes_by_id (задача 2026-08-14, "House-resolution в скоринге") —
    # тот же набор строк, что и complexes ниже, просто дополнительно
    # индексированный по id — см. docstring compute_deal_scores.
    cx_rows = await fetch(
        "SELECT id, name, housing_class, year_built, source_info FROM complexes")
    complexes: dict[str, dict] = {}
    complexes_by_id: dict[int, dict] = {}
    for c in cx_rows:
        si = c["source_info"]
        if isinstance(si, str):
            try:
                si = json.loads(si)
            except ValueError:
                si = {}
        si = si or {}
        kr = si.get("krisha") or {}
        cx_data = {
            "housing_class": c["housing_class"],
            "year_built": c["year_built"],
            "krisha_rating": kr.get("rating"),
        }
        complexes[(c["name"] or "").strip().lower()] = cx_data
        complexes_by_id[c["id"]] = cx_data

    poi_rows = await fetch("SELECT kind, lat, lon FROM city_poi")
    poi_idx = _poi_index_from_rows(poi_rows)

    result = compute_deal_scores(listings, complexes, edge, weights, poi_idx, complexes_by_id)

    updated = 0
    for lid, r in result.items():
        lg = r["legacy"]
        await execute("""
            UPDATE apartment_listings
            SET score_total = $2, deal_confidence = $3,
                hex_deal_index = $4, hex_details = $5::jsonb, hex_price_adj = 0,
                score_yield = $6, score_price_market = $7, score_location = $8,
                score_apt_type = $9, score_floor = $10, score_complex = $11,
                score_supply = $12
            WHERE id = $1
        """, lid, r["deal"], r["confidence"], r["di"],
            json.dumps(r, ensure_ascii=False),
            lg["yield"], lg["price_market"], lg["location"],
            lg["apt_type"], lg["floor"], lg["complex"], lg["supply"])
        updated += 1
    logger.info("deal score v4: %d listings (edge=%dм, weights=%s)",
                updated, int(edge), weights)
    return updated
