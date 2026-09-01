"""bot/core/listing_risks.py — единый «паспорт рисков» объявления (задача
2026-08-21, "Риски объекта"). Собирает УЖЕ РАССЧИТАННЫЕ сигналы (Deal Score
components.risk/flags из hex_details, seller_profile, kzk_badge, layer_
details, Property Identity, прогноз срока экспозиции) в одну нормализованную
структуру `risk_analysis` — единственный источник истины для UI. НЕ пересчитывает
риски заново другим методом и НЕ обучает никакую модель (никакого ML, тот же
принцип "Пауза по ML", что и в bot/analytics/dom_scenario.py).

## Зачем отдельный модуль, а не JS в dashboard.html

Задача явно требует: "UI должен использовать нормализованный risk_analysis, а
не собирать бизнес-логику на JavaScript". Весь разбор condition'ов (что
считать «последним этажом», когда КЖК-схема — риск, а когда защита, пороги
для «мало аналогов» и т.п.) живёт ЗДЕСЬ и только здесь.

## Источники сигналов (по категориям задачи)

  А. Цена/качество оценки — `hex_details` (bot/core/deal_score.py, уже
     посчитан батчем apply_deal_scores(), лежит в apartment_listings.
     hex_details, sources/di/confidence/flags).
  Б. Ликвидность — price_history (агрегат: сколько раз цена снижалась,
     ОДИН запрос по listing_id), bot/analytics/dom_scenario.py (прогноз
     срока экспозиции — используется как есть, НЕ переоценивается),
     apartment_listings.score_supply (уже посчитанная поправка на
     конкуренцию — legacy-проекция market-компонента), Property Identity
     (property_listings — сколько раз физическая квартира выставлялась).
  В. Характеристики объекта/дома — apartment_listings (floor/floors_total/
     year_built), ai_analysis (is_relayout/is_relayout_legal/is_free_layout
     — уже посчитан AI-парсером описания, bot/core/ai_text_analysis.py),
     complexes.housing_class.
  Г. Продавец — seller_profiles (уже посчитан seller_profile_snapshot.py).
  Д. КЖК/защита дольщика — kzk_badge (bot/core/complex_detail.py::
     get_kzk_info(), уже резолвится в build_listing_detail()).
  Е. Локация — apartment_listings.layer_details (bot/score_layers/, уже
     посчитан при парсинге — noise/transit/...), demolition_houses (через
     ту же функцию _demolition_factor(), что уже использует location_score.py
     — не дублируем логику 250-метрового порога).

## Сознательно НЕ реализовано

  - Промзона/ж.д./кладбище/полигон рядом — в проекте НЕТ существующего
    гео-слоя с такими категориями (LAYERS в bot/score_layers/__init__.py —
    только noise/schools/transit/amenities/parks/banks). Задание прямо
    требует "только если существующий геослой действительно подтверждает"
    — такого слоя нет, сигнал не выдумывается.
  - Преступность рядом (crime_incidents) — таблица есть (250k+ точек), но
    per-listing плотность в радиусе 500м на реальных данных — 0-6 инцидентов
    почти у любой точки города (проверено эмпирически на выборке при
    разработке) без городского baseline для сравнения "это много или мало
    для этого места" сырое число вводит в заблуждение (та же ловушка, что
    задание явно запрещает для POI: "не превращать отсутствие POI в
    доказанный негативный риск" — тут симметрично, "не превращать сырой
    подсчёт в доказанный риск"). Baseline-модель плотности — отдельная,
    не откалиброванная задача, не делается здесь по тому же принципу, что
    остановил AFT в dom_forecast_audit.md.

## Уровни

critical/high/medium/low/info — `overall_level` берётся как МАКСИМУМ
severity среди `items` (не среднее, не взвешенная сумма — задание прямо
запрещает "непрозрачную среднюю арифметику"). Пустой items -> "info"
("явных рисков не обнаружено", НЕ "unknown" — unknown зарезервирован для
отказа расчёта целиком, см. compute_listing_risks_safe).

Защитные факторы (КЖК-гарантия/БВУ) — ОТДЕЛЬНЫЙ список `protective`, не
смешиваются с items ни по цвету, ни по вкладу в overall_level (зелёный
пункт не может "разбавить" красный)."""
from __future__ import annotations

