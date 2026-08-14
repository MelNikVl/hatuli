"""Общие константы hedonic-ядра "аналоги по гекс+кольцо+ЖК+площадь" —
задача 2026-08-14 (Часть 2, п.9: "скоринг волна 2"). Используются
независимо bot/core/bargain.py (get_comparables — async, живой запрос к
БД на одно объявление) и bot/core/deal_score.py (compute_deal_scores —
batch-агрегация по всей активной вторичке разом) — архитектуры разные
(один запрашивает БД по требованию, другой строит словари в памяти по
уже выбранным строкам), поэтому общего КОДА аналогов тут нет, но сами
пороги/веса — те же числа, раньше синхронизировались вручную комментарием
в каждом файле по отдельности.

_activity_filter() (задача 2026-08-14, "as_of для score_total, минимальный
план") — вынесена сюда из bargain.py по тому же принципу: и bargain.py, и
deal_score.py теперь фильтруют аналоги по активности через одну функцию,
не две независимые копии условия "is_active IS NOT FALSE" / "было активно
на as_of", которые могли бы разойтись так же, как AREA_BAND_PCT/MIN_BLDG
до этого файла.

Живой инцидент, из-за которого это стало задачей (см. docs/
scoring_audit.md, раздел про двойной счёт): кейс #1014506231 "Landmark" —
до синхронизации AREA_BAND_PCT/MIN_BLDG вручную комментарием, bargain.py
и deal_score.py одновременно показывали "Недооценено на 48%" и
"переоценена на 43%" для ОДНОГО И ТОГО ЖЕ объявления на одной странице,
из-за рассинхрона фильтра по площади. Импорт из одного модуля не даёт
двум файлам физически разойтись в будущем, комментарий-синхронизация —
могла.

MIN_COMPARABLES (bargain.py, было своим числом) семантически ближе всего
к MIN_RING — "хватает ли аналогов гекс+кольцо вместе, чтобы доверять
оценке" — в deal_score.py та же роль у отдельного порога MIN_RING,
используемого для оценки достаточности кольца. У deal_score.py есть ещё
MIN_HEX (свой гексагон отдельно, без кольца) — этому в bargain.py прямого
аналога нет (там гекс+кольцо всегда считаются вместе, не по отдельности).

_CLASS_SCORE/_class_key/_FINISH_QUALITY_SCORE/_FINISH_LABEL (задача
2026-08-14, "Фаза B: comparable_score core") — перенесены сюда из
deal_score.py: теперь нужны ВТОРОМУ модулю (bot/core/comparable_score.py,
housing_class_similarity/finish_level_similarity), не только deal_score.py
(quality-компонент) — тот же принцип централизации, что уже применён к
AREA_BAND_PCT/MIN_BLDG/_activity_filter выше, не копия таксономии в двух
местах, которая может разойтись (класс "элит" в comparable_score.py и в
deal_score.py обязан значить одно и то же число).

is_active_as_of() — Python-твин _activity_filter() для пар УЖЕ
загруженных словарей (comparable_score.py сравнивает два listing-dict,
не делает SQL-запрос сам) — та же троичная логика, тот же смысл, другая
форма (булева проверка, не SQL-фрагмент) — намеренно не сведена в одну
функцию с _activity_filter (разные потребители: SQL-строитель vs
Python-предикат), но сверяется с ней тестами.
"""
from __future__ import annotations

from datetime import datetime
from functools import lru_cache

# ±15% площади — тот же метраж считается сопоставимым (аналог/сравнение).
AREA_BAND_PCT = 0.15

# Минимум аналогов В ТОМ ЖЕ доме/ЖК (той же площади ±AREA_BAND_PCT) —
# при достаточном количестве используются ТОЛЬКО они, точнее любого
# геометрического гекс-соседства (bargain.py: MIN_SAME_COMPLEX,
# deal_score.py: MIN_BLDG — то же число, тот же смысл).
MIN_BLDG = 3

# deal_score.py: минимум аналогов В СВОЁМ гексагоне (без кольца) —
# у bargain.py прямого аналога нет (см. докстринг модуля).
MIN_HEX = 3

# Минимум аналогов гекс+кольцо вместе — общий порог "хватает данных,
# чтобы доверять локальной оценке, не откатываться на город/район"
# (bargain.py: MIN_COMPARABLES, deal_score.py: MIN_RING).
MIN_RING = 5

# Веса гекс/кольцо/город в hedonic-блендинге ожидаемой цены (deal_score.py
# — bargain.py не блендит, использует одно из трёх как есть по методу).
W0, W1, W2 = 1.0, 0.7, 0.35