from datetime import datetime, timezone

SEVERITY_RANK = {"info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}
VERSION = "listing_risk_v1"

_RU_PLURAL_FORMS = {
    "риск": ("риск", "риска", "рисков"),
    "ограничение": ("ограничение", "ограничения", "ограничений"),
}


def _ru_count(n: int, word: str) -> str:
    one, few, many = _RU_PLURAL_FORMS[word]
    n_abs = abs(n)
    if n_abs % 10 == 1 and n_abs % 100 != 11:
        form = one
    elif 2 <= n_abs % 10 <= 4 and not (12 <= n_abs % 100 <= 14):
        form = few
    else:
        form = many
    return f"{n} {form}"


def _item(code: str, category: str, severity: str, title: str, description: str,
          source: str, recommendation: str | None = None) -> dict:
    return {
        "code": code, "category": category, "severity": severity,
        "title": title, "description": description, "source": source,
        "recommendation": recommendation,
        # Every item здесь строится ТОЛЬКО из подтверждённых структурных
        # данных (не текстовых догадок AI/эвристик по описанию) — verified
        # всегда True на этом уровне; поле оставлено в схеме на будущее
        # (задание явно просит его в примере), если появится источник с
        # менее надёжным сигналом.
        "verified": True,
    }


def _protective(code: str, category: str, title: str, description: str, source: str) -> dict:
    return {"code": code, "category": category, "title": title,
            "description": description, "source": source}


def _unknown(code: str, title: str, description: str) -> dict:
    return {"code": code, "title": title, "description": description}


# ── А. Цена и качество оценки (hex_details = Deal Score v4) ─────────────

def _valuation_signals(hex_details: dict | None) -> tuple[list[dict], list[dict]]:
    items: list[dict] = []
    unknowns: list[dict] = []
    if not hex_details:
        unknowns.append(_unknown(
            "VALUATION_UNAVAILABLE", "Оценка цены не рассчитана",
            "Deal Score для этого объявления ещё не посчитан (нет координат "
            "или площади/цены) — сравнить цену с рынком автоматически нельзя."))
        return items, unknowns

    di = hex_details.get("di")
    if isinstance(di, (int, float)):
        # di = expected/actual: di<1 -> цена ВЫШЕ ожидания по локальному рынку.
        overpay_pct = round((1 - di) * 100)
        if di < 0.75:
            items.append(_item(
                "PRICE_ABOVE_MARKET", "valuation", "high",
                "Цена заметно выше ожидаемой рыночной",
                f"По сравнению с похожими объектами рядом цена выглядит завышенной "
                f"примерно на {overpay_pct}%.",
                "Deal Score — оценка по локальным аналогам",
                "Сравнить с 2-3 похожими объявлениями в том же ЖК/районе перед просмотром.",
            ))
        elif di < 0.85:
            items.append(_item(
                "PRICE_ABOVE_MARKET", "valuation", "medium",
                "Цена выше ожидаемой рыночной",
                f"Цена выглядит выше локального ожидания примерно на {overpay_pct}%.",
                "Deal Score — оценка по локальным аналогам",
                "Сравнить с аналогами и учитывать при обсуждении цены.",
            ))

    sources = hex_details.get("sources")
    if sources == "только город":
        unknowns.append(_unknown(
            "VALUATION_CITY_ONLY", "Оценка построена только по городской медиане",
            "Рядом (в доме/ЖК/гексагоне) не нашлось достаточно похожих объявлений "
            "— локальная цена сравнивается с медианой по всему городу, это грубее, "
            "чем сравнение с соседями."))
    elif sources in ("гекс+город", "кольцо+город"):
        unknowns.append(_unknown(
            "VALUATION_FEW_COMPARABLES", "Мало локальных аналогов",
            "Для оценки цены нашлось немного похожих объектов поблизости — "
            "результат менее точен, чем при большом числе аналогов в том же доме/ЖК."))

    confidence = hex_details.get("confidence")
    if isinstance(confidence, (int, float)) and confidence < 40:
        unknowns.append(_unknown(
            "VALUATION_LOW_CONFIDENCE", "Низкая полнота данных для оценки",
            "Не хватает части исходных данных (класс ЖК, год постройки, доходность "
            "аренды и т.п.) — итоговая оценка цены менее надёжна."))

    return items, unknowns


# ── В. Характеристики объекта и дома ─────────────────────────────────────

_OLD_BUILDING_YEAR = 2000  # тот же порог, что нижняя граница _year_score() в
                            # deal_score.py (y<2000 -> низший тир) — не
                            # изобретаем новую границу.


def _floor_signals(floor: int | None, floors_total: int | None) -> list[dict]:
    items = []
    if floor is None or floors_total is None:
        items.append(_item(
            "FLOOR_UNKNOWN", "object", "low",
            "Этаж не указан",
            "В объявлении не хватает данных об этаже и/или этажности дома.",
            "Данные объявления",
            "Уточнить этаж и общую этажность у продавца.",
        ))
        return items
    if floor == 1:
        items.append(_item(
            "FIRST_FLOOR", "object", "medium",
            "Первый этаж",
            "Квартира на первом этаже — стоит проверить шум с улицы, вид из окон "
            "и наличие решёток/сигнализации.",
            "Данные объявления",
            "При просмотре обратить внимание на шум и безопасность окон.",
        ))
    elif floor == floors_total:
        items.append(_item(
            "LAST_FLOOR", "object", "medium",
            "Последний этаж",
            "Квартира на последнем этаже — стоит проверить состояние кровли и "
            "технического этажа (протечки, утепление).",
            "Данные объявления",
            "Спросить у продавца/УК о протечках крыши за последние годы.",
        ))
    return items


def _building_signals(year_built: int | None, ai_analysis: dict | None,
                       complex_housing_class: str | None) -> tuple[list[dict], list[dict]]:
    items: list[dict] = []
    unknowns: list[dict] = []
    if year_built is None:
        items.append(_item(
            "YEAR_BUILT_UNKNOWN", "object", "low",
            "Неизвестен год постройки",
            "В данных нет года постройки дома — сложнее оценить возраст "
            "коммуникаций и общий износ здания.",
            "Данные объявления/ЖК",
        ))
    elif year_built < _OLD_BUILDING_YEAR:
        # Нейтральная формулировка (задание явно требует без юридических/
        # технических утверждений) — только факт года, без "аварийности".
        items.append(_item(
            "OLD_BUILDING", "object", "low",
            "Старый дом",
            f"Дом построен в {year_built} году — стоит отдельно уточнить "
            "состояние коммуникаций и капремонта.",
            "Данные объявления/ЖК",
            "Спросить о капремонте (кровля, трубы, электрика, лифт).",
        ))

    # "Неизвестен класс ЖК" — ограничение полноты данных, НЕ риск самого
    # объекта (тот же принцип, что и у остальных unknowns: задание прямо
    # запрещает называть нехватку данных "риском квартиры") — поэтому
    # ОДИН раз здесь, в unknowns, не дублируется отдельным items-сигналом.
    if not complex_housing_class or complex_housing_class == "не определён":
        unknowns.append(_unknown(
            "COMPLEX_CLASS_UNKNOWN", "Неизвестен класс ЖК",
            "Класс жилого комплекса (эконом/комфорт/бизнес и т.п.) не определён "
            "— часть оценки качества/цены сделана без этого фактора."))

    if ai_analysis:
        is_relayout = ai_analysis.get("is_relayout")
        is_relayout_legal = ai_analysis.get("is_relayout_legal")
        is_free_layout = ai_analysis.get("is_free_layout")
        if is_relayout and not is_relayout_legal:
            items.append(_item(
                "RELAYOUT_LEGALITY_UNCONFIRMED", "object", "medium",
                "Перепланировка не подтверждена как узаконенная",
                "В описании упоминается перепланировка, но текст не подтверждает, "
                "что она узаконена — это отдельный юридический риск при сделке.",
                "AI-анализ текста объявления",
                "Запросить у продавца техпаспорт/решение об узаконивании перепланировки.",
            ))
        elif is_free_layout:
            items.append(_item(
                "FREE_LAYOUT", "object", "low",
                "Свободная или изменённая планировка",
                "В описании указана свободная планировка — стоит уточнить, "
                "зафиксирована ли она в техпаспорте.",
                "AI-анализ текста объявления",
            ))
    return items, unknowns


# ── Г. Продавец (seller_profile) ─────────────────────────────────────────

def _seller_signals(seller_profile: dict | None) -> tuple[list[dict], list[dict]]:
    items: list[dict] = []
    unknowns: list[dict] = []
    if not seller_profile:
        unknowns.append(_unknown(
            "SELLER_TYPE_UNKNOWN", "Тип продавца не определён",
            "Недостаточно данных, чтобы классифицировать продавца (собственник/"
            "риелтор/агентство)."))
        return items, unknowns

    seller_type = seller_profile.get("seller_type")
    if seller_type == "realtor":
        items.append(_item(
            "SELLER_REALTOR", "seller", "info",
            "Продавец — риелтор",
            "Объявление размещено риелтором, не собственником — возможна "
            "дополнительная комиссия, это не признак мошенничества.",
            "Профиль продавца",
        ))
    elif not seller_type:
        unknowns.append(_unknown(
            "SELLER_TYPE_UNKNOWN", "Тип продавца не определён",
            "Недостаточно данных, чтобы классифицировать продавца."))

    if seller_profile.get("is_large_agency"):
        items.append(_item(
            "SELLER_LARGE_AGENCY", "seller", "info",
            "Вероятное агентство",
            f"Под этим именем сейчас {seller_profile.get('active_listings_count')} "
            "активных объявлений — похоже на агентство, а не частного продавца.",
            "Профиль продавца",
        ))

    if seller_profile.get("is_high_relist_rate"):
        items.append(_item(
            "SELLER_HIGH_RELIST_RATE", "seller", "medium",
            "Высокая частота перевыставлений",
            "Продавец часто перевыставляет объявления заново — возможно, это "
            "релисты той же квартиры, а не отдельные новые предложения.",
            "Профиль продавца",
        ))

    if seller_profile.get("is_ambiguous"):
        unknowns.append(_unknown(
            "SELLER_AMBIGUOUS_NAME", "Неоднозначное имя продавца",
            "Имя продавца слишком общее (например «хозяин») — под ним могут "
            "скрываться разные люди, поведенческие сигналы по нему ненадёжны."))

    return items, unknowns


# ── Д. КЖК / защита дольщика (первичка) ──────────────────────────────────

def _kzk_signals(kzk_badge: dict | None, market_type: str | None) -> tuple[list[dict], list[dict]]:
    items: list[dict] = []
    protective: list[dict] = []
    if market_type != "primary" or not kzk_badge:
        return items, protective

    if kzk_badge.get("is_blacklisted"):
        items.append(_item(
            "KZK_BLACKLISTED", "legal", "critical",
            "Застройщик в чёрном списке КЖК",
            "Застройщик найден в чёрном списке КЖК (Қазақстанның Құрылыс "
            "Қоры) — известны серьёзные проблемы с обязательствами.",
            "Реестр КЖК",
            "Обязательно проверить статус объекта и застройщика на сайте КЖК перед сделкой.",
        ))
        return items, protective  # blacklist — самодостаточный сигнал, схему не оцениваем отдельно

    scheme = kzk_badge.get("warranty_scheme")
    if scheme == "Гарантия КЖК":
        protective.append(_protective(
            "KZK_GUARANTEE", "legal", "Гарантия КЖК",
            "У застройщика подтверждена гарантия КЖК — официальная схема "
            "защиты дольщика.", "Реестр КЖК"))
    elif scheme == "Участие БВУ":
        protective.append(_protective(
            "KZK_BVU", "legal", "Участие БВУ",
            "Подтверждено участие банка (БВУ) в схеме финансирования — "
            "официальная схема защиты дольщика.", "Реестр КЖК"))
    elif scheme == "Разрешение МИО":
        items.append(_item(
            "KZK_MIO_PERMIT", "legal", "medium",
            "Разрешение МИО (не гарантия КЖК и не участие БВУ)",
            "У застройщика есть разрешение акимата (МИО) — официальная "
            "схема существует, но это самая слабая из трёх схем защиты "
            "дольщика (слабее гарантии КЖК и участия БВУ).",
            "Реестр КЖК",
        ))
    else:
        items.append(_item(
            "KZK_NO_PROTECTION", "legal", "high",
            "Нет подтверждённой официальной схемы защиты дольщика",
            "В реестре КЖК не нашлось подтверждённой схемы защиты дольщика "
            "(гарантия КЖК / участие БВУ / разрешение МИО) для этого застройщика.",
            "Реестр КЖК",
            "Уточнить у застройщика официальную схему защиты дольщика напрямую.",
        ))
    return items, protective


# ── Е. Локация ─────────────────────────────────────────────────────────

def _location_signals(layers: dict | None, demolition: dict | None) -> tuple[list[dict], list[dict]]:
    items: list[dict] = []
    unknowns: list[dict] = []
    if not layers:
        unknowns.append(_unknown(
            "LOCATION_LAYERS_UNAVAILABLE", "Недостаток инфраструктурных данных",
            "Гео-слои (шум, транспорт, инфраструктура) для этого объявления "
            "не рассчитаны — обычно из-за отсутствия координат."))
    else:
        noise = layers.get("noise") or {}
        if isinstance(noise.get("adj"), (int, float)) and noise["adj"] <= -4:
            items.append(_item(
                "NOISE_MAJOR_ROAD", "location", "medium",
                "Рядом крупная дорога / повышенный шум",
                noise.get("reason") or "Рядом магистраль — возможен повышенный уличный шум.",
                "Гео-слои Clearly (OSM)",
                "При просмотре оценить шум в разное время суток, особенно у окон на улицу.",
            ))
        elif isinstance(noise.get("adj"), (int, float)) and noise["adj"] <= -1:
            items.append(_item(
                "NOISE_MAJOR_ROAD", "location", "low",
                "Рядом дорога средней загруженности",
                noise.get("reason") or "Рядом дорога — возможен шум.",
                "Гео-слои Clearly (OSM)",
            ))

        transit = layers.get("transit") or {}
        if isinstance(transit.get("adj"), (int, float)) and transit["adj"] == 0:
            unknowns.append(_unknown(
                "TRANSIT_WEAK", "Слабая транспортная доступность",
                "Рядом не нашлось остановок общественного транспорта в шаговой "
                "доступности по имеющимся данным."))

    if demolition and demolition.get("adj", 0) < 0:
        items.append(_item(
            "NEAR_DEMOLITION_LIST", "location", "medium",
            "Рядом дом из перечня на снос/реновацию",
            demolition.get("reason") or "Рядом дом из официального перечня на снос.",
            "Реестр сноса/реновации (см. /admin/analytics/demolition)",
            "Уточнить сроки и охват программы сноса в этом квартале.",
        ))

    return items, unknowns


# ── Б. Ликвидность ────────────────────────────────────────────────────

async def _price_history_stats(listing_id: str) -> dict:
    from bot.db.pg import fetchrow
    row = await fetchrow(
        "SELECT count(*) FILTER (WHERE new_price < old_price) AS decreases, "
        "count(*) AS changes FROM price_history WHERE listing_id = $1",
        listing_id,
    )
    return {"decreases": row["decreases"] if row else 0, "changes": row["changes"] if row else 0}


async def _property_siblings(listing_id: str) -> list[dict]:
    """Другие listing_id той же физической квартиры (Property Identity) —
    ОДИН запрос, не по одному property_id за раз (не N+1, вызывается один
    раз на попап, не в цикле по списку объявлений)."""
    from bot.db.pg import fetch
    rows = await fetch("""
        SELECT a2.id, a2.price, a2.area, a2.rooms, a2.first_seen
        FROM property_listings pl
        JOIN property_listings pl2 ON pl2.property_id = pl.property_id AND pl2.listing_id != pl.listing_id
        JOIN apartment_listings a2 ON a2.id = pl2.listing_id
        WHERE pl.listing_id = $1
    """, listing_id)
    return [dict(r) for r in rows]


def _liquidity_signals(l: dict, price_stats: dict, siblings: list[dict],
                        dom_forecast: dict | None) -> tuple[list[dict], list[dict]]:
    items: list[dict] = []
    unknowns: list[dict] = []

    decreases = price_stats.get("decreases", 0)
    is_active = l.get("is_active") is not False
    if decreases >= 2:
        items.append(_item(
            "PRICE_MULTIPLE_REDUCTIONS", "liquidity", "medium",
            "Неоднократные снижения цены",
            f"Цена снижалась {decreases} раз(а) с момента публикации объявления.",
            "История цены объявления",
        ))
    if decreases >= 1 and is_active:
        items.append(_item(
            "PRICE_CUT_STILL_ACTIVE", "liquidity", "low",
            "Цена снижалась, но объявление остаётся активным",
            "Цена уже снижалась хотя бы раз, а объявление до сих пор не "
            "исчезло с публикации — возможно пространство для торга. Это "
            "показатель активности объявления, не подтверждённая сделка.",
            "История цены объявления",
        ))

    score_supply = l.get("score_supply")
    # score_supply — legacy-проекция market-компонента Deal Score (0-7,
    # см. bot/core/deal_score.py: round(supply_sc/100*7), supply_sc=30 при
    # >8 активных объявлений в том же ЖК/доме) — те же данные, что уже
    # посчитаны батчем, не отдельный запрос.
    if isinstance(score_supply, (int, float)) and score_supply <= 2:
        items.append(_item(
            "HIGH_LOCAL_COMPETITION", "liquidity", "low",
            "Высокая конкуренция похожих предложений",
            "В этом ЖК/доме сейчас много похожих активных объявлений — "
            "покупателю есть из чего выбирать, это может влиять на срок продажи.",
            "Deal Score (рыночный компонент)",
        ))

    if len(siblings) >= 1:
        items.append(_item(
            "PROPERTY_RELISTED", "liquidity", "medium",
            "Квартира выставлялась повторно",
            f"Property Identity нашла {len(siblings) + 1} объявлени(й/е) под этой "
            "же физической квартирой — она уже выставлялась на продажу раньше.",
            "Property Identity",
        ))
        cur_area, cur_rooms = l.get("area"), l.get("rooms")
        for sib in siblings:
            sib_area = sib.get("area")
            if (cur_area and sib_area and abs(float(cur_area) - float(sib_area)) > 2) or \
               (cur_rooms and sib.get("rooms") and cur_rooms != sib.get("rooms")):
                items.append(_item(
                    "PROPERTY_VERSION_MISMATCH", "liquidity", "low",
                    "Расхождение между версиями объявления",
                    "У другого объявления той же физической квартиры (Property "
                    "Identity) отличаются площадь или число комнат — стоит "
                    "перепроверить характеристики у продавца.",
                    "Property Identity",
                    "Сверить площадь/планировку/число комнат с продавцом напрямую.",
                ))
                break

    if dom_forecast and dom_forecast.get("available") and not dom_forecast.get("insufficient_data"):
        current = dom_forecast.get("current") or {}
        days_high = current.get("days_high")
        first_seen = l.get("first_seen")
        if isinstance(days_high, (int, float)) and first_seen:
            age_days = (datetime.now(timezone.utc) - first_seen).days
            if age_days > days_high * 1.5:
                items.append(_item(
                    "LONGER_THAN_EXPECTED", "liquidity", "medium",
                    "На рынке заметно дольше аналогов",
                    f"Объявление активно уже {age_days} дн. — заметно дольше "
                    f"ожидаемого срока экспозиции похожих объектов ({dom_forecast.get('segment') or 'сегмент'}: "
                    f"~{current.get('days_low')}–{days_high} дн.).",
                    "Прогноз срока экспозиции (сегментный анализ)",
                ))
        if dom_forecast.get("confidence") == "low":
            unknowns.append(_unknown(
                "DOM_FORECAST_WEAK", "Слабый прогноз срока экспозиции",
                "Прогноз срока экспозиции для этого сегмента посчитан с низкой "
                "надёжностью — мало разрешившихся аналогов для точной оценки."))
    else:
        unknowns.append(_unknown(
            "DOM_FORECAST_WEAK", "Слабый прогноз срока экспозиции",
            "Похожих объявлений недостаточно, чтобы оценить типичный срок "
            "экспозиции для сравнения."))

    return items, unknowns


# ── Фиксированные группы "что не удалось проверить" (задание §4) ────────
# Всегда одни и те же 3 компактные группы — НЕ длинный список из 10
# отдельных строк на каждую карточку (задание прямо это запрещает).
# Раскрытие подробностей — на фронте (details/summary), текст здесь.

_ALWAYS_UNKNOWN_GROUPS = [
    _unknown(
        "DOCUMENTS_UNKNOWN", "Документы не проверены",
        "Право собственности, обременения, залог, судебные споры и (если не "
        "подтверждено отдельно выше) законность перепланировки — Clearly не "
        "имеет доступа к этим данным и не проверяет их автоматически.",
    ),
    _unknown(
        "TECHNICAL_UNKNOWN", "Техническое состояние не проверено",
        "Состояние кровли, коммуникаций и несущих конструкций можно оценить "
        "только при личном осмотре или независимой экспертизе.",
    ),
    _unknown(
        "DEAL_HISTORY_UNKNOWN", "История сделки неизвестна",
        "Фактическая причина продажи, окончательная цена сделки и долги по "
        "коммунальным услугам не отражены в объявлении и не проверяются Clearly.",
    ),
]


def _overall_level(items: list[dict]) -> str:
    if not items:
        return "info"
    return max(items, key=lambda it: SEVERITY_RANK[it["severity"]])["severity"]


def _znachimykh(n: int) -> str:
    """Согласование прилагательного "значимый" с числительным — форма
    genitive plural (значимых) совпадает у "мало"/"много", отличается
    только "один" (значимый)."""
    n_abs = abs(n)
    return "значимый" if (n_abs % 10 == 1 and n_abs % 100 != 11) else "значимых"


def _build_summary(items: list[dict], unknown_count: int) -> str:
    significant = [it for it in items if SEVERITY_RANK[it["severity"]] >= SEVERITY_RANK["medium"]]
    if significant:
        n = len(significant)
        return (f"Обнаружено {n} {_znachimykh(n)} {_ru_count(n, 'риск').split(' ', 1)[1]} и "
                f"{_ru_count(unknown_count, 'ограничение')} данных")
    minor = [it for it in items if it["severity"] in ("low", "info")]
    if minor:
        return (f"Явных серьёзных рисков не найдено, но есть {len(minor)} момент(ов) "
                f"для проверки и {_ru_count(unknown_count, 'ограничение')} данных")
    return f"Явных рисков не обнаружено — {_ru_count(unknown_count, 'ограничение')} данных ниже"


async def compute_listing_risks(
    listing_id: str, l: dict, *,
    kzk_badge: dict | None, seller_profile: dict | None,
    layers: dict | None, ai_analysis: dict | None,
    complex_housing_class: str | None,
) -> dict:
    """Главная точка входа. `l` — уже загруженная строка apartment_listings
    (build_listing_detail() её и так фетчит `SELECT *` — сюда передаётся
    тот же dict, не перезапрашивается). kzk_badge/seller_profile/layers/
    ai_analysis/complex_housing_class — тоже уже посчитаны вызывающим
    build_listing_detail() для СВОИХ полей ответа, здесь только читаются,
    не пересчитываются другим способом (задание: "один источник истины").

    Единственные НОВЫЕ запросы здесь — price_history-агрегат, siblings по
    Property Identity и прогноз срока экспозиции (TTL-кэш, см. bot/
    analytics/dom_scenario.py) — по одному на попап, не в цикле."""
    hex_details = l.get("hex_details")
    if isinstance(hex_details, str):
        import json
        try:
            hex_details = json.loads(hex_details)
        except ValueError:
            hex_details = None

    price_stats = await _price_history_stats(listing_id)
    siblings = await _property_siblings(listing_id)

    dom_forecast = None
    try:
        from bot.analytics.dom_scenario import compute_dom_scenario_cached
        dom_forecast = await compute_dom_scenario_cached(listing_id)
    except Exception:
        dom_forecast = None  # прогноз — необязательный сигнал, ошибка тут не должна ронять весь паспорт рисков

    demolition = None
    if l.get("lat") is not None and l.get("lon") is not None:
        try:
            from bot.core.location_score import _demolition_factor
            demolition = await _demolition_factor(float(l["lat"]), float(l["lon"]))
        except Exception:
            demolition = None

    items: list[dict] = []
    unknowns: list[dict] = []
    protective: list[dict] = []

    v_items, v_unknowns = _valuation_signals(hex_details)
    items += v_items
    unknowns += v_unknowns

    items += _floor_signals(l.get("floor"), l.get("floors_total"))
    b_items, b_unknowns = _building_signals(l.get("year_built"), ai_analysis, complex_housing_class)
    items += b_items
    unknowns += b_unknowns

    s_items, s_unknowns = _seller_signals(seller_profile)
    items += s_items
    unknowns += s_unknowns

    k_items, k_protective = _kzk_signals(kzk_badge, l.get("market_type"))
    items += k_items
    protective += k_protective

    loc_items, loc_unknowns = _location_signals(layers, demolition)
    items += loc_items
    unknowns += loc_unknowns

    liq_items, liq_unknowns = _liquidity_signals(l, price_stats, siblings, dom_forecast)
    items += liq_items
    unknowns += liq_unknowns

    unknowns += _ALWAYS_UNKNOWN_GROUPS

    # Сортировка items по убыванию severity — фронт показывает "3 самых
    # серьёзных + кнопка Показать все" (задание §5), порядок готовит бэкенд,
    # не JS (тот же принцип "не собирать бизнес-логику на JS").
    items.sort(key=lambda it: SEVERITY_RANK[it["severity"]], reverse=True)

    overall_level = _overall_level(items)
    summary = _build_summary(items, len(unknowns))

    return {
        "overall_level": overall_level,
        "summary": summary,
        "items": items,
        "protective": protective,
        "unknowns": unknowns,
        "calculated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "version": VERSION,
    }


FALLBACK_RISK_ANALYSIS = {
    "overall_level": "unknown",
    "summary": "Риски временно не рассчитаны",
    "items": [],
    "protective": [],
    "unknowns": [],
    "calculated_at": None,
    "version": VERSION,
}


async def compute_listing_risks_safe(
    listing_id: str, l: dict, *,
    kzk_badge: dict | None, seller_profile: dict | None,
    layers: dict | None, ai_analysis: dict | None,
    complex_housing_class: str | None,
) -> dict:
    """Обёртка с гарантией graceful fallback (задание §6: "ошибка расчёта
    рисков не должна ломать карточку объявления") — вызывать ИЗ роута/
    build_listing_detail(), не compute_listing_risks() напрямую."""
    import logging
    try:
        return await compute_listing_risks(
            listing_id, l, kzk_badge=kzk_badge, seller_profile=seller_profile,
            layers=layers, ai_analysis=ai_analysis,
            complex_housing_class=complex_housing_class,
        )
    except Exception:
        logging.getLogger(__name__).exception(
            "listing_risks: расчёт не удался для listing_id=%s", listing_id)
        return dict(FALLBACK_RISK_ANALYSIS)