def _activity_filter(as_of: datetime | None, param_idx: int, alias: str = "") -> tuple[str, list]:
    """SQL-фрагмент фильтра активности для аналогов — изначально задача
    2026-08-14 (Фаза A.5 п.1 вердикт-стратегии, docs/verdict_strategy.md),
    вынесена сюда из bargain.py задачей "as_of для score_total,
    минимальный план" (тот же день) — используется и bargain.py::
    get_comparables(), и deal_score.py::compute_deal_scores()/
    apply_deal_scores(), одна функция вместо двух копий условия.

    as_of=None (по умолчанию — весь текущий живой вызов, "сейчас"):
    текущий is_active, архив не участвует.
    as_of=дата: точечная реконструкция "было активно НА ЭТУ ДАТУ"
    (first_seen <= as_of И (ещё не архивировано ИЛИ архивировано позже
    as_of)) — НЕ текущий is_active, для честного backtesting/снапшотов,
    где текущее состояние БД (объявление могло уйти в архив уже после
    интересующей даты) не совпадает с состоянием на момент, который
    анализируется.

    **Известное ограничение** (задокументировано, не решено этой задачей):
    сама таблица apartment_listings хранит только ТЕКУЩИЕ price/area/
    rooms и т.п. для КАЖДОЙ строки — реконструкция набора ID, которые
    были активны на as_of, честная, но их price/area в выдаче — сегодняшние,
    не те, что были на as_of (площадь/комнаты после публикации практически
    не меняются, но цена — меняется, история есть в price_history, эта
    функция её не джойнит). Для комнатности/площади это не проблема; для
    цены — приближение, допустимое для старта backtest'а (см. deal_score_
    backtest.py), не полная историческая точность.
    """
    p = f"{alias}." if alias else ""
    if as_of is None:
        return f"AND {p}is_active IS NOT FALSE", []
    return (f"AND {p}first_seen <= ${param_idx} "
            f"AND ({p}archived_at IS NULL OR {p}archived_at > ${param_idx})", [as_of])


def is_active_as_of(first_seen: datetime | None, archived_at: datetime | None,
                     as_of: datetime | None, is_active: bool | None = None) -> bool:
    """Python-эквивалент _activity_filter() для уже загруженных пар
    (comparable_score.py) — та же троичная логика:
    as_of=None — текущий is_active (передан явно вызывающим, НЕ выводится
    из first_seen/archived_at — is_active остаётся источником истины для
    ЖИВОГО состояния, тот же принцип, что в SQL-версии; если не передан,
    честно считаем активным — вызывающий сам решает, нужен ли ему этот
    параметр вовсе).
    as_of=дата — first_seen<=as_of И (ещё не архивировано ИЛИ
    архивировано позже as_of), НЕ текущий is_active.
    """
    if as_of is None:
        return True if is_active is None else bool(is_active)
    if first_seen is None or first_seen > as_of:
        return False
    if archived_at is not None and archived_at <= as_of:
        return False
    return True


# ── Класс ЖК: элит/бизнес/комфорт/эконом ────────────────────────────────
# Задача 2026-08-14 ("Фаза B: comparable_score core") — перенесено из
# deal_score.py (было там с волны 1, quality-компонент) — теперь общее с
# comparable_score.py (housing_class_similarity), см. докстринг модуля.
_CLASS_SCORE = {"элит": 100, "бизнес": 80, "комфорт": 60, "эконом": 35}


@lru_cache(maxsize=256)
def _class_key(cls: str) -> str | None:
    """Нормализует произвольный текст housing_class к одному из ключей
    _CLASS_SCORE (подстрокой — источники пишут по-разному: "элит-класс",
    "бизнес класс" и т.п.). None, если класс неизвестен/пуст/не узнан.

    @lru_cache — задача 2026-08-14 (Фаза B п.2, интеграция comparable_
    score.py в deal_score.py): живой профайлинг на 30К объявлений показал
    ~660К вызовов за один прогон (comparable_score дергает эту функцию на
    КАЖДУЮ пару, а словарь значений housing_class — маленький словарь
    из нескольких десятков различных строк на весь город) — линейный
    scan по 4 ключам на каждый вызов был чистым повтором одной и той же
    работы. Небольшой конечный словарь строк — безопасный кандидат для
    кэша (не растёт неограниченно, maxsize=256 с запасом)."""
    return next((k for k in _CLASS_SCORE if k in (cls or "")), None)


# ── Отделка: 7 кодов bot/core/listing_intel.detect_finish_level ─────────
# Тоже перенесено из deal_score.py той же задачей — общее с comparable_
# score.py (finish_level_similarity).
_FINISH_QUALITY_SCORE = {
    "rough": 20, "prefinish": 35, "needs_repair": 25,
    "finished": 60, "renovated": 75, "furnished": 80, "designer": 95,
}
_FINISH_LABEL = {
    "rough": "черновая", "prefinish": "предчистовая", "needs_repair": "требует ремонта",
    "finished": "чистовая", "renovated": "свежий ремонт", "furnished": "с мебелью", "designer": "дизайнерский ремонт",
}
